"""The append-only event log and its full-text index.

Every state change in the hub calls :func:`emit` inside the same transaction
as the change itself, so the log and the current-state tables never diverge.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from agenthub.hub.db import now_iso

# Upper bound on how much of a payload is copied into the FTS index.
_MAX_INDEXED_CHARS = 20_000
# An agent's real progress, or its answer to a request: the wake chain it works in has landed (wakes.LANDING_EVENTS).
LANDING_EVENTS = frozenset({"task.created", "task.transitioned", "task.agent_finished", "task.verdict",
                            "chat.declined"})


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def emit(
    conn: sqlite3.Connection,
    *,
    type: str,
    actor: str,
    project: str | None = None,
    device: str | None = None,
    payload: dict[str, Any] | None = None,
    index_text: str | None = None,
) -> int:
    """Append an event. ``index_text`` goes into the full-text index only, not the payload (a chat message's text,
    whose event carries ids only)."""
    payload = payload or {}
    cur = conn.execute(
        "INSERT INTO events (ts, project, actor, device, type, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso(), project, actor, device, type, json.dumps(payload, separators=(",", ":"))),
    )
    event_id = cur.lastrowid
    text = " ".join([type, actor, *_strings(payload), *([index_text] if index_text else [])])[:_MAX_INDEXED_CHARS]
    conn.execute("INSERT INTO events_fts (rowid, text) VALUES (?, ?)", (event_id, text))
    if type in LANDING_EVENTS and not actor.startswith(("human:", "system:")):
        # an agent's progress lands the wake chain it works in (0.3.9.1)
        from agenthub.hub import wakes

        wakes.land(conn, actor)
    return event_id


def row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "project": row["project"],
        "actor": row["actor"],
        "device": row["device"],
        "type": row["type"],
        "payload": json.loads(row["payload"]),
    }


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def fts_query(user_query: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word becomes a quoted term.

    This prevents user input from being parsed as FTS5 syntax (column filters,
    NEAR, boolean operators), which could raise errors or change meaning.
    """
    terms = _TOKEN_RE.findall(user_query)[:20]
    if not terms:
        return None
    return " ".join(f'"{t}"' for t in terms)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    projects: list[str] | None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search events. ``projects=None`` means unrestricted (operator only)."""
    match = fts_query(query)
    if match is None:
        return []
    sql = (
        "SELECT e.* FROM events_fts f JOIN events e ON e.id = f.rowid "
        "WHERE events_fts MATCH ?"
    )
    params: list[Any] = [match]
    if projects is not None:
        if not projects:
            return []
        sql += f" AND e.project IN ({','.join('?' * len(projects))})"
        params.extend(projects)
    sql += " ORDER BY f.rank LIMIT ?"
    params.append(limit)
    return [row_to_event(r) for r in conn.execute(sql, params)]


def list_events(
    conn: sqlite3.Connection,
    *,
    projects: list[str] | None,
    after_id: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM events WHERE id > ?"
    params: list[Any] = [after_id]
    if projects is not None:
        if not projects:
            return []
        sql += f" AND project IN ({','.join('?' * len(projects))})"
        params.extend(projects)
    sql += " ORDER BY id LIMIT ?"
    params.append(limit)
    return [row_to_event(r) for r in conn.execute(sql, params)]
