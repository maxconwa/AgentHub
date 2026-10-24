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
    "ta.budget AS budget, ta.finished_at AS finished_at, ta.finish_note AS finish_note, ta.verdict AS verdict, "
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
    if stage is not None and stage != current["stage"]:
        if current["stage"] == "works":
            conn.execute(
                "UPDATE task_agents SET finished_at = NULL, finish_note = '' WHERE task_id = ? AND agent_id = ?",
                (task_id, agent_id),
            )
        else:
            conn.execute(
                "UPDATE task_agents SET verdict = NULL, verdict_notes = '' WHERE task_id = ? AND agent_id = ?",
                (task_id, agent_id),
            )
        _emit(conn, "updated", task, agent_id, actor)
        return maybe_advance(conn, task_id, actor=actor)
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
    the hub moves the task to ``review``; revoked or pending ones do not block it, and
    its reviewers are woken with this note (:func:`wake_reviewers`).

    A reviewer finishing a task of its own (a review task, as #39 was) without having recorded any verdict since the
    task last entered in_progress still finishes, but the result carries ``warning: "no_verdict"`` and the task chat
    gets a status line saying so (:func:`missing_verdict`)."""
    task = _state_membership(conn, task_id, agent, "works", "in_progress")
    conn.execute(
        "UPDATE task_agents SET finished_at = COALESCE(finished_at, ?), finish_note = ? "
        "WHERE task_id = ? AND agent_id = ?",
        (now_iso(), note, task_id, agent.id),
    )
    fresh = tasks.get_task(conn, task_id)
    events.emit(
        conn, type="task.agent_finished", actor=agent.id, project=task["project"], device=device,
        payload={"task_id": task_id, "agent_id": agent.id, "note": note,
                 "agents": [m["agent_id"] for m in fresh["agents"]]},
    )
    warning = missing_verdict(conn, task, agent)
    fresh = maybe_advance(conn, task_id, actor=agent.id)
    if fresh["state"] == "review":
        wake_reviewers(conn, fresh, agent, note)
        fresh = tasks.get_task(conn, task_id)
    return {**fresh, "warning": warning} if warning else fresh


def is_reviewer(conn: sqlite3.Connection, project: str, agent_id: str) -> bool:
    """Whether ``agent_id`` reviews in ``project``: one of its project reviewers, or a ``reviews`` member of one of
    its tasks."""
    return conn.execute(
        "SELECT 1 FROM project_reviewers WHERE project = ? AND agent_id = ? UNION ALL "
        "SELECT 1 FROM task_agents ta JOIN tasks t ON t.id = ta.task_id "
        "WHERE t.project = ? AND ta.agent_id = ? AND ta.stage = 'reviews' LIMIT 1",
        (project, agent_id, project, agent_id)).fetchone() is not None


def missing_verdict(conn: sqlite3.Connection, task: dict[str, Any], agent: Agent) -> str | None:
    """``"no_verdict"`` when ``agent``, a reviewer in the task's project, finishes its part of ``task`` having recorded
    no verdict (``review_task``, on any task) since the task last entered in_progress, and the task chat says so;
    else None. That is how #39 went to review before anything had been reviewed."""
    if not is_reviewer(conn, task["project"], agent.id):
        return None
    entered = conn.execute(
        "SELECT MAX(ts) FROM events WHERE type = 'task.transitioned' AND json_extract(payload, '$.task_id') = ? "
        "AND json_extract(payload, '$.to') = 'in_progress'", (task["id"],)).fetchone()[0] or task["created_at"]
    if conn.execute("SELECT 1 FROM events WHERE type = 'task.verdict' AND actor = ? AND ts >= ? LIMIT 1",
                    (agent.id, entered)).fetchone():
        return None
    warn(conn, task["id"], agent, "no_verdict")
    return "no_verdict"


def warn(conn: sqlite3.Connection, task_id: int, agent: Agent, warning: str) -> None:
    """A hub status line in the task chat warning about ``agent`` (``meta.warning``: ``no_verdict``)."""
    from agenthub.hub import chats

    chats.hub_status(conn, chats.chat(conn, "task", str(task_id)),
                     {"state": warning, "warning": warning, "task_id": task_id, "agent_id": agent.id,
                      "agent_name": agent.name})


def record_verdict(
    conn: sqlite3.Connection,
    task_id: int,
    agent: Agent,
    *,
    verdict: str,
    notes: str = "",
    device: str | None = None,
) -> dict[str, Any]:
    """A reviewer records its verdict. ``approve`` leaves the task in review (only the operator marks it done);
    ``changes`` sends it back: review → in_progress with the notes as the send-back note (which clears this round's
    finishes and verdicts), and its builders are woken in the task's chat (:func:`wake_builders`)."""
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
    if verdict != "changes":
        return fresh
    fresh = tasks.transition(conn, task_id, "in_progress", actor=agent.id, role="reviewer", agent=agent,
                             note=notes.strip() or "changes requested", device=device)
    wake_builders(conn, fresh, agent, notes)
    return tasks.get_task(conn, task_id)


def wake_builders(conn: sqlite3.Connection, task: dict[str, Any], reviewer: Agent, notes: str) -> None:
    """A reviewer sent ``task`` back: its active builders are woken with the notes, by the reviewer's message in the
    task chat addressed to them (an agent's chat message wakes its recipients, 0.3.9.1). Their runners also resume
    the task's session for the move back to in_progress, with the notes as the send-back note."""
    from agenthub.hub import chats  # chats imports this module's neighbours

    builders = [m for m in task["agents"] if m["stage"] == "works" and m["status"] == "active"]
    if not builders:
        return
    body = (f"Changes requested on task #{task['id']}; it is back in progress.\n\n"
            + (notes.strip() or "(no notes)"))[:chats.MAX_BODY_CHARS]
    meta = {"send_back": True}
    # chats.post is the wake path itself (#30); it skips the per-agent wake rate limit, which lives in the API
    # route, but that is bounded here by the task's builders
    for m in builders:
        chats.post(conn, reviewer, "task", str(task["id"]), body, to=m["agent_id"], meta=meta)


def wake_reviewers(conn: sqlite3.Connection, task: dict[str, Any], builder: Agent, note: str) -> None:
    """``builder``'s finish moved ``task`` to review (0.3.11): its active reviewers are woken with the finish note, by
    the builder's message in the task chat addressed to each, the mirror of :func:`wake_builders`. A hand-off never
    goes silent, and it keeps wake chains, loop protection and the pause switches (:func:`chats.post`)."""
    from agenthub.hub import chats

    reviewers = [m for m in task["agents"] if m["stage"] == "reviews" and m["status"] == "active"]
    if not reviewers:
        return
    body = (f"Task #{task['id']} is ready for review: every implementer has finished.\n\n"
            + (note.strip() or "(no note)"))[:chats.MAX_BODY_CHARS]
    for m in reviewers:
        chats.post(conn, builder, "task", str(task["id"]), body, to=m["agent_id"], meta={"handoff": True})


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
        try:
            stored = now_iso(parsed)
        except OverflowError:
            raise Invalid("until is out of range") from None
    conn.execute(
        "UPDATE task_agents SET paused_until = ? WHERE task_id = ? AND agent_id = ?",
        (stored, task_id, agent.id),
    )
    return _emit(conn, "paused", task, agent.id, agent.id)
