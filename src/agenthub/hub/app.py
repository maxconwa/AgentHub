"""The hub's FastAPI application (agent-facing REST API, v1)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import mimetypes
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agenthub import __version__
from agenthub.config import HubConfig
from agenthub.hub import (
    approvals, asks, db, device_board, events, intents, private, prompts, questions, store, task_agents, tasks,
)
from agenthub.hub.auth import (
    AgentCaller, active_agent, any_agent, authenticate, get_conn, require_project, resolve_device,
)
from agenthub.hub.bus import CLOSE_FORBIDDEN, CLOSE_REVOKED, EventBus
from agenthub.hub.limits import BodySizeLimit
from agenthub.hub.ratelimit import RateLimiter
from agenthub.hub.tailscale import TailscaleWhois, Whois

# KaTeX's fonts under /ui/static: not every system's mime table knows woff2.
mimetypes.add_type("font/woff2", ".woff2")

MAX_MESSAGE_CHARS = 16_000
MAX_BOARD_VALUE_CHARS = 32_000
MAX_COMMAND_CHARS = 8_000

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
    prompt_expiry_seconds: float = 60.0
    csrf_secret: bytes = b""
    bus: EventBus = field(init=False)
    agent_limiter: RateLimiter = field(init=False)
    enroll_limiter: RateLimiter = field(init=False)
    enroll_global_limiter: RateLimiter = field(init=False)
    frame_limiter: RateLimiter = field(init=False)

    def __post_init__(self) -> None:
        cfg = self.config
        self.agent_limiter = RateLimiter(cfg.agent_rate_per_second, cfg.agent_rate_burst)
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
    """Time out group prompt discussions older than ``prompt_discussion_minutes`` (see :func:`prompts.expire`)."""
    conn = state.connect()
    try:
        with db.transaction(conn):
            return prompts.expire(conn, datetime.now(UTC), minutes=state.config.prompt_discussion_minutes)
    finally:
        conn.close()


async def _expire_prompts_forever(state: HubState) -> None:
    while True:
        await asyncio.sleep(state.prompt_expiry_seconds)
        try:
            await asyncio.to_thread(expire_prompts, state)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("prompt expiry failed")


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
    body: str = Field(min_length=1, max_length=4000)


class PromptFinishRequest(BaseModel):
    ok: bool
    note: str = Field(default="", max_length=4000)


class PromptClaimRequest(BaseModel):
    title: str = Field(min_length=1, max_length=tasks.MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=tasks.MAX_BODY_CHARS)
    project: str | None = Field(default=None, max_length=64)


class PromptDeclineRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class CommandApprovalRequest(BaseModel):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    cwd: str = Field(min_length=1, max_length=4096)
    project: str | None = Field(default=None, max_length=64)
    reason: str = Field(default="", max_length=2000)


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Caller = Annotated[AgentCaller, Depends(active_agent)]


def jsonable(value: Any) -> Any:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(value)


def create_app(state: HubState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.init_db(state.config.db_path)
        conn.execute("UPDATE agents SET connected = 0")
        conn.close()
        state.bus.start()
        expiry = asyncio.create_task(_expire_prompts_forever(state), name="agenthub-prompt-expiry")
        try:
            yield
        finally:
            expiry.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await expiry
            await state.bus.stop()

    app = FastAPI(title="agenthub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.hub = state
    app.add_middleware(BodySizeLimit, max_bytes=state.config.max_body_bytes)

    from fastapi.staticfiles import StaticFiles

    from agenthub.hub import dashboard

    app.include_router(dashboard.build_router())
    app.mount("/ui/static", StaticFiles(directory=dashboard.HERE / "static"), name="static")

    @app.exception_handler(store.StoreError)
    async def store_error(request: Request, exc: store.StoreError):
        status = {store.NotFound: 404, store.Conflict: 409, store.Invalid: 422, store.Forbidden: 403}
        if request.url.path.startswith("/ui"):
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

    # --- messages ------------------------------------------------------------

    @app.post(f"{api}/projects/{{project}}/messages", status_code=201)
    def post_message(project: str, body: PostMessageRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        require_project(caller, project)
        with db.transaction(conn):
            return store.post_message(
                conn, author=caller.agent, project=project, channel=body.channel,
                body=body.body, to=body.to, device=caller.device.name,
            )

    @app.get(f"{api}/projects/{{project}}/messages")
    def recent_messages(
        project: str, caller: Caller, conn: Conn,
        channel: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        require_project(caller, project)
        return {"messages": store.recent_messages(conn, project=project, channel=channel, limit=limit)}

    @app.get(f"{api}/inbox")
    def inbox(
        caller: Caller, conn: Conn,
        project: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        peek: bool = False,
        addressed_only: bool = False,
    ) -> dict[str, Any]:
        """Unread private messages from the operator first, then the caller's device board (both
        whatever ``project`` is), then channel messages; all of them count toward ``limit``."""
        projects = [require_project(caller, project)] if project else list(caller.agent.allowed_projects)
        mark = not (peek or addressed_only)
        agent = caller.agent
        with db.transaction(conn):
            mine = private.unread_for_agent(conn, agent, mark_read=mark, limit=limit)
            room = limit - len(mine)
            near = device_board.unread_for_agent(conn, agent, mark_read=mark, limit=room,
                                                 addressed_only=addressed_only)
            got = store.read_inbox(conn, agent, projects=projects, limit=room - len(near),
                                   mark_read=not peek, addressed_only=addressed_only)
            more = got["more"]
            # Anything still unread beyond what was just returned? (Marked rows are no longer unread.)
            if len(mine) == limit:
                left = private.unread_for_agent(conn, agent, mark_read=False, limit=limit + 1)
                more = more or len(left) > (0 if mark else limit)
            if len(near) == room:
                seen = 0 if mark else len(near)
                left = device_board.unread_for_agent(conn, agent, mark_read=False, limit=seen + 1,
                                                     addressed_only=addressed_only)
                more = more or len(left) > seen
        entries = [{"id": m["id"], "kind": "private", "project": None, "channel": "private", "author": m["author"],
                    "author_name": None, "to_agent": agent.id, "body": m["body"], "ts": m["ts"],
                    "addressed_to_me": True} for m in mine]
        entries += [{"id": m["id"], "kind": "device", "project": None, "channel": "device", "author": m["author"],
                     "author_name": m["author_name"], "to_agent": m["to_agent"], "body": m["body"], "ts": m["ts"],
                     "addressed_to_me": m["addressed_to_me"]} for m in near]
        return {"messages": entries + got["messages"], "more": more}

    @app.post(f"{api}/private", status_code=201)
    def message_operator(body: PrivateMessageRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """Write to the operator in the caller's own private chat."""
        with db.transaction(conn):
            return private.send(conn, caller.agent.id, author=caller.agent.id, body=body.body)

    @app.post(f"{api}/device/messages", status_code=201)
    def post_device(body: DeviceMessageRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        """Write on the board of the caller's own machine (the device it enrolled from)."""
        with db.transaction(conn):
            return device_board.post(conn, caller.agent.device_node_id, author=caller.agent.id, body=body.body,
                                     to=body.to, device=caller.device.name)

    # --- questions to the operator -------------------------------------------

    @app.post(f"{api}/projects/{{project}}/questions", status_code=201)
    def ask_operator(project: str, body: QuestionRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        require_project(caller, project)
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

    # --- prompts from the operator (agents act on their own; none can create one) ---

    @app.get(f"{api}/prompts/pending")
    def pending_prompts(caller: Caller, conn: Conn) -> dict[str, Any]:
        return {"prompts": prompts.pending_for(conn, caller.agent)}

    @app.get(f"{api}/prompts/{{prompt_id}}")
    def get_prompt(prompt_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        return prompts.get_for_agent(conn, prompt_id, caller.agent)

    @app.post(f"{api}/prompts/{{prompt_id}}/start")
    def start_prompt(prompt_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return prompts.start(conn, prompt_id, caller.agent)

    @app.post(f"{api}/prompts/{{prompt_id}}/finish")
    def finish_prompt(prompt_id: int, body: PromptFinishRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return prompts.finish(conn, prompt_id, caller.agent, ok=body.ok, note=body.note)

    @app.post(f"{api}/prompts/{{prompt_id}}/claim", status_code=201)
    def claim_share(prompt_id: int, body: PromptClaimRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return prompts.claim(conn, prompt_id, caller.agent, title=body.title, body=body.body,
                                 project=body.project)

    @app.post(f"{api}/prompts/{{prompt_id}}/decline")
    def decline_share(prompt_id: int, body: PromptDeclineRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return prompts.decline(conn, prompt_id, caller.agent, reason=body.reason)

    # --- board ---------------------------------------------------------------

    @app.get(f"{api}/projects/{{project}}/board")
    def board_list(project: str, caller: Caller, conn: Conn, prefix: str = "") -> dict[str, Any]:
        require_project(caller, project)
        return {"keys": store.board_list(conn, project, prefix[:200])}

    @app.get(f"{api}/projects/{{project}}/board/{{key:path}}")
    def board_get(project: str, key: str, caller: Caller, conn: Conn) -> dict[str, Any]:
        require_project(caller, project)
        return store.board_get(conn, project, key)

    @app.put(f"{api}/projects/{{project}}/board/{{key:path}}")
    def board_put(project: str, key: str, body: BoardPutRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        require_project(caller, project)
        with db.transaction(conn):
            return store.board_put(
                conn, actor=caller.agent.actor, project=project, key=key, value=body.value,
                expected_version=body.expected_version, device=caller.device.name,
            )

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
        require_project(caller, project)
        member_id = caller.agent.id if mine else None
        return {"tasks": tasks.list_tasks(conn, projects=[project], state=state, agent=member_id)}

    @app.post(f"{api}/projects/{{project}}/tasks", status_code=201)
    def create_task(project: str, body: CreateTaskRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        require_project(caller, project)
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
            return tasks.end_run(conn, task_id, run_id, caller.agent, outcome=body.outcome,
                                 session_id=body.session_id, num_turns=body.num_turns, cost_usd=body.cost_usd,
                                 device=caller.device.name)

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
        require_project(caller, project)
        with db.transaction(conn):
            return intents.announce(conn, caller.agent, project=project, patterns=body.patterns,
                                    description=body.description, ttl_minutes=body.ttl_minutes,
                                    device=caller.device.name)

    @app.get(f"{api}/projects/{{project}}/intents")
    def list_intents(project: str, caller: Caller, conn: Conn) -> dict[str, Any]:
        require_project(caller, project)
        return {"intents": intents.active(conn, project)}

    @app.post(f"{api}/intents/{{intent_id}}/clear")
    def clear_intent(intent_id: int, caller: Caller, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return intents.clear(conn, caller.agent, intent_id, device=caller.device.name)

    # --- asks ----------------------------------------------------------------

    @app.post(f"{api}/projects/{{project}}/asks", status_code=201)
    def create_ask(project: str, body: AskRequest, caller: Caller, conn: Conn) -> dict[str, Any]:
        require_project(caller, project)
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

    def _reported_dirs(message: dict[str, Any]) -> tuple[str, str | None] | None:
        """The runner's reported checkouts (compact JSON) and current directory, or None if the frame has none
        (an older runner's plain heartbeat) or anything invalid (then neither changes)."""
        if "checkouts" not in message and "current_dir" not in message:
            return None
        checkouts, current = message.get("checkouts", {}), message.get("current_dir")
        valid = (
            isinstance(checkouts, dict) and len(checkouts) <= 50
            and all(isinstance(k, str) and store.NAME_RE.fullmatch(k) and isinstance(v, str) and len(v) <= 500
                    for k, v in checkouts.items())
            and (current is None or (isinstance(current, str) and len(current) <= 500))
        )
        return (json.dumps(checkouts, separators=(",", ":")), current) if valid else None

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
            dirs = _reported_dirs(message)
            if dirs is not None and not conn.execute(
                    "SELECT 1 FROM agents WHERE id = ? AND checkouts IS ? AND current_dir IS ?",
                    (agent_id, *dirs)).fetchone():
                conn.execute("UPDATE agents SET last_heartbeat = ?, checkouts = ?, current_dir = ? WHERE id = ?",
                             (db.now_iso(), *dirs, agent_id))
            else:
                conn.execute("UPDATE agents SET last_heartbeat = ? WHERE id = ?", (db.now_iso(), agent_id))
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
        return {"results": events.search(conn, q, projects=projects, limit=limit)}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/ui"):
            return dashboard.error_page(exc.status_code, str(exc.detail))
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/ui"):
            return dashboard.error_page(422, "; ".join(str(e.get("msg")) for e in exc.errors()))
        return JSONResponse({"detail": jsonable(exc.errors())}, status_code=422)

    return app
