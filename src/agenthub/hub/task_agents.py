"""Which agents work on a task, in which stage, with what role.

A task holds several agents. Each member has an operator-written ``role``
(free text, shown to the agent), a ``stage`` (``works`` implements, ``reviews``
reviews) and its own budget, copied from the task's budget when it is added.
Roles are written by the operator only; no agent-facing code path sets them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from agenthub.hub import events, tasks
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound, find_agent

STAGES = ("works", "reviews")
VERDICTS = ("approve", "changes")
MAX_ROLE_CHARS = 200

_COLUMNS = (
    "ta.agent_id AS agent_id, a.name AS name, a.status AS status, ta.role AS role, ta.stage AS stage, "
    "ta.budget AS budget, ta.finished_at AS finished_at, ta.verdict AS verdict, "
    "ta.verdict_notes AS verdict_notes, ta.paused_until AS paused_until, ta.added_by AS added_by, "
    "ta.added_at AS added_at"
)


def list_agents(conn: sqlite3.Connection, task_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM task_agents ta JOIN agents a ON a.id = ta.agent_id "
        "WHERE ta.task_id = ? ORDER BY ta.added_at, ta.rowid",
        (task_id,),
    )
    return [{**dict(r), "budget": json.loads(r["budget"])} for r in rows]


def member(task: dict[str, Any], agent_id: str) -> dict[str, Any] | None:
    return next((m for m in task["agents"] if m["agent_id"] == agent_id), None)


def _check_role(role: str) -> str:
    if not isinstance(role, str) or len(role) > MAX_ROLE_CHARS:
        raise Invalid(f"role must be text of at most {MAX_ROLE_CHARS} characters")
    return role


def _check_stage(stage: str) -> str:
    if stage not in STAGES:
        raise Invalid(f"stage must be one of {', '.join(STAGES)}")
    return stage


def _emit(conn: sqlite3.Connection, kind: str, task: dict[str, Any], agent_id: str, actor: str) -> dict[str, Any]:
    fresh = tasks.get_task(conn, task["id"])
    events.emit(
        conn, type=f"task.agent_{kind}", actor=actor, project=task["project"],
        payload={"task_id": task["id"], "agent_id": agent_id, "agents": [m["agent_id"] for m in fresh["agents"]]},
    )
    return fresh


def add_agent(
    conn: sqlite3.Connection,
    task_id: int,
    agent: str,
    *,
    role: str,
    stage: str,
    actor: str,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = tasks.get_task(conn, task_id)
    _check_role(role)
    _check_stage(stage)
    found = find_agent(conn, agent)
    if found.status != "active" or task["project"] not in found.allowed_projects:
        raise Invalid(f"agent {agent!r} is not an active member of project {task['project']!r}")
    if member(task, found.id):
        raise Conflict(f"agent {found.name!r} is already on task {task_id}")
    conn.execute(
        "INSERT INTO task_agents (task_id, agent_id, role, stage, budget, added_by, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, found.id, role, stage, json.dumps(tasks.normalize_budget(budget, task["budget"])),
         actor, now_iso()),
    )
    return _emit(conn, "added", task, found.id, actor)


def _require_member(conn: sqlite3.Connection, task_id: int, agent: str) -> tuple[dict[str, Any], str]:
    task = tasks.get_task(conn, task_id)
    found = find_agent(conn, agent)
    if member(task, found.id) is None:
        raise NotFound(f"agent {found.name!r} is not on task {task_id}")
    return task, found.id


def remove_agent(
    conn: sqlite3.Connection, task_id: int, agent: str, *, actor: str, advance: bool = True
) -> dict[str, Any]:
    """Remove a member. With ``advance`` (default) the removal may complete the implementers' part."""
    task, agent_id = _require_member(conn, task_id, agent)
    conn.execute("DELETE FROM task_agents WHERE task_id = ? AND agent_id = ?", (task_id, agent_id))
    fresh = _emit(conn, "removed", task, agent_id, actor)
    return maybe_advance(conn, task_id, actor=actor) if advance else fresh


def maybe_advance(conn: sqlite3.Connection, task_id: int, *, actor: str) -> dict[str, Any]:
    """Move an ``in_progress`` task to ``review`` once every active implementer has finished.

    Needs at least one active ``works`` member; revoked or pending ones never block it.
    The move is always made by the hub (``system:hub``); ``actor`` is whoever triggered the check.
    """
    task = tasks.get_task(conn, task_id)
    active = [m for m in task["agents"] if m["stage"] == "works" and m["status"] == "active"]
    if task["state"] == "in_progress" and active and all(m["finished_at"] for m in active):
        return tasks.transition(
            conn, task_id, "review", actor="system:hub", role="system", note="all implementers finished",
        )
    return task


def update_agent(
    conn: sqlite3.Connection,
    task_id: int,
    agent: str,
    *,
    actor: str,
    role: str | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    task, agent_id = _require_member(conn, task_id, agent)
    if role is not None:
        _check_role(role)
    if stage is not None:
        _check_stage(stage)
    current = member(task, agent_id)
    assert current is not None
    conn.execute(
        "UPDATE task_agents SET role = ?, stage = ? WHERE task_id = ? AND agent_id = ?",
        (current["role"] if role is None else role, current["stage"] if stage is None else stage,
         task_id, agent_id),
    )
    return _emit(conn, "updated", task, agent_id, actor)


def _state_membership(
    conn: sqlite3.Connection, task_id: int, agent: Agent, stage: str, state: str
) -> dict[str, Any]:
    task = tasks.get_task(conn, task_id)
    m = member(task, agent.id)
    if m is None or m["stage"] != stage:
        raise Forbidden(f"only agents that {stage[:-1]} this task may do this")
    if task["state"] != state:
        raise Conflict(f"task {task_id} must be {state} (it is {task['state']})")
    return task


def finish(
    conn: sqlite3.Connection, task_id: int, agent: Agent, *, note: str = "", device: str | None = None
) -> dict[str, Any]:
    """An implementer says its part is done. When every *active* implementer has,
    the hub moves the task to ``review``; revoked or pending ones do not block it."""
    task = _state_membership(conn, task_id, agent, "works", "in_progress")
    conn.execute(
        "UPDATE task_agents SET finished_at = COALESCE(finished_at, ?) WHERE task_id = ? AND agent_id = ?",
        (now_iso(), task_id, agent.id),
    )
    fresh = tasks.get_task(conn, task_id)
    events.emit(
        conn, type="task.agent_finished", actor=agent.id, project=task["project"], device=device,
        payload={"task_id": task_id, "agent_id": agent.id, "note": note,
                 "agents": [m["agent_id"] for m in fresh["agents"]]},
    )
    return maybe_advance(conn, task_id, actor=agent.id)


def record_verdict(
    conn: sqlite3.Connection,
    task_id: int,
    agent: Agent,
    *,
    verdict: str,
    notes: str = "",
    device: str | None = None,
) -> dict[str, Any]:
    """A reviewer records its verdict. It never moves the task: only the operator does."""
    if verdict not in VERDICTS:
        raise Invalid(f"verdict must be one of {', '.join(VERDICTS)}")
    task = _state_membership(conn, task_id, agent, "reviews", "review")
    conn.execute(
        "UPDATE task_agents SET verdict = ?, verdict_notes = ? WHERE task_id = ? AND agent_id = ?",
        (verdict, notes, task_id, agent.id),
    )
    fresh = tasks.get_task(conn, task_id)
    events.emit(
        conn, type="task.verdict", actor=agent.id, project=task["project"], device=device,
        payload={"task_id": task_id, "agent_id": agent.id, "verdict": verdict, "notes": notes,
                 "agents": [m["agent_id"] for m in fresh["agents"]]},
    )
    return fresh


def pause(
    conn: sqlite3.Connection, task_id: int, agent: Agent, *, until: str | None, device: str | None = None
) -> dict[str, Any]:
    """An agent sets (or with ``None`` clears) its own ``paused_until``; the value is an RFC 3339
    timestamp with a zone, stored as fixed-width UTC."""
    task = tasks.get_task(conn, task_id)
    if member(task, agent.id) is None:
        raise Forbidden("only agents on this task may do this")
    stored = None
    if until is not None:
        try:
            parsed = datetime.fromisoformat(until)
        except ValueError:
            raise Invalid("until must be an RFC 3339 timestamp") from None
        if parsed.tzinfo is None:
            raise Invalid("until must include a time zone offset")
        stored = now_iso(parsed)
    conn.execute(
        "UPDATE task_agents SET paused_until = ? WHERE task_id = ? AND agent_id = ?",
        (stored, task_id, agent.id),
    )
    return _emit(conn, "paused", task, agent.id, agent.id)
