"""The hub's FastAPI application (agent-facing REST API, v1)."""

from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import re
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection
from starlette.types import Scope
from pydantic import BaseModel, Field

from agenthub import __version__
from agenthub.config import HubConfig
from agenthub.hub import (
    approvals, asks, broadcasts, chats, db, device_board, device_stats, events, intents, private, prompts, questions,
    reports, reviewers, store, task_agents, tasks, wakes,
)
from agenthub.hub.auth import (
    AgentCaller, active_agent, any_agent, authenticate, get_conn, require_project, resolve_device,
)
from agenthub.hub.bus import CLOSE_FORBIDDEN, CLOSE_REVOKED, EventBus
from agenthub.hub.limits import AGENT_MIN_BODY_BYTES, AGENT_TEXT_CHARS, OPERATOR_MAX_BODY_BYTES, BodySizeLimit
from agenthub.hub.operator import authenticate_operator
from agenthub.hub.ratelimit import RateLimiter
from agenthub.hub.tailscale import TailscaleWhois, Whois

# KaTeX's fonts under /ui/static: not every system's mime table knows woff2.
mimetypes.add_type("font/woff2", ".woff2")

MAX_MESSAGE_CHARS = AGENT_TEXT_CHARS  # every /api/v1 route is an agent's; the operator writes through /ui
MAX_BOARD_VALUE_CHARS = 32_000
MAX_COMMAND_CHARS = 8_000
# A model name a runner may report: Claude Code's ids and aliases (claude-opus-5-5, sonnet, opus[1m], ...).
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]{0,99}")
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

log = logging.getLogger("agenthub.hub")


class BindError(Exception):
    pass


def check_bind(host: str, *, insecure_dev: bool) -> None:
    """The hub trusts tailscale serve's headers, so it must only listen on loopback."""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False  # hostnames are refused: "localhost" can resolve elsewhere
    if not loopback and not insecure_dev:
        raise BindError(
            f"refusing to bind to {host!r}: the hub must listen on a loopback IP (127.0.0.1) "
            "behind tailscale serve. Use --insecure-dev only for local development."
        )


INSECURE_DEV_WARNING = """
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!  agenthub is running with --insecure-dev                                  !!
!!  * tailscale device binding (whois) is DISABLED                           !!
!!  * Tailscale identity headers are IGNORED (no human operator via HTTP)    !!
!!  * any process that can reach the port and holds a token is trusted      !!
!!  Never use this mode on a shared machine or a reachable interface.       !!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""


@dataclass
class HubState:
    config: HubConfig
    insecure_dev: bool = False
    whois: Whois = field(default=None)  # type: ignore[assignment]
    bus_poll_seconds: float = 0.5
    csrf_secret: bytes = b""
    bus: EventBus = field(init=False)
    agent_limiter: RateLimiter = field(init=False)
    enroll_limiter: RateLimiter = field(init=False)
    enroll_global_limiter: RateLimiter = field(init=False)
    frame_limiter: RateLimiter = field(init=False)

    def __post_init__(self) -> None:
        cfg = self.config
        self.agent_limiter = RateLimiter(cfg.agent_rate_per_second, cfg.agent_rate_burst)
        wakes.configure(cfg.wake_loop_limit)
        per_minute = cfg.enroll_rate_per_minute
        self.enroll_limiter = RateLimiter(per_minute / 60, max(1, int(per_minute)))
        self.enroll_global_limiter = RateLimiter(4 * per_minute / 60, max(1, int(4 * per_minute)))
        # Agents send one heartbeat every 15 s; anything much faster is misbehaving.
        self.frame_limiter = RateLimiter(2.0, 20)
        if not self.csrf_secret:
            from agenthub.hub.operator import load_csrf_secret

            self.csrf_secret = load_csrf_secret()
        if self.whois is None:
            self.whois = TailscaleWhois(self.config.tailscale_bin, self.config.whois_cache_seconds)
        from agenthub.hub.dashboard import render_event_row
        from agenthub.hub.notify import Notifier

        self.notifier = Notifier(self.config)
        self.bus = EventBus(self.connect, poll_seconds=self.bus_poll_seconds,
                            render_for_operator=render_event_row, notifier=self.notifier)

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.config.db_path)


def expire_prompts(state: HubState) -> list[int]:
    """Time out group prompt discussions older than ``prompt_discussion_minutes`` (see :func:`prompts.expire`).
    Since 0.3.8 no new prompt is made and the hub no longer runs this on a timer; prompts left open by 0.3.7 stay
    as they were, for a hub rolled back to 0.3.7 to settle."""
    conn = state.connect()
    try:
        with db.transaction(conn):
            return prompts.expire(conn, datetime.now(UTC), minutes=state.config.prompt_discussion_minutes)
    finally:
        conn.close()


RETIRED = "retired in 0.3.8: talk in chats (chat_post, read_inbox, take_task, sit_out)"


# --- request models -----------------------------------------------------------


class EnrollRequest(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    name: str = Field(min_length=1, max_length=64)


class PostMessageRequest(BaseModel):
    channel: str = Field(default="general", max_length=64)
    body: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    to: str | None = Field(default=None, max_length=64)


class PrivateMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=private.MAX_BODY_CHARS)


class DeviceMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=device_board.MAX_BODY_CHARS)
    to: str | None = Field(default=None, max_length=64)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=questions.MAX_CHARS)
    task_id: int | None = None


class BoardPutRequest(BaseModel):
    value: str = Field(max_length=MAX_BOARD_VALUE_CHARS)
    expected_version: int = Field(ge=0)


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    assignee: str | None = Field(default=None, max_length=64)
    budget: dict[str, int] | None = None
    reason: str = Field(default="", max_length=2000)


class UpdateTaskRequest(BaseModel):
    state: str | None = Field(default=None, max_length=32)
    note: str = Field(default="", max_length=4000)


class ApprovalRequest(BaseModel):
    kind: str = Field(max_length=32)
    project: str | None = Field(default=None, max_length=64)
    reason: str = Field(default="", max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)


class AskRequest(BaseModel):
    to: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    timeout_seconds: int = Field(default=300, ge=asks.MIN_TIMEOUT, le=asks.MAX_TIMEOUT)


class IntentRequest(BaseModel):
    patterns: list[str] = Field(min_length=1, max_length=intents.MAX_PATTERNS)
    description: str = Field(default="", max_length=2000)
    ttl_minutes: int = Field(default=60, ge=1, le=intents.MAX_TTL_MINUTES)


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class RunEndRequest(BaseModel):
    outcome: str = Field(max_length=64)
    session_id: str | None = Field(default=None, max_length=128)
    num_turns: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    chain_ids: list[Annotated[str, Field(max_length=64)]] | None = Field(default=None, max_length=100)


class RunSessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


class ProgressItem(BaseModel):
    kind: str = Field(max_length=32)
    text: str = Field(max_length=2000)


class ProgressRequest(BaseModel):
    run_id: int | None = None
    items: list[ProgressItem] = Field(max_length=tasks.MAX_PROGRESS_ITEMS)


class FinishRequest(BaseModel):
    note: str = Field(default="", max_length=4000)


class VerdictRequest(BaseModel):
    verdict: str = Field(max_length=32)
    notes: str = Field(default="", max_length=4000)


class PauseRequest(BaseModel):
    until: str | None = Field(default=None, max_length=64)


class ToolPermissionRequest(BaseModel):
    task_id: int
    tool_name: str = Field(min_length=1, max_length=200)
    tool_input: Any = None
    reason: str = Field(default="", max_length=2000)


class DiscussionRequest(BaseModel):
    body: str = Field(min_length=1, max_length=AGENT_TEXT_CHARS)


class ChatPostRequest(BaseModel):
    body: str = Field(min_length=1, max_length=chats.MAX_BODY_CHARS)
    to: str | None = Field(default=None, max_length=64)


class ChatStreamRequest(BaseModel):
    task_id: int | None = None
    # Items are checked one by one (an unknown or malformed one is skipped and counted), so a newer runner's items
    # don't make the whole call fail.
    items: list[Any] = Field(max_length=chats.MAX_STREAM_ITEMS)


class ChatRunRequest(BaseModel):
    outcome: str = Field(min_length=1, max_length=64)
    seconds: float = Field(ge=0, le=chats.MAX_RUN_SECONDS)
    session_id: str | None = Field(default=None, max_length=128)
    num_turns: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    project: str | None = Field(default=None, max_length=64)
    task_id: int | None = None
    # the wake chains of the messages the session took in (0.3.9.1)
    chain_ids: list[Annotated[str, Field(max_length=64)]] | None = Field(default=None, max_length=100)


class DeclineRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DeliveryClaimRequest(BaseModel):
    mode: str = Field(max_length=16)
    note: str = Field(default="", max_length=chats.MAX_DETAIL_CHARS)


class ChatSessionRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=128)


class TakeTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=chats.MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=chats.MAX_TASK_BODY_CHARS)


class SitOutRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class PromptFinishRequest(BaseModel):
    ok: bool
    note: str = Field(default="", max_length=4000)
    # The runner found the prompt too old to run when it started (the hub checks the age and writes the note).
    stale: bool = False


class PromptRunRequest(BaseModel):
    outcome: str = Field(min_length=1, max_length=64)
    seconds: float = Field(ge=0, le=prompts.MAX_RUN_SECONDS)
    session_id: str | None = Field(default=None, max_length=128)
    num_turns: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    project: str | None = Field(default=None, max_length=64)


class PromptClaimRequest(BaseModel):
    title: str = Field(min_length=1, max_length=tasks.MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=tasks.MAX_BODY_CHARS)
    project: str | None = Field(default=None, max_length=64)


class PromptDeclineRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ReportRequest(BaseModel):
    project: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=reports.MAX_TITLE_CHARS)
    summary: str = Field(default="", max_length=reports.MAX_SUMMARY_CHARS)
    url: str | None = Field(default=None, max_length=reports.MAX_URL_CHARS)
    html: str | None = Field(default=None, max_length=reports.MAX_HTML_CHARS)
    task_id: int | None = None
    report_id: int | None = None


class CommandApprovalRequest(BaseModel):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    cwd: str = Field(min_length=1, max_length=4096)
    project: str | None = Field(default=None, max_length=64)
    reason: str = Field(default="", max_length=2000)


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Caller = Annotated[AgentCaller, Depends(active_agent)]


def _private_entry(m: dict[str, Any], agent_id: str) -> dict[str, Any]:
    return {"id": m["id"], "kind": "private", "project": None, "channel": "private", "author": m["author"],
            "author_name": None, "to_agent": agent_id, "body": m["body"], "ts": m["ts"], "addressed_to_me": True}


def _device_entry(m: dict[str, Any]) -> dict[str, Any]:
    return {"id": m["id"], "kind": "device", "project": None, "channel": "device", "author": m["author"],
            "author_name": m["author_name"], "to_agent": m["to_agent"], "body": m["body"], "ts": m["ts"],
            "addressed_to_me": m["addressed_to_me"]}


def jsonable(value: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(value)


def create_app(state: HubState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.init_db(state.config.db_path)
        conn.execute("UPDATE agents SET connected = 0")
        with db.transaction(conn):  # what 0.3.7 wrote (also while rolled back) shows in the chats
            added = chats.backfill(conn)
        if added:
            log.info("copied %d earlier messages, prompts, approvals and questions into chats", added)
        conn.close()
        state.bus.start()
        try:
            yield
        finally:
            await state.bus.stop()

    app = FastAPI(title="agenthub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.hub = state
    async def is_operator(scope: Scope) -> bool:
        """Whether a dashboard request comes from the operator (the dashboard's own check, run on its headers)."""
        conn = state.connect()
        try:
            await authenticate_operator(HTTPConnection(scope), conn)
            return True
        except HTTPException:
            return False
        finally:
            conn.close()

    max_body = max(state.config.max_body_bytes, AGENT_MIN_BODY_BYTES)
    app.add_middleware(BodySizeLimit, max_bytes=max_body,
                       larger={"/api/v1/reports": max(reports.MAX_REQUEST_BYTES, max_body)},
                       operator_bytes=OPERATOR_MAX_BODY_BYTES, is_operator=is_operator)

    from fastapi.staticfiles import StaticFiles

    from agenthub.hub import dashboard

    app.include_router(dashboard.build_router())
    app.include_router(wakes.build_router())  # operator-only JSON under /ui/api
    app.include_router(reviewers.build_router())  # operator-only JSON under /ui/api
    app.include_router(device_stats.build_router())  # operator-only JSON under /ui/api
    app.include_router(broadcasts.build_router())  # operator-only JSON under /ui/api
    app.mount("/ui/static", StaticFiles(directory=dashboard.HERE / "static"), name="static")

    @app.exception_handler(store.StoreError)
    async def store_error(request: Request, exc: store.StoreError):
        status = {store.NotFound: 404, store.Conflict: 409, store.Invalid: 422, store.Forbidden: 403}
        if request.url.path.startswith("/ui") and not request.url.path.startswith("/ui/api/"):
            return dashboard.error_page(status.get(type(exc), 400), str(exc))
        body: dict[str, Any] = {"detail": str(exc)}
        if isinstance(exc, store.Conflict):
            body.update(exc.details)
        return JSONResponse(body, status_code=status.get(type(exc), 400))

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    api = "/api/v1"

    # --- enrollment and identity ---------------------------------------------

    @app.post(f"{api}/enroll", status_code=201)
    async def enroll(body: EnrollRequest, request: Request, conn: Conn) -> dict[str, Any]:
        # Throttle code guessing per caller address and overall.
        caller = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")
        state.enroll_limiter.check(f"ip:{caller}", "enrollment attempts")
        state.enroll_global_limiter.check("all", "enrollment attempts")
        device = await resolve_device(request)
        with db.transaction(conn):
            agent, token = store.redeem_enrollment(conn, code=body.code, name=body.name, device=device)
            approvals.create(
                conn, kind="agent_enrollment", requester=agent.id, device=device.name,
                details={"agent_id": agent.id, "name": agent.name, "device": device.__dict__,
                         "projects": list(agent.allowed_projects)},
            )
        return {"agent": agent.public(), "token": token}

    @app.get(f"{api}/agents/me")
    def me(caller: Annotated[AgentCaller, Depends(any_agent)]) -> dict[str, Any]:
        return caller.agent.public()

    @app.get(f"{api}/projects")
    def projects(caller: Caller) -> dict[str, Any]:
        return {"projects": list(caller.agent.allowed_projects)}

    # --- chats -------------------------------------------------------------------

    @app.get(f"{api}/chats/deliveries")
    def pending_deliveries(caller: Caller, conn: Conn) -> dict[str, Any]:
        """The messages (the operator's and other agents') the caller's runner hasn't given to a session yet, oldest
        first. ``wake`` says whether one may start a session (False: only into a running one, or it waits)."""
        return {"deliveries": chats.pending_deliveries(conn, caller.agent)}

    @app.post(f"{api}/chats/deliveries/{{delivery_id}}")
    def claim_delivery(delivery_id: int, body: DeliveryClaimRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """The runner takes one (compare-and-set: 409 when it was delivered already, so it skips it)."""
        with db.transaction(conn):
            return chats.claim_delivery(conn, caller.agent, delivery_id, mode=body.mode, note=body.note)

    @app.get(f"{api}/chats/agent/me/session")
    def chat_session(caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return {"session_id": chats.session(conn, caller.agent)}

    @app.put(f"{api}/chats/agent/me/session")
    def set_chat_session(body: ChatSessionRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            chats.set_session(conn, caller.agent, body.session_id)
        return {"session_id": body.session_id}

    @app.get(f"{api}/chats/{{kind}}/{{ref}}/messages")
    def chat_history(kind: str, ref: str, caller: Caller, conn: Conn,
                     limit: Annotated[int, Query(ge=1, le=200)] = 30, after: int = 0) -> dict[str, Any]:
        with db.transaction(conn):
            return chats.history(conn, caller.agent, kind, ref, limit=limit, after_id=after)

    @app.post(f"{api}/chats/{{kind}}/{{ref}}/messages", status_code=201)
    def chat_post(kind: str, ref: str, body: ChatPostRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """Write in a chat; the message is delivered to its recipients, which may wake them (0.3.9.1). Each agent it
        wakes costs a token of the caller's rate limit: over it, nothing is posted (429)."""
        with db.transaction(conn):
            found = chats.post(conn, caller.agent, kind, ref, body.body, to=body.to, device=caller.device.name)
            if waking := sum(w["wake"] for w in found["wakes"]):  # raising here rolls the post back
                state.agent_limiter.check(caller.agent.id, "wakes", cost=waking)
            return found

    @app.post(f"{api}/chats/{{kind}}/{{ref}}/stream", status_code=201)
    def chat_stream(kind: str, ref: str, body: ChatStreamRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """A session's live output: text blocks, tool lines (folded into one row per stretch) and status lines."""
        with db.transaction(conn):
            return chats.stream(conn, caller.agent, kind, ref, body.items, task_id=body.task_id,
                                device=caller.device.name)

    @app.post(f"{api}/chats/{{kind}}/{{ref}}/runs", status_code=201)
    def chat_run(kind: str, ref: str, body: ChatRunRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """One chat session the runner ran (turns, est. cost, duration), for the Usage page."""
        with db.transaction(conn):
            return chats.record_run(conn, caller.agent, kind, ref, outcome=body.outcome, seconds=body.seconds,
                                    session_id=body.session_id, num_turns=body.num_turns, cost_usd=body.cost_usd,
                                    project=body.project, task_id=body.task_id, chain_ids=body.chain_ids)

    @app.post(f"{api}/chats/requests/{{message_id}}/take", status_code=201)
    def take_task(message_id: int, body: TakeTaskRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return chats.take_task(conn, caller.agent, message_id, title=body.title, body=body.body,
                                   device=caller.device.name)

    @app.post(f"{api}/chats/requests/{{message_id}}/decline", status_code=201)
    def decline_request(message_id: int, body: DeclineRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """Decline another agent's message delivered to the caller; the reason shows in its chat."""
        with db.transaction(conn):
            return chats.decline(conn, caller.agent, message_id, reason=body.reason, device=caller.device.name)

    @app.post(f"{api}/chats/requests/{{message_id}}/sit-out", status_code=201)
    def sit_out(message_id: int, body: SitOutRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return chats.sit_out(conn, caller.agent, message_id, reason=body.reason, device=caller.device.name)

    # --- messages ------------------------------------------------------------

    @app.post(f"{api}/projects/{{project}}/messages", status_code=201)
    def post_message(project: str, caller: Caller) -> dict[str, Any]:
        """Retired: channels are read-only history (copied into the project's and tasks' chats)."""
        project = require_project(caller, project)
        raise HTTPException(410, RETIRED)

    @app.get(f"{api}/projects/{{project}}/messages")
    def recent_messages(project: str, caller: Caller) -> dict[str, Any]:
        project = require_project(caller, project)
        raise HTTPException(410, RETIRED)

    @app.get(f"{api}/inbox")
    def inbox(
        caller: Caller, conn: Conn,
        project: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        peek: bool = False,
        addressed_only: bool = False,
    ) -> dict[str, Any]:
        """Unread mail, all of it counting toward ``limit``, in this order: the operator's chat messages not yet given
        to a session (reading them delivers them), private messages 0.3.7 left unread, the caller's device board
        (these three whatever ``project`` is), other agents' messages delivered to the caller (0.3.9.1; reading
        delivers them too), what agents and the hub wrote in the caller's task and project chats, and channel
        messages 0.3.7 left unread."""
        projects = [require_project(caller, project)] if project else list(caller.agent.allowed_projects)
        mark = not (peek or addressed_only)
        agent = caller.agent
        sources = [
            lambda n, m: chats.operator_mail(conn, agent, limit=n, mark_read=m),
            lambda n, m: [_private_entry(x, agent.id) for x in private.unread_for_agent(conn, agent, mark_read=m,
                                                                                         limit=n)],
            lambda n, m: [_device_entry(x) for x in device_board.unread_for_agent(
                conn, agent, mark_read=m, limit=n, addressed_only=addressed_only)],
            lambda n, m: chats.operator_mail(conn, agent, limit=n, mark_read=m, addressed_only=addressed_only,
                                             projects=projects if project else None, from_agents=True),
            lambda n, m: chats.chat_mail(conn, agent, projects=projects, limit=n, mark_read=m,
                                         addressed_only=addressed_only),
            lambda n, m: store.read_inbox(conn, agent, projects=projects, limit=n, mark_read=m,
                                          addressed_only=addressed_only)["messages"],
        ]
        entries: list[dict[str, Any]] = []
        more = False
        with db.transaction(conn):
            for fetch in sources:
                room = limit - len(entries)
                got = fetch(room, mark) if room > 0 else []
                entries += got
                # Anything still unread beyond what was just returned? (Marked rows no longer are.)
                seen = 0 if mark else len(got)
                more = more or len(fetch(seen + 1, False)) > seen
        return {"messages": entries, "more": more}

    @app.post(f"{api}/private", status_code=201)
    def message_operator(body: PrivateMessageRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """Write to the operator in the caller's own chat."""
        with db.transaction(conn):
            return chats.post(conn, caller.agent, "agent", "me", body.body, device=caller.device.name)

    @app.post(f"{api}/device/messages", status_code=201)
    def post_device(body: DeviceMessageRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """Write on the board of the caller's own machine (the device it enrolled from)."""
        with db.transaction(conn):
            return device_board.post(conn, caller.agent.device_node_id, author=caller.agent.id, body=body.body,
                                     to=body.to, device=caller.device.name)

    @app.get(f"{api}/device/messages")
    def recent_device_messages(caller: Caller, conn: Conn,
                               limit: Annotated[int, Query(ge=1, le=200)] = 20) -> dict[str, Any]:
        """Recent notes on the caller's own machine's board (read-only; never another machine's)."""
        return {"messages": device_board.recent_for_agent(conn, caller.agent, limit)}

    # --- questions to the operator -------------------------------------------

    @app.post(f"{api}/projects/{{project}}/questions", status_code=201)
    def ask_operator(project: str, body: QuestionRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        project = require_project(caller, project)
        with db.transaction(conn):
            return questions.ask(conn, caller.agent, project=project, question=body.question,
                                 task_id=body.task_id, device=caller.device.name)

    @app.get(f"{api}/questions/{{question_id}}")
    def get_question(question_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        return questions.get_own(conn, question_id, caller.agent)

    @app.post(f"{api}/questions/{{question_id}}/withdraw")
    def withdraw_question(question_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return questions.withdraw(conn, question_id, caller.agent)

    # --- prompts (retired in 0.3.8; an older runner finds none pending and idles) ---------------------------

    @app.get(f"{api}/prompts/pending")
    def pending_prompts(caller: Caller) -> dict[str, Any]:
        return {"prompts": []}

    @app.api_route(f"{api}/prompts/{{rest:path}}", methods=["GET", "POST"])
    def retired_prompts(rest: str, caller: Caller) -> dict[str, Any]:
        raise HTTPException(410, RETIRED)

    # --- board ---------------------------------------------------------------

    @app.get(f"{api}/projects/{{project}}/board")
    def board_list(project: str, caller: Caller, conn: Conn, prefix: str = "") -> dict[str, Any]:
        project = require_project(caller, project)
        return {"keys": store.board_list(conn, project, prefix[:200])}

    @app.get(f"{api}/projects/{{project}}/board/{{key:path}}")
    def board_get(project: str, key: str, caller: Caller, conn: Conn) -> dict[str, Any]:
        project = require_project(caller, project)
        return store.board_get(conn, project, key)

    @app.put(f"{api}/projects/{{project}}/board/{{key:path}}")
    def board_put(project: str, key: str, body: BoardPutRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        project = require_project(caller, project)
        with db.transaction(conn):
            return store.board_put(
                conn, actor=caller.agent.actor, project=project, key=key, value=body.value,
                expected_version=body.expected_version, device=caller.device.name,
            )

    # --- reports -------------------------------------------------------------

    @app.post(f"{api}/reports", status_code=201)
    def publish_report(body: ReportRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return reports.publish(conn, caller.agent, project=body.project, title=body.title, summary=body.summary,
                                   url=body.url, html=body.html, task_id=body.task_id, report_id=body.report_id,
                                   device=caller.device.name)

    @app.get(f"{api}/projects/{{project}}/reports")
    def list_reports(project: str, caller: Caller, conn: Conn, task_id: int | None = None) -> dict[str, Any]:
        project = require_project(caller, project)
        return {"reports": reports.list_reports(conn, projects=[project], task_id=task_id)}

    @app.get(f"{api}/reports/{{report_id}}")
    def get_report(report_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        return reports.get_for_agent(conn, report_id, caller.agent)

    # --- tasks ---------------------------------------------------------------

    def visible_task(caller: AgentCaller, conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
        task = tasks.get_task(conn, task_id)
        if task["project"] not in caller.agent.allowed_projects:
            raise HTTPException(404, f"no task {task_id}")  # don't reveal other projects' tasks
        return task

    @app.get(f"{api}/projects/{{project}}/tasks")
    def list_tasks(
        project: str, caller: Caller, conn: Conn, state: str | None = None, mine: bool = False,
    ) -> dict[str, Any]:
        project = require_project(caller, project)
        member_id = caller.agent.id if mine else None
        return {"tasks": tasks.list_tasks(conn, projects=[project], state=state, agent=member_id)}

    @app.post(f"{api}/projects/{{project}}/tasks", status_code=201)
    def create_task(project: str, body: CreateTaskRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        project = require_project(caller, project)
        with db.transaction(conn):
            task, approval = approvals.agent_create_task(
                conn, caller.agent, project=project, title=body.title, body=body.body,
                assignee=body.assignee, budget=body.budget, reason=body.reason, device=caller.device.name,
            )
        return {"task": task, "approval": approval}

    @app.get(f"{api}/tasks/{{task_id}}")
    def get_task(task_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        return visible_task(caller, conn, task_id)

    @app.post(f"{api}/tasks/{{task_id}}/claim")
    def claim_task(task_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            return tasks.transition(conn, task_id, "claimed", actor=caller.agent.id, role="agent",
                                    agent=caller.agent, device=caller.device.name)

    @app.post(f"{api}/tasks/{{task_id}}/update")
    def update_task(task_id: int, body: UpdateTaskRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        if body.state is None and not body.note:
            raise HTTPException(422, "provide a new state and/or a note")
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            if body.state is None:
                tasks.add_note(conn, task_id, body.note, agent=caller.agent, device=caller.device.name)
                return tasks.get_task(conn, task_id)
            return tasks.transition(conn, task_id, body.state, actor=caller.agent.id, role="agent",
                                    agent=caller.agent, note=body.note, device=caller.device.name)

    @app.post(f"{api}/tasks/{{task_id}}/finish")
    def finish_task(task_id: int, body: FinishRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            return task_agents.finish(conn, task_id, caller.agent, note=body.note, device=caller.device.name)

    @app.post(f"{api}/tasks/{{task_id}}/verdict")
    def task_verdict(task_id: int, body: VerdictRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            return task_agents.record_verdict(conn, task_id, caller.agent, verdict=body.verdict,
                                              notes=body.notes, device=caller.device.name)

    @app.post(f"{api}/tasks/{{task_id}}/pause")
    def pause_task(task_id: int, body: PauseRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            return task_agents.pause(conn, task_id, caller.agent, until=body.until, device=caller.device.name)

    @app.get(f"{api}/tasks/{{task_id}}/runs")
    def task_runs(task_id: int, caller: Caller, conn: Conn, agent: str | None = None) -> dict[str, Any]:
        visible_task(caller, conn, task_id)
        return {"runs": tasks.list_runs(conn, task_id), "usage": tasks.usage(conn, task_id, agent)}

    @app.post(f"{api}/tasks/{{task_id}}/runs", status_code=201)
    def start_run(task_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            return tasks.start_run(conn, task_id, caller.agent, device=caller.device.name)

    @app.post(f"{api}/tasks/{{task_id}}/runs/{{run_id}}/session")
    def run_session(task_id: int, run_id: int, body: RunSessionRequest, caller: Caller, conn: Conn) -> dict:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            tasks.set_run_session(conn, task_id, run_id, caller.agent, body.session_id)
        return {"ok": True}

    @app.post(f"{api}/tasks/{{task_id}}/runs/{{run_id}}/end")
    def end_run(task_id: int, run_id: int, body: RunEndRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            found = tasks.end_run(conn, task_id, run_id, caller.agent, outcome=body.outcome,
                                  session_id=body.session_id, num_turns=body.num_turns, cost_usd=body.cost_usd,
                                  device=caller.device.name)
            wakes.store_run_chains(conn, "task_runs", run_id, body.chain_ids)
            return found

    @app.post(f"{api}/tasks/{{task_id}}/progress", status_code=201)
    def progress(task_id: int, body: ProgressRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, task_id)
            event_id = tasks.record_progress(conn, task_id, caller.agent, [i.model_dump() for i in body.items],
                                             run_id=body.run_id, device=caller.device.name)
        return {"event_id": event_id}

    # --- approvals (agents may file and read their own; never decide) ----------

    @app.post(f"{api}/approvals/tool", status_code=201)
    def request_tool(body: ToolPermissionRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, body.task_id)
            return approvals.request_tool_permission(
                conn, caller.agent, task_id=body.task_id, tool_name=body.tool_name, tool_input=body.tool_input,
                reason=body.reason, device=caller.device.name)

    @app.post(f"{api}/approvals/tool/consume")
    def consume_tool(body: ToolPermissionRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            visible_task(caller, conn, body.task_id)
            approval = approvals.consume_tool_permission(conn, caller.agent, task_id=body.task_id,
                                                         tool_name=body.tool_name, tool_input=body.tool_input)
        return {"allowed": approval is not None, "approval_id": approval["id"] if approval else None}

    @app.post(f"{api}/approvals", status_code=201)
    def request_approval(body: ApprovalRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return approvals.request(conn, caller.agent, kind=body.kind, details=body.details,
                                     project=body.project, reason=body.reason, device=caller.device.name)

    @app.get(f"{api}/approvals/{{approval_id}}")
    def get_approval(approval_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        approval = approvals.get(conn, approval_id)
        if approval["requester"] != caller.agent.id:
            raise HTTPException(404, f"no approval {approval_id}")
        return approval

    @app.get(f"{api}/approvals")
    def my_approvals(caller: Caller, conn: Conn, awaiting_reply: bool = False) -> dict[str, Any]:
        """The caller's own pending approvals (optionally only those where the operator asked something)."""
        return {"approvals": approvals.list_for_agent(conn, caller.agent.id, awaiting_reply_only=awaiting_reply)}

    @app.post(f"{api}/approvals/{{approval_id}}/discussion", status_code=201)
    def discuss(approval_id: int, body: DiscussionRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            if approvals.get(conn, approval_id)["requester"] != caller.agent.id:
                raise HTTPException(404, f"no approval {approval_id}")
            return approvals.post_discussion(conn, approval_id, author=caller.agent.id, body=body.body,
                                             device=caller.device.name)

    @app.post(f"{api}/approvals/command", status_code=201)
    def request_command(body: CommandApprovalRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return approvals.request_command(conn, caller.agent, command=body.command, cwd=body.cwd,
                                             project=body.project, reason=body.reason, device=caller.device.name)

    @app.post(f"{api}/approvals/command/consume")
    def consume_command(body: CommandApprovalRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            approval = approvals.consume_command(conn, caller.agent, command=body.command, cwd=body.cwd)
        return {"allowed": approval is not None, "approval_id": approval["id"] if approval else None}

    # --- intents ---------------------------------------------------------------

    @app.post(f"{api}/projects/{{project}}/intents", status_code=201)
    def announce_intent(project: str, body: IntentRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        project = require_project(caller, project)
        with db.transaction(conn):
            return intents.announce(conn, caller.agent, project=project, patterns=body.patterns,
                                    description=body.description, ttl_minutes=body.ttl_minutes,
                                    device=caller.device.name)

    @app.get(f"{api}/projects/{{project}}/intents")
    def list_intents(project: str, caller: Caller, conn: Conn) -> dict[str, Any]:
        project = require_project(caller, project)
        return {"intents": intents.active(conn, project)}

    @app.post(f"{api}/intents/{{intent_id}}/clear")
    def clear_intent(intent_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return intents.clear(conn, caller.agent, intent_id, device=caller.device.name)

    # --- asks ----------------------------------------------------------------

    @app.post(f"{api}/projects/{{project}}/asks", status_code=201)
    def create_ask(project: str, body: AskRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        project = require_project(caller, project)
        with db.transaction(conn):
            return asks.create(conn, caller.agent, project=project, to=body.to, question=body.question,
                               timeout_seconds=body.timeout_seconds, device=caller.device.name)

    @app.get(f"{api}/asks")
    def pending_asks(caller: Caller, conn: Conn) -> dict[str, Any]:
        return {"asks": asks.pending_for(conn, caller.agent)}

    @app.get(f"{api}/asks/{{ask_id}}")
    def get_ask(ask_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        return asks.get(conn, ask_id, viewer=caller.agent)

    @app.post(f"{api}/asks/{{ask_id}}/answer")
    def answer_ask(ask_id: int, body: AnswerRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return asks.answer(conn, caller.agent, ask_id, body.answer, device=caller.device.name)

    # --- WebSocket: push notifications + heartbeats -------------------------

    @app.websocket(f"{api}/ws")
    async def agent_socket(websocket: WebSocket) -> None:
        conn = state.connect()
        try:
            try:
                caller = await authenticate(websocket, conn, allow_pending=False)
            except HTTPException as exc:
                # Closing before accept makes the handshake fail with HTTP 403.
                await websocket.close(code=CLOSE_FORBIDDEN, reason=str(exc.detail)[:120])
                return
            agent_id = caller.agent.id
            if not state.bus.register(agent_id, websocket):
                await websocket.close(code=CLOSE_FORBIDDEN, reason="too many connections")
                return
            try:
                await websocket.accept()
                _set_connected(conn, agent_id, +1, device=caller.device.name)
                last_event = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
                await websocket.send_json(
                    {"type": "hello", "agent": caller.agent.public(), "last_event_id": last_event}
                )
                await _agent_socket_loop(websocket, conn, agent_id)
            except WebSocketDisconnect:
                pass
            finally:
                state.bus.unregister(agent_id, websocket)
                _set_connected(conn, agent_id, -1)
        finally:
            conn.close()

    def _set_connected(conn: sqlite3.Connection, agent_id: str, delta: int, device: str | None = None) -> None:
        with db.transaction(conn):
            conn.execute(
                "UPDATE agents SET connected = MAX(connected + ?, 0), last_heartbeat = ? WHERE id = ?",
                (delta, db.now_iso(), agent_id),
            )
            events.emit(conn, type="agent.connected" if delta > 0 else "agent.disconnected",
                        actor=agent_id, device=device)

    def _reported_dirs(conn: sqlite3.Connection, message: dict[str, Any]) -> tuple[str, str | None] | None:
        """The runner's reported checkouts (compact JSON) and current directory, or None if the frame has none
        (an older runner's plain heartbeat) or anything invalid (then neither changes). runner.toml's project keys
        match ignoring case, so a key is stored as the project's own spelling (a key naming no project stays)."""
        if "checkouts" not in message and "current_dir" not in message:
            return None
        checkouts, current = message.get("checkouts", {}), message.get("current_dir")
        valid = (
            isinstance(checkouts, dict) and len(checkouts) <= 50
            and all(isinstance(k, str) and store.NAME_RE.fullmatch(k) and isinstance(v, str) and len(v) <= 500
                    for k, v in checkouts.items())
            and (current is None or (isinstance(current, str) and len(current) <= 500))
        )
        if not valid:
            return None
        checkouts = {store.project_name(conn, k) or k: v for k, v in checkouts.items()}
        return json.dumps(checkouts, separators=(",", ":")), current

    def _reported_model(message: dict[str, Any]) -> str | None:
        """The model the runner reports (e.g. "claude-opus-5-5"), or None if absent or not a plausible model name."""
        model = message.get("model")
        return model if isinstance(model, str) and MODEL_RE.fullmatch(model) else None

    def _reported_effort(message: dict[str, Any]) -> str | None:
        """The reasoning effort the runner reports (e.g. "high"), or None if absent or not a known level."""
        effort = message.get("effort")
        return effort if isinstance(effort, str) and effort in EFFORT_LEVELS else None

    async def _agent_socket_loop(websocket: WebSocket, conn: sqlite3.Connection, agent_id: str) -> None:
        while True:
            try:
                message = await websocket.receive_json()
            except ValueError:
                await websocket.close(code=CLOSE_FORBIDDEN, reason="invalid frame")
                return
            if state.frame_limiter.try_acquire(agent_id):
                await websocket.close(code=CLOSE_FORBIDDEN, reason="too many frames")
                return
            if not isinstance(message, dict) or message.get("type") != "heartbeat":
                continue
            # Re-check the agent on every heartbeat as a backstop to the bus.
            agent = store.get_agent(conn, agent_id)
            if agent is None or agent.status != "active":
                await websocket.close(code=CLOSE_REVOKED, reason="agent is no longer active")
                return
            # One write per heartbeat; the directories ride along only when they differ from what is stored.
            # A runner that runs prompts says "prompts": 1 on every heartbeat; an older runner's heartbeat lacks it
            # (and otherwise works as before), so its agent isn't woken by prompts.
            # The model and effort, likewise, only when the runner reports one that differs (an older runner
            # reports neither).
            # Likewise a runner that runs chats (0.3.8) says "chat": 1; the hub records it for the UI to show,
            # and an older runner's operator messages wait as pending deliveries until it is upgraded.
            prompt_support = 1 if message.get("prompts") == 1 else 0
            chat_support = 1 if message.get("chat") == 1 else 0
            dirs, model, effort = _reported_dirs(conn, message), _reported_model(message), _reported_effort(message)
            stored = conn.execute("SELECT checkouts, current_dir, model, effort FROM agents WHERE id = ?",
                                  (agent_id,)).fetchone()
            sets = ["last_heartbeat = ?", "prompt_support = ?", "chat_support = ?"]
            args: list[Any] = [db.now_iso(), prompt_support, chat_support]
            if dirs is not None and (stored["checkouts"], stored["current_dir"]) != dirs:
                sets.append("checkouts = ?, current_dir = ?")
                args.extend(dirs)
            if model is not None and stored["model"] != model:
                sets.append("model = ?")
                args.append(model)
            if effort is not None and stored["effort"] != effort:
                sets.append("effort = ?")
                args.append(effort)
            conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", (*args, agent_id))
            # Usage stats (0.3.10) belong to the caller's machine, never to anything the frame names; at most one
            # sample per machine every few seconds is kept, however many of its runners report.
            if "stats" in message:
                device_stats.record(conn, agent.device_node_id, agent_id, message["stats"])
            await websocket.send_json({"type": "heartbeat_ack", "ts": db.now_iso()})

    # --- search --------------------------------------------------------------

    @app.get(f"{api}/search")
    def search(
        caller: Caller, conn: Conn,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        project: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        projects = [require_project(caller, project)] if project else list(caller.agent.allowed_projects)
        return {"results": chats.with_bodies(conn, events.search(conn, q, projects=projects, limit=limit))}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/ui") and not request.url.path.startswith("/ui/api/"):
            return dashboard.error_page(exc.status_code, str(exc.detail))
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/ui") and not request.url.path.startswith("/ui/api/"):
            return dashboard.error_page(422, "; ".join(str(e.get("msg")) for e in exc.errors()))
        return JSONResponse({"detail": jsonable(exc.errors())}, status_code=422)

    return app
