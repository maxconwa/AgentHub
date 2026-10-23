"""Direct agent-to-agent questions with a timeout.

An ask is delivered as a message addressed to the target in the project's
chat (``meta.to``, ``meta.ask_id``), so the inbox, the Stop hook, and the runner's WebSocket all
pick it up without special cases. The answer is posted back the same way, so
an asker whose ``ask_agent`` call timed out still receives it. (Before 0.3.8 both went to the
project's ``asks`` channel.)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthub.hub import chats, events, store
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound

ASK_CHANNEL = "asks"
MIN_TIMEOUT, MAX_TIMEOUT = 10, 3600

_SELECT = (
    "SELECT k.*, f.name AS from_name, t.name AS to_name FROM asks k "
    "LEFT JOIN agents f ON f.id = k.from_agent LEFT JOIN agents t ON t.id = k.to_agent "
)


def _expire(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    conn.execute(
        "UPDATE asks SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?",
        (now_iso(now),),
    )


def get(conn: sqlite3.Connection, ask_id: int, *, viewer: Agent) -> dict[str, Any]:
    row = conn.execute(_SELECT + "WHERE k.id = ?", (ask_id,)).fetchone()
    if row is None or viewer.id not in (row["from_agent"], row["to_agent"]):
        raise NotFound(f"no ask {ask_id}")
    out = dict(row)
    if out["status"] == "pending" and out["expires_at"] <= now_iso():
        out["status"] = "expired"
    return out


def create(
    conn: sqlite3.Connection,
    asker: Agent,
    *,
    project: str,
    to: str,
    question: str,
    timeout_seconds: int,
    device: str | None = None,
) -> dict[str, Any]:
    if not MIN_TIMEOUT <= timeout_seconds <= MAX_TIMEOUT:
        raise Invalid(f"timeout_seconds must be in [{MIN_TIMEOUT}, {MAX_TIMEOUT}]")
    target = store.find_agent(conn, to)
    if target.id == asker.id:
        raise Invalid("cannot ask yourself")
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=timeout_seconds)
    cur = conn.execute(
        "INSERT INTO asks (project, from_agent, to_agent, question, status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (project, asker.id, target.id, question, now_iso(now), now_iso(expires)),
    )
    ask_id = cur.lastrowid
    # chats.post validates that the target is an active member of the project.
    chats.post(
        conn, asker, "project", project, to=target.id, device=device, meta={"ask_id": ask_id},
        max_chars=chats.MAX_BODY_CHARS + 200,
        body=f"[ask #{ask_id}] {question}\n\n(Answer with the answer_ask tool, ask_id={ask_id}, "
             f"before {now_iso(expires)}.)",
    )
    events.emit(
        conn, type="ask.created", actor=asker.id, project=project, device=device,
        payload={"ask_id": ask_id, "to": target.id, "question": question, "expires_at": now_iso(expires)},
    )
    return get(conn, ask_id, viewer=asker)


def answer(
    conn: sqlite3.Connection, responder: Agent, ask_id: int, text: str, *, device: str | None = None
) -> dict[str, Any]:
    ask = get(conn, ask_id, viewer=responder)
    if ask["to_agent"] != responder.id:
        raise Forbidden("only the agent that was asked can answer")
    if ask["status"] == "expired":
        _expire(conn)
        raise Conflict(f"ask {ask_id} expired at {ask['expires_at']}")
    cur = conn.execute(
        "UPDATE asks SET status = 'answered', answer = ?, answered_at = ? WHERE id = ? AND status = 'pending'",
        (text, now_iso(), ask_id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"ask {ask_id} is already {ask['status']}")
    asker = store.get_agent(conn, ask["from_agent"])
    if asker is not None and asker.status == "active" and ask["project"] in asker.allowed_projects:
        chats.post(conn, responder, "project", ask["project"], to=asker.id, device=device,
                   meta={"ask_id": ask_id}, max_chars=chats.MAX_BODY_CHARS + 200,
                   body=f"[answer to ask #{ask_id}] {text}")
    events.emit(
        conn, type="ask.answered", actor=responder.id, project=ask["project"], device=device,
        payload={"ask_id": ask_id, "to": ask["from_agent"], "answer": text},
    )
    return get(conn, ask_id, viewer=responder)


def pending_for(conn: sqlite3.Connection, agent: Agent) -> list[dict[str, Any]]:
    rows = conn.execute(
        _SELECT + "WHERE k.to_agent = ? AND k.status = 'pending' AND k.expires_at > ? ORDER BY k.id",
        (agent.id, now_iso()),
    ).fetchall()
    return [dict(r) for r in rows]
