"""Stalled reviews (0.3.11, task #51): a task left in review with nobody reviewing it.

Review tasks #39 and #44 sat in review for hours and nothing showed it. A task in ``review`` is flagged
``stalled_review`` once it has been there ``[reviews] stall_minutes`` (hub.toml, default 30; 0 turns it off) with no
verdict recorded and no reviewer session running:

* ``reason: "no_reviewer"``: it has no active reviewer, so no agent will review it (its project has no reviewers, or
  they are all on the task as implementers, as hub-reviewer was on #39);
* ``reason: "idle"``: it has reviewers, but none has a session running (a task run, or a session the task chat woke)
  and none recorded a verdict this round.

A verdict ends the stall either way: ``approve`` leaves the task with the operator, which is not a stall. The flag is
``{"since", "minutes", "reason"}``, ``since`` being when the task entered review. The dashboard shows it on task rows
(:mod:`views`) and ``agenthub doctor`` warns about it. It is read-only: nothing here wakes anyone or moves a task.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from agenthub.config import DEFAULT_REVIEW_STALL_MINUTES

# The states a status line says a session is running in (wakes.AWAKE_STATES; wakes imports the dashboard, this
# module is also used by `agenthub doctor`, so it doesn't import it).
AWAKE_STATES = ("started", "working", "queued", "approval_wait")

# Set from hub.toml by the app (``[reviews] stall_minutes``); 0 turns the flag off.
_minutes = DEFAULT_REVIEW_STALL_MINUTES


def configure(minutes: int) -> None:
    global _minutes
    _minutes = minutes


def stall_minutes() -> int:
    return _minutes


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def stalled_reviews(conn: sqlite3.Connection, *, minutes: int | None = None,
                    now: datetime | None = None) -> dict[int, dict[str, Any]]:
    """``{task_id: {since, minutes, reason}}`` for every stalled review (empty when the flag is off)."""
    limit = _minutes if minutes is None else minutes
    if limit <= 0:
        return {}
    now = now or datetime.now(UTC)
    in_review = {r["id"]: r["updated_at"]
                 for r in conn.execute("SELECT id, updated_at FROM tasks WHERE state = 'review'")}
    if not in_review:
        return {}
    marks = ",".join("?" * len(in_review))
    # When each entered review: its latest move there (a task's updated_at also moves on edits, so it's the fallback).
    since = {r["task_id"]: r["ts"] for r in conn.execute(
        "SELECT json_extract(payload, '$.task_id') AS task_id, MAX(ts) AS ts FROM events "
        "WHERE type = 'task.transitioned' AND json_extract(payload, '$.to') = 'review' "
        f"AND json_extract(payload, '$.task_id') IN ({marks}) GROUP BY 1", list(in_review))}
    reviewers: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute(
            "SELECT ta.task_id, ta.agent_id, ta.verdict, a.status FROM task_agents ta "
            f"JOIN agents a ON a.id = ta.agent_id WHERE ta.stage = 'reviews' AND ta.task_id IN ({marks})",
            list(in_review)):
        reviewers.setdefault(r["task_id"], []).append(r)
    out: dict[int, dict[str, Any]] = {}
    for task_id, updated_at in in_review.items():
        entered = since.get(task_id) or updated_at
        elapsed = (now - _parse(entered)).total_seconds() / 60
        if elapsed < limit:
            continue
        rows = reviewers.get(task_id, [])
        if any(r["verdict"] is not None for r in rows):
            continue
        active = [r["agent_id"] for r in rows if r["status"] == "active"]
        if active and _reviewing(conn, task_id, active):
            continue
        out[task_id] = {"since": entered, "minutes": int(elapsed), "reason": "idle" if active else "no_reviewer"}
    return out


def _reviewing(conn: sqlite3.Connection, task_id: int, agent_ids: list[str]) -> bool:
    """Whether one of the task's reviewers has a session running on it: a task run not yet ended, or a session its
    task chat woke (the reviewer's latest status line in that chat says it is running)."""
    marks = ",".join("?" * len(agent_ids))
    if conn.execute(f"SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL AND agent_id IN ({marks})",
                    [task_id, *agent_ids]).fetchone():
        return True
    chat = conn.execute("SELECT id FROM chats WHERE kind = 'task' AND ref = ?", (str(task_id),)).fetchone()
    if chat is None:
        return False
    for agent_id in agent_ids:
        row = conn.execute("SELECT meta FROM chat_messages WHERE chat_id = ? AND author = ? AND kind = 'status' "
                           "ORDER BY id DESC LIMIT 1", (chat["id"], agent_id)).fetchone()
        if row is not None and json.loads(row["meta"]).get("state") in AWAKE_STATES:
            return True
    return False
