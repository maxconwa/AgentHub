"""Device boards: notes shared by the agents on one machine and the operator.

A machine is its tailscale StableID (``agents.device_node_id``, verified by the
hub at enrollment and on every request). Its board is visible only to the
agents on it and to the operator. Messages live in ``device_messages``, never in
project channels; their ``device.message_posted`` events carry ids only and no
project, so the event log's full-text index never holds the text and agents'
project-scoped searches never match them.

An author is ``human:<login>`` (the operator) or an active agent on that
machine; the API derives both the author and the machine from the caller's
token, so an agent writes only on its own machine's board.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from agenthub.hub import events
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Forbidden, Invalid, NotFound, find_agent

MAX_BODY_CHARS = 4000

_SELECT = (
    "SELECT d.id, d.node_id, d.author, a.name AS author_name, d.to_agent, d.body, d.ts "
    "FROM device_messages d LEFT JOIN agents a ON a.id = d.author "
)


def _agents_on(conn: sqlite3.Connection, node_id: str) -> dict[str, str]:
    """Agent id -> status for the non-revoked agents on ``node_id``."""
    return {r["id"]: r["status"] for r in conn.execute(
        "SELECT id, status FROM agents WHERE device_node_id = ? AND status != 'revoked'", (node_id,))}


def post(
    conn: sqlite3.Connection, node_id: str, *, author: str, body: str, to: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """Add a note to ``node_id``'s board, from the operator or an active agent on that machine.

    ``to`` (an agent name or id) addresses one active agent on the machine. Raises NotFound
    for a machine with no non-revoked agent.
    """
    if not body.strip() or len(body) > MAX_BODY_CHARS:
        raise Invalid(f"a device board note must be 1 to {MAX_BODY_CHARS} characters")
    on_device = _agents_on(conn, node_id)
    if not on_device:
        raise NotFound(f"no device {node_id!r}")
    if not author.startswith("human:") and on_device.get(author) != "active":
        raise Forbidden("an agent may only post to its own device's board")
    to_agent = None
    if to is not None:
        try:
            to_agent = find_agent(conn, to).id
        except NotFound:
            to_agent = None
        if to_agent is None or on_device.get(to_agent) != "active":
            raise Invalid(f"agent {to!r} is not an active agent on this device")
    ts = now_iso()
    cur = conn.execute("INSERT INTO device_messages (node_id, author, to_agent, body, ts) VALUES (?, ?, ?, ?, ?)",
                       (node_id, author, to_agent, body, ts))
    events.emit(conn, type="device.message_posted", actor=author, device=device,
                payload={"message_id": cur.lastrowid, "node_id": node_id, "to": to_agent})
    return {"id": cur.lastrowid, "node_id": node_id, "author": author, "to": to_agent, "ts": ts}


def recent(conn: sqlite3.Connection, node_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """The newest ``limit`` notes on the board, oldest first."""
    rows = conn.execute(_SELECT + "WHERE d.node_id = ? ORDER BY d.id DESC LIMIT ?", (node_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def unread_for_agent(
    conn: sqlite3.Connection, agent: Agent, *, mark_read: bool, limit: int | None = None,
    addressed_only: bool = False,
) -> list[dict[str, Any]]:
    """Notes on ``agent``'s own machine it hasn't read, oldest first (at most ``limit``).

    Its own notes and notes addressed to another agent are left out; ``addressed_only``
    keeps only those addressed to it. ``mark_read`` advances its cursor past what is returned.
    """
    addressed = "d.to_agent = ?" if addressed_only else "(d.to_agent IS NULL OR d.to_agent = ?)"
    sql = (_SELECT + f"WHERE d.node_id = ? AND d.author != ? AND {addressed} "
           "AND d.id > COALESCE((SELECT last_read_id FROM device_cursors WHERE agent_id = ?), 0) ORDER BY d.id")
    params: list[Any] = [agent.device_node_id, agent.id, agent.id, agent.id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = [dict(r) | {"addressed_to_me": r["to_agent"] == agent.id} for r in conn.execute(sql, params)]
    if mark_read and rows:
        conn.execute("INSERT INTO device_cursors (agent_id, last_read_id) VALUES (?, ?) ON CONFLICT (agent_id) "
                     "DO UPDATE SET last_read_id = MAX(last_read_id, excluded.last_read_id)",
                     (agent.id, rows[-1]["id"]))
    return rows


# How far back a board list looks for the faces of who posted last (the list shows three).
RECENT_AUTHORS_WINDOW = 30


def last_posts(conn: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Node id -> ``{ts, authors}`` for each of ``node_ids`` whose board has notes: the newest note's time and
    the authors of its latest notes, newest first. One short index range per node, so a poll stays cheap."""
    out: dict[str, dict[str, Any]] = {}
    for node_id in dict.fromkeys(node_ids):
        rows = conn.execute("SELECT author, ts FROM device_messages WHERE node_id = ? ORDER BY id DESC LIMIT ?",
                            (node_id, RECENT_AUTHORS_WINDOW)).fetchall()
        if rows:
            out[node_id] = {"ts": rows[0]["ts"], "authors": list(dict.fromkeys(r["author"] for r in rows))}
    return out


def device_labels(conn: sqlite3.Connection) -> dict[str, str]:
    """``device:<node_id>`` -> that machine's name (from the agent that heartbeat most recently).

    Merged into the agent-name maps that event summaries use, so a ``device.message_posted``
    line can name the machine; the prefix keeps these keys apart from agent ids.
    """
    out: dict[str, str] = {}
    for r in conn.execute("SELECT device_node_id, device_name FROM agents "
                          "ORDER BY COALESCE(last_heartbeat, ''), created_at"):
        out[f"device:{r['device_node_id']}"] = r["device_name"]
    return out
