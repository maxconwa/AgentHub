"""Private chats: the operator and one agent.

Messages live in ``private_messages``, never in project channels, so no other
agent can reach them through its inbox, boards, or channel history. Their
``private.sent`` events carry ids only and no project: the event log's
full-text index never holds the text, and project-scoped event queries
(agent search) never match them.

An author is ``human:<login>`` (the operator) or the agent's own id; the API
derives it from the caller's token, so an agent writes only in its own chat.

A prompt's copy (``prompt_id`` set, stored already read) shows the prompt in the
chat but is never delivered to the agent as unread mail: its prompt session acts
on it, and a busy agent's running session must not pick it up early from the inbox.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from agenthub.hub import events, limits
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Forbidden, Invalid, NotFound, get_agent

MAX_BODY_CHARS = limits.AGENT_TEXT_CHARS  # an agent's message; the operator's have no limit

_SELECT = (
    "SELECT p.id, p.agent_id, p.author, a.name AS author_name, p.body, p.ts, p.read_at, p.prompt_id "
    "FROM private_messages p LEFT JOIN agents a ON a.id = p.author "
)


def send(
    conn: sqlite3.Connection, agent_id: str, *, author: str, body: str, prompt_id: int | None = None
) -> dict[str, Any]:
    """Add a message to ``agent_id``'s chat, from the operator or from that agent; ``prompt_id`` marks the
    operator's copy of that prompt (shown in the chat, never unread mail)."""
    if not body.strip() or (not author.startswith("human:") and len(body) > MAX_BODY_CHARS):
        raise Invalid(f"a private message must be 1 to {MAX_BODY_CHARS} characters")
    agent = get_agent(conn, agent_id)
    if agent is None:
        raise NotFound(f"no agent {agent_id!r}")
    if agent.status == "revoked":
        raise Forbidden(f"agent {agent.name!r} is revoked; its chat is read-only")
    if author.startswith("human:"):
        sender = "operator"
    elif author == agent.id:
        sender = "agent"
    else:
        raise Forbidden("an agent may only write in its own private chat")
    ts = now_iso()
    if prompt_id is not None and sender != "operator":
        raise Forbidden("only the operator's messages can carry a prompt")
    # A prompt copy is stored read as well, so a hub rolled back to v0.1.0 (which knows no prompt_id) doesn't
    # deliver it as mail either.
    cur = conn.execute("INSERT INTO private_messages (agent_id, author, body, ts, prompt_id, read_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (agent.id, author, body, ts, prompt_id, ts if prompt_id else None))
    events.emit(conn, type="private.sent", actor=author,
                payload={"message_id": cur.lastrowid, "agent_id": agent.id, "from": sender})
    return {"id": cur.lastrowid, "agent_id": agent.id, "author": author, "body": body, "ts": ts,
            "prompt_id": prompt_id}


def unread_for_agent(
    conn: sqlite3.Connection, agent: Agent, *, mark_read: bool, limit: int | None = None
) -> list[dict[str, Any]]:
    """The operator's messages to ``agent`` it hasn't read yet, oldest first (at most ``limit``); prompt copies
    never count."""
    sql = (_SELECT + "WHERE p.agent_id = ? AND p.author LIKE 'human:%' AND p.read_at IS NULL "
           "AND p.prompt_id IS NULL ORDER BY p.id")
    params: list[Any] = [agent.id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params)]
    if mark_read and rows:
        ids = [r["id"] for r in rows]
        conn.execute(f"UPDATE private_messages SET read_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                     [now_iso(), *ids])
    return rows


def thread(conn: sqlite3.Connection, agent_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """The newest ``limit`` messages of the chat, oldest first."""
    rows = conn.execute(_SELECT + "WHERE p.agent_id = ? ORDER BY p.id DESC LIMIT ?", (agent_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def mark_read_by_operator(conn: sqlite3.Connection, agent_id: str, *, since_id: int = 0) -> None:
    """Mark the agent's messages (those with id >= ``since_id``) read by the operator."""
    conn.execute("UPDATE private_messages SET read_at = ? WHERE agent_id = ? AND author = agent_id "
                 "AND read_at IS NULL AND id >= ?", (now_iso(), agent_id, since_id))


def unread_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Agent id -> messages that non-revoked agent wrote that the operator hasn't read."""
    rows = conn.execute(
        "SELECT p.agent_id, COUNT(*) AS n FROM private_messages p JOIN agents a ON a.id = p.agent_id "
        "WHERE p.author = p.agent_id AND p.read_at IS NULL AND a.status != 'revoked' GROUP BY p.agent_id")
    return {r["agent_id"]: r["n"] for r in rows}


def body(conn: sqlite3.Connection, message_id: int) -> str | None:
    row = conn.execute("SELECT body FROM private_messages WHERE id = ?", (message_id,)).fetchone()
    return row["body"] if row else None
