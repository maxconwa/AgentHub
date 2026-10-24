"""The operator's message to every agent on a device (0.3.10).

One message in the device's chat (``device:<node_id>``, :mod:`chats`), delivered to every active agent on that machine:
one ``chat_deliveries`` row each, which its runner takes into that agent's own chat session (agents on one machine can
be in different projects, so no project or task session is the place for it). It wakes an idle agent the way the
operator's messages do, except that it respects the pause switches (the agent's and the hub's, :mod:`wakes`): a paused
agent still gets it, into a running session or through read_inbox, and it wakes the agent once the pause lifts.

Only the operator sends one (the route is under ``/ui/api``: Tailscale identity, CSRF, never an agent token). Agents
have no tool for it and keep their device board (``post_device``) as it was, so this never widens what an agent can
reach. The agents on the machine can read its chat (``chat_history(chat='device')``); nobody else can, and its text is
never in the full-text index (the chat has no project).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agenthub.hub import chats, db, views, wakes
from agenthub.hub.dashboard import Conn, Op, PostOp  # the dashboard's operator checks
from agenthub.hub.store import Conflict, Forbidden

# Why an agent on the device wasn't woken (None: it was). ``delivered`` is False only for the last two.
REASONS = ("offline", "pending_runner", "paused", "busy_queued", "pending", "revoked")


def _reason(conn: sqlite3.Connection, agent: sqlite3.Row) -> str | None:
    """Why the delivery to active ``agent`` starts no session now, or None if its runner wakes it."""
    if not agent["connected"]:
        return "offline"  # it waits for the runner to connect
    if not agent["chat_support"]:
        return "pending_runner"  # a runner before 0.3.8 takes no chat messages: it waits for an upgrade
    if not wakes.may_wake(conn, agent["id"], None):
        return "paused"  # into a running session, or read_inbox; it wakes when the pause lifts
    if wakes.awake(conn, agent["id"]):
        return "busy_queued"  # into its running session, or the next turn
    return None


def send(conn: sqlite3.Connection, node_id: str, body: str, *, actor: str) -> dict[str, Any]:
    """The operator writes to every agent on device ``node_id`` (call it inside a transaction).

    Returns ``{message_id, chat_id, device: {node_id, name}, recipients: [{agent_id, name, delivered, woke, delivery_id,
    reason}], sent, woke}``: every agent on the machine, pending and revoked ones too (``delivered`` False, so the
    operator sees why), in enrollment order. ``woke`` is what the hub knows when it sends: the agent's runner is
    connected and may wake it, and it is idle. NotFound for a device with no agent that isn't revoked, Conflict for one
    with no active agent, Invalid for an empty message, Forbidden for anyone but the operator."""
    if not actor.startswith("human:"):
        raise Forbidden("only a human operator can message every agent on a device")
    chats._check_body(body, None)
    device = views.device(conn, node_id)  # NotFound
    agents = conn.execute("SELECT id, name, status, connected, chat_support FROM agents WHERE device_node_id = ? "
                          "ORDER BY created_at, id", (node_id,)).fetchall()
    if not any(a["status"] == "active" for a in agents):
        raise Conflict(f"no active agent on device {device['name']} to message")
    message = chats.send(conn, "device", node_id, body, actor)
    delivered = {d["agent_id"]: d["id"] for d in conn.execute(
        "SELECT id, agent_id FROM chat_deliveries WHERE message_id = ?", (message["id"],))}
    recipients = []
    for a in agents:
        if a["id"] in delivered:
            reason = _reason(conn, a)
            recipients.append({"agent_id": a["id"], "name": a["name"], "delivered": True, "woke": reason is None,
                               "delivery_id": delivered[a["id"]], "reason": reason})
        else:
            recipients.append({"agent_id": a["id"], "name": a["name"], "delivered": False, "woke": False,
                               "delivery_id": None, "reason": a["status"]})
    return {"message_id": message["id"], "chat_id": message["chat_id"],
            "device": {"node_id": node_id, "name": device["name"]}, "recipients": recipients,
            "sent": len(delivered), "woke": sum(r["woke"] for r in recipients)}


def recent(conn: sqlite3.Connection, node_id: str, limit: int = 50) -> dict[str, Any]:
    """Device ``node_id``'s chat for the operator: the latest ``limit`` rows, oldest first; the operator's messages
    carry their ``deliveries`` (to whom, and how: ``live``, ``queued``, ``woke``, ``inbox``, ``failed``, or still
    pending). NotFound for an unknown device."""
    device = views.device(conn, node_id)
    c = chats.chat(conn, "device", node_id)
    rows = chats.messages(conn, "device", node_id, limit=limit)
    return {"chat_id": c["id"], "device": {"node_id": node_id, "name": device["name"]}, "members": c["members"],
            "messages": rows}


# --- operator routes ---------------------------------------------------------------------------------------------


class BroadcastBody(BaseModel):
    body: str = Field(min_length=1)


def build_router() -> APIRouter:
    """Operator-only JSON for the dashboard, under /ui so the dashboard's rules cover it (Tailscale identity, never
    an agent token, CSRF on writes, the agents' guard hook)."""
    router = APIRouter(prefix="/ui/api")

    @router.post("/devices/{node_id}/broadcast")
    def post_broadcast(node_id: str, body: BroadcastBody, operator: PostOp, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return send(conn, node_id, body.body, actor=operator.actor)

    @router.get("/devices/{node_id}/broadcast")
    def get_broadcasts(node_id: str, operator: Op, conn: Conn, limit: int = 50) -> dict[str, Any]:
        return recent(conn, node_id, limit=max(1, min(limit, 300)))

    return router
