"""Agent authentication for every agent-facing endpoint.

Order of checks:
  1. Bearer token is well-formed and names an existing agent.
  2. Token matches the stored hash (constant-time).
  3. Agent is not revoked (and is active, unless the route allows pending).
  4. Outside --insecure-dev: the request came through tailscale serve and
     ``tailscale whois`` on the caller's tailnet IP reports the same node
     (StableID) that was recorded at enrollment.

Failures before the agent is identified return 401 with a uniform message,
so callers cannot probe which agent ids exist.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from starlette.requests import HTTPConnection

from agenthub import security
from agenthub.hub import store
from agenthub.hub.store import Agent, Device
from agenthub.hub.tailscale import DEV_DEVICE, IdentityError, tailnet_client_ip

_UNAUTHORIZED = HTTPException(401, "invalid or missing agent token", headers={"WWW-Authenticate": "Bearer"})
# Compared against when the agent id is unknown, so timing does not reveal it.
_DUMMY_HASH = security.hash_token("ahk_none_none")


@dataclass(frozen=True)
class AgentCaller:
    agent: Agent
    device: Device


def get_conn(request: Request):
    conn = request.app.state.hub.connect()
    try:
        yield conn
    finally:
        conn.close()


async def resolve_device(request: HTTPConnection) -> Device:
    """The caller's tailnet device (or a fixed placeholder in --insecure-dev)."""
    hub = request.app.state.hub
    if hub.insecure_dev:
        return DEV_DEVICE
    peer = request.client.host if request.client else None
    try:
        ip = tailnet_client_ip(peer, request.headers)
        return (await hub.whois.lookup(ip)).device
    except IdentityError as exc:
        raise HTTPException(403, f"tailnet identity check failed: {exc}") from exc


def _bearer(request: HTTPConnection) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTHORIZED
    return token.strip()


async def authenticate(request: HTTPConnection, conn: sqlite3.Connection, allow_pending: bool) -> AgentCaller:
    """Shared by HTTP routes and the WebSocket endpoint."""
    token = _bearer(request)
    parsed = security.parse_token(token)
    agent = store.get_agent(conn, parsed[0]) if parsed else None
    if not security.verify_token(token, agent.token_hash if agent else _DUMMY_HASH) or agent is None:
        raise _UNAUTHORIZED
    if agent.status == "revoked":
        raise _UNAUTHORIZED
    request.app.state.hub.agent_limiter.check(agent.id)
    if agent.status != "active" and not allow_pending:
        raise HTTPException(403, f"agent is {agent.status}; an operator must approve it first")
    device = await resolve_device(request)
    if not request.app.state.hub.insecure_dev and device.node_id != agent.device_node_id:
        raise HTTPException(403, "token is bound to a different tailnet device")
    return AgentCaller(agent=agent, device=device)


async def active_agent(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> AgentCaller:
    return await authenticate(request, conn, allow_pending=False)


async def any_agent(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> AgentCaller:
    """Pending agents may only learn their own status."""
    return await authenticate(request, conn, allow_pending=True)


def require_project(caller: AgentCaller, project: str) -> str:
    """Agents may only touch projects the operator granted them. The project's name as stored (names ignore case)."""
    found = caller.agent.project(project)
    if found is None:
        raise HTTPException(403, f"agent has no access to project {project!r}")
    return found
