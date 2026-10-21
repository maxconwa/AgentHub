"""Push events to connected agents over WebSockets.

The append-only ``events`` table is the bus. A single background task tails
it (every ``poll_seconds``), so changes made by any process (the hub itself or
the operator CLI writing to the same database) reach agents the same way.

Routing is deliberately narrow: an agent is only told about things it could
read anyway. Notifications carry ids and metadata, not bodies; clients fetch
content through the normal authenticated endpoints.

Revocation: an ``agent.revoked`` event closes that agent's sockets with code
4401. HTTP requests are already refused immediately because every request
re-checks the token in the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

from agenthub.hub.events import row_to_event

log = logging.getLogger("agenthub.bus")

CLOSE_REVOKED = 4401
CLOSE_FORBIDDEN = 4403
MAX_SOCKETS_PER_AGENT = 4


class Socket(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


def _notification(event: dict[str, Any], recipient: str) -> dict[str, Any]:
    p = event["payload"]
    keep = ("message_id", "channel", "task_id", "approval_id", "ask_id", "kind", "status", "from", "to",
            "state", "assignee", "expires_at", "key", "version", "conflict_id", "board_key")
    data = {k: p[k] for k in keep if k in p}
    if event["type"] == "message.posted":
        data["addressed_to_me"] = p.get("to") == recipient
    return {
        "type": "event",
        "event": {"id": event["id"], "ts": event["ts"], "type": event["type"], "project": event["project"],
                  "actor": event["actor"], "data": data},
    }


def recipients(event: dict[str, Any], conn: sqlite3.Connection) -> set[str]:
    """Which agents should hear about this event."""
    etype, p, project = event["type"], event["payload"], event["project"]
    if etype in ("message.posted", "board.updated", "channel.created"):
        rows = conn.execute("SELECT id, allowed_projects FROM agents WHERE status = 'active'").fetchall()
        return {r["id"] for r in rows if project in json.loads(r["allowed_projects"])} - {event["actor"]}
    if etype in ("task.created", "task.transitioned", "task.assigned"):
        return {p["assignee"]} if p.get("assignee") else set()
    if etype in ("approval.decided", "approval.discussed", "approval.consumed"):
        if "requester" in p:
            return {p["requester"]} - {event["actor"]}
        row = conn.execute("SELECT requester FROM approvals WHERE id = ?", (p.get("approval_id"),)).fetchone()
        return {row["requester"]} if row else set()
    if etype in ("ask.created", "ask.answered"):
        return {p["to"]}
    if etype == "intent.conflict":
        rows = conn.execute("SELECT agent_id FROM intents WHERE id IN (?, ?)", tuple(p["intents"])).fetchall()
        return {r["agent_id"] for r in rows}
    return set()


class EventBus:
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        poll_seconds: float = 0.5,
        *,
        render_for_operator: Callable[[dict[str, Any], dict[str, str]], str] | None = None,
        notifier: Any = None,
    ) -> None:
        self._connect = connect
        self.poll_seconds = poll_seconds
        self.render_for_operator = render_for_operator
        self.notifier = notifier
        self._sockets: dict[str, set[Socket]] = defaultdict(set)
        self._operators: set[Socket] = set()
        self._task: asyncio.Task | None = None
        self._last_id = 0

    # --- connection registry -------------------------------------------------

    def connected(self, agent_id: str) -> int:
        return len(self._sockets.get(agent_id, ()))

    def register(self, agent_id: str, socket: Socket) -> bool:
        if self.connected(agent_id) >= MAX_SOCKETS_PER_AGENT:
            return False
        self._sockets[agent_id].add(socket)
        return True

    def unregister(self, agent_id: str, socket: Socket) -> None:
        sockets = self._sockets.get(agent_id)
        if sockets is not None:
            sockets.discard(socket)
            if not sockets:
                del self._sockets[agent_id]

    def add_operator(self, socket: Socket) -> None:
        self._operators.add(socket)

    def remove_operator(self, socket: Socket) -> None:
        self._operators.discard(socket)

    async def close_agent(self, agent_id: str, code: int = CLOSE_REVOKED, reason: str = "revoked") -> None:
        for socket in list(self._sockets.pop(agent_id, ())):
            with contextlib.suppress(Exception):
                await socket.close(code=code, reason=reason)

    # --- tailing -------------------------------------------------------------

    def start(self) -> None:
        conn = self._connect()
        try:
            self._last_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        finally:
            conn.close()
        self._task = asyncio.create_task(self._run(), name="agenthub-event-bus")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for agent_id in list(self._sockets):
            await self.close_agent(agent_id, code=1001, reason="hub shutting down")
        for socket in list(self._operators):
            with contextlib.suppress(Exception):
                await socket.close(code=1001, reason="hub shutting down")

    async def _run(self) -> None:
        while True:
            try:
                await self.pump()
            except Exception:  # noqa: BLE001 - keep the bus alive
                log.exception("event bus iteration failed")
            await asyncio.sleep(self.poll_seconds)

    async def pump(self) -> int:
        """Deliver all events newer than the last one seen. Returns how many were processed."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT 500", (self._last_id,)).fetchall()
            deliveries: list[tuple[str, dict[str, Any]]] = []
            operator_rows: list[dict[str, Any]] = []
            revoked: list[str] = []
            names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")} if rows else {}
            for row in rows:
                event = row_to_event(row)
                self._last_id = event["id"]
                if self.notifier is not None:
                    self.notifier.submit(event, names)
                if self._operators and self.render_for_operator is not None:
                    operator_rows.append({"type": "event", "id": event["id"], "event_type": event["type"],
                                          "project": event["project"], "actor": event["actor"],
                                          "html": self.render_for_operator(event, names)})
                if event["type"] == "agent.revoked":
                    revoked.append(event["payload"]["agent_id"])
                    continue
                if not self._sockets:
                    continue
                for agent_id in recipients(event, conn) & set(self._sockets):
                    deliveries.append((agent_id, _notification(event, agent_id)))
        finally:
            conn.close()
        for agent_id in revoked:
            await self.close_agent(agent_id)
        for agent_id, message in deliveries:
            await self.send(agent_id, message)
        for message in operator_rows:
            data = json.dumps(message)
            for socket in list(self._operators):
                try:
                    await socket.send_text(data)
                except Exception:  # noqa: BLE001
                    self._operators.discard(socket)
        return len(rows)

    async def send(self, agent_id: str, message: dict[str, Any]) -> None:
        data = json.dumps(message)
        for socket in list(self._sockets.get(agent_id, ())):
            try:
                await socket.send_text(data)
            except Exception:  # noqa: BLE001 - drop dead sockets
                self.unregister(agent_id, socket)
