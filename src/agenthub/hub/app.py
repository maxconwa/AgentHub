"""The hub's FastAPI application (agent-facing REST API, v1)."""

from __future__ import annotations

import ipaddress
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agenthub import __version__
from agenthub.config import HubConfig
from agenthub.hub import db, events, store
from agenthub.hub.auth import AgentCaller, active_agent, any_agent, get_conn, require_project, resolve_device
from agenthub.hub.limits import BodySizeLimit
from agenthub.hub.tailscale import TailscaleWhois, Whois

MAX_MESSAGE_CHARS = 16_000
MAX_BOARD_VALUE_CHARS = 32_000


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

    def __post_init__(self) -> None:
        if self.whois is None:
            self.whois = TailscaleWhois(self.config.tailscale_bin, self.config.whois_cache_seconds)

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.config.db_path)


# --- request models -----------------------------------------------------------


class EnrollRequest(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    name: str = Field(min_length=1, max_length=64)


class PostMessageRequest(BaseModel):
    channel: str = Field(default="general", max_length=64)
    body: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    to: str | None = Field(default=None, max_length=64)


class BoardPutRequest(BaseModel):
    value: str = Field(max_length=MAX_BOARD_VALUE_CHARS)
    expected_version: int = Field(ge=0)


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Caller = Annotated[AgentCaller, Depends(active_agent)]


def create_app(state: HubState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.init_db(state.config.db_path)
        conn.close()
        yield

    app = FastAPI(title="agenthub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.hub = state
    app.add_middleware(BodySizeLimit, max_bytes=state.config.max_body_bytes)

    @app.exception_handler(store.StoreError)
    async def store_error(request: Request, exc: store.StoreError) -> JSONResponse:
        status = {store.NotFound: 404, store.Conflict: 409, store.Invalid: 422, store.Forbidden: 403}
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
        device = await resolve_device(request)
        with db.transaction(conn):
            agent, token = store.redeem_enrollment(conn, code=body.code, name=body.name, device=device)
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
    ) -> dict[str, Any]:
        projects = [require_project(caller, project)] if project else list(caller.agent.allowed_projects)
        with db.transaction(conn):
            return store.read_inbox(conn, caller.agent, projects=projects, limit=limit, mark_read=not peek)

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
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app
