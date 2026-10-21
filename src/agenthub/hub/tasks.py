"""Tasks and their state machine.

All state changes go through :func:`transition`, which is the only place that
knows the legal moves and who may make them:

  proposed ──▶ awaiting_approval ──▶ open ──▶ claimed ──▶ in_progress ──▶ review ──▶ done
                     │                 ▲         │          │   ▲            │
                     ▼                 └─────────┴──────────┤   │            │ (operator:
                  rejected                                  ▼   │            ▼  changes requested)
                                                          blocked         in_progress
  Any non-terminal state ──▶ cancelled (operator only).

Roles:
  agent     the task's assignee (claim sets it); may go no further than review
  operator  a human; any legal move
  approval  the outcome of an approval decision (awaiting_approval → open / rejected)
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agenthub.hub import events
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound, find_agent

STATES = (
    "proposed", "awaiting_approval", "open", "claimed", "in_progress",
    "blocked", "review", "done", "rejected", "cancelled",
)
TERMINAL = frozenset({"done", "rejected", "cancelled"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"awaiting_approval", "cancelled"}),
    "awaiting_approval": frozenset({"open", "rejected", "cancelled"}),
    "open": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset({"in_progress", "blocked", "open", "cancelled"}),
    "in_progress": frozenset({"blocked", "review", "open", "cancelled"}),
    "blocked": frozenset({"in_progress", "open", "cancelled"}),
    "review": frozenset({"done", "in_progress", "cancelled"}),
}

# Moves each role may make (subset of TRANSITIONS).
AGENT_MOVES = frozenset({
    ("open", "claimed"),
    ("claimed", "in_progress"), ("claimed", "blocked"), ("claimed", "open"),
    ("in_progress", "blocked"), ("in_progress", "review"), ("in_progress", "open"),
    ("blocked", "in_progress"), ("blocked", "open"),
})
APPROVAL_MOVES = frozenset({("awaiting_approval", "open"), ("awaiting_approval", "rejected")})

BUDGET_LIMITS = {"max_turns": (1, 500), "wall_clock_minutes": (1, 24 * 60), "max_subtasks": (0, 50)}
DEFAULT_BUDGET = {"max_turns": 50, "wall_clock_minutes": 60, "max_subtasks": 5}


def check_transition(current: str, new: str, role: str) -> None:
    if new not in STATES:
        raise Invalid(f"unknown task state {new!r}")
    if new not in TRANSITIONS.get(current, frozenset()):
        raise Conflict(f"illegal task transition {current} → {new}")
    if role == "operator":
        return
    allowed = {"agent": AGENT_MOVES, "approval": APPROVAL_MOVES}.get(role)
    if allowed is None:
        raise Invalid(f"unknown role {role!r}")
    if (current, new) not in allowed:
        raise Forbidden(f"{role} may not move a task {current} → {new}")


def normalize_budget(budget: dict[str, Any] | None, defaults: dict[str, Any] | None = None) -> dict[str, int]:
    merged = {**DEFAULT_BUDGET, **(defaults or {}), **(budget or {})}
    unknown = set(merged) - set(BUDGET_LIMITS)
    if unknown:
        raise Invalid(f"unknown budget keys: {', '.join(sorted(unknown))}")
    out = {}
    for key, (lo, hi) in BUDGET_LIMITS.items():
        value = merged[key]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise Invalid(f"budget {key} must be an integer in [{lo}, {hi}]")
        out[key] = value
    return out


_SELECT = (
    "SELECT t.*, a.name AS assignee_name, c.name AS created_by_name, ap.status AS approval_status, "
    "(SELECT COUNT(*) FROM task_runs r WHERE r.task_id = t.id) AS run_count FROM tasks t "
    "LEFT JOIN agents a ON a.id = t.assignee LEFT JOIN agents c ON c.id = t.created_by "
    "LEFT JOIN approvals ap ON ap.id = t.approval_id "
)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["budget"] = json.loads(out["budget"])
    return out


def get_task(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    row = conn.execute(_SELECT + "WHERE t.id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"no task {task_id}")
    return _row(row)


def list_tasks(
    conn: sqlite3.Connection,
    *,
    projects: list[str] | None,
    state: str | None = None,
    assignee: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = _SELECT + "WHERE 1 = 1 "
    params: list[Any] = []
    if projects is not None:
        if not projects:
            return []
        sql += f"AND t.project IN ({','.join('?' * len(projects))}) "
        params.extend(projects)
    if state is not None:
        if state not in STATES:
            raise Invalid(f"unknown task state {state!r}")
        sql += "AND t.state = ? "
        params.append(state)
    if assignee is not None:
        sql += "AND t.assignee = ? "
        params.append(assignee)
    sql += "ORDER BY t.id LIMIT ?"
    params.append(limit)
    return [_row(r) for r in conn.execute(sql, params)]


def _resolve_assignee(conn: sqlite3.Connection, assignee: str | None, project: str) -> str | None:
    if assignee is None:
        return None
    agent = find_agent(conn, assignee)
    if agent.status != "active" or project not in agent.allowed_projects:
        raise Invalid(f"agent {assignee!r} is not an active member of project {project!r}")
    return agent.id


def create_task(
    conn: sqlite3.Connection,
    *,
    actor: str,
    project: str,
    title: str,
    body: str = "",
    assignee: str | None = None,
    budget: dict[str, Any] | None = None,
    budget_defaults: dict[str, Any] | None = None,
    state: str = "open",
    device: str | None = None,
) -> dict[str, Any]:
    """Insert a task. Callers choose the initial state (``open`` for operators,
    ``awaiting_approval`` for agents via :func:`approvals.agent_create_task`)."""
    if state not in ("proposed", "awaiting_approval", "open"):
        raise Invalid(f"tasks cannot be created in state {state!r}")
    if not conn.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone():
        raise NotFound(f"no project {project!r}")
    assignee_id = _resolve_assignee(conn, assignee, project)
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO tasks (project, title, body, created_by, assignee, state, budget, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project, title, body, actor, assignee_id, state,
         json.dumps(normalize_budget(budget, budget_defaults)), ts, ts),
    )
    task_id = cur.lastrowid
    events.emit(
        conn, type="task.created", actor=actor, project=project, device=device,
        payload={"task_id": task_id, "title": title, "body": body, "state": state, "assignee": assignee_id},
    )
    return get_task(conn, task_id)


def transition(
    conn: sqlite3.Connection,
    task_id: int,
    new_state: str,
    *,
    actor: str,
    role: str,
    agent: Agent | None = None,
    note: str = "",
    device: str | None = None,
) -> dict[str, Any]:
    """The single place task state changes. Uses compare-and-set on the old state."""
    task = get_task(conn, task_id)
    current = task["state"]
    check_transition(current, new_state, role)
    assignee = task["assignee"]
    if role == "agent":
        if agent is None:
            raise Invalid("agent role requires the acting agent")
        if new_state == "claimed":
            if assignee not in (None, agent.id):
                raise Forbidden("task is assigned to another agent")
            assignee = agent.id
        elif assignee != agent.id:
            raise Forbidden("only the task's assignee may change it")
    if new_state == "open" and current in ("claimed", "in_progress", "blocked"):
        assignee = None  # unclaimed: back in the pool
    cur = conn.execute(
        "UPDATE tasks SET state = ?, assignee = ?, updated_at = ?, last_note = ?, last_moved_by = ? "
        "WHERE id = ? AND state = ?",
        (new_state, assignee, now_iso(), note, actor, task_id, current),
    )
    if cur.rowcount == 0:
        raise Conflict(f"task {task_id} changed concurrently; re-read and retry")
    events.emit(
        conn, type="task.transitioned", actor=actor, project=task["project"], device=device,
        payload={"task_id": task_id, "from": current, "to": new_state, "assignee": assignee, "note": note},
    )
    return get_task(conn, task_id)


def assign(conn: sqlite3.Connection, task_id: int, assignee: str | None, *, actor: str) -> dict[str, Any]:
    """Operator (or approved reassignment) sets the assignee.

    A task that was already claimed goes back to ``open`` for the new assignee,
    so the runner's "open and assigned to me" launch check applies afresh.
    """
    task = get_task(conn, task_id)
    if task["state"] in TERMINAL:
        raise Conflict(f"task {task_id} is {task['state']}")
    assignee_id = _resolve_assignee(conn, assignee, task["project"])
    state = "open" if task["state"] in ("claimed", "in_progress", "blocked") else task["state"]
    conn.execute(
        "UPDATE tasks SET assignee = ?, state = ?, updated_at = ? WHERE id = ?",
        (assignee_id, state, now_iso(), task_id),
    )
    events.emit(
        conn, type="task.assigned", actor=actor, project=task["project"],
        payload={"task_id": task_id, "assignee": assignee_id, "from_state": task["state"], "state": state},
    )
    return get_task(conn, task_id)


def add_note(conn: sqlite3.Connection, task_id: int, note: str, *, agent: Agent, device: str | None = None) -> None:
    task = get_task(conn, task_id)
    if task["assignee"] != agent.id:
        raise Forbidden("only the task's assignee may add progress notes")
    events.emit(conn, type="task.note", actor=agent.id, project=task["project"], device=device,
                payload={"task_id": task_id, "note": note})


# --- runs, progress, budgets (used by the runner) --------------------------------

MAX_PROGRESS_ITEMS = 20
MAX_PROGRESS_TEXT = 500
# Approved extensions double a limit, up to these hard caps.
EXTENSION_CAPS = {"max_turns": 5000, "wall_clock_minutes": 7 * 24 * 60, "max_subtasks": 200}


def _require_assignee(task: dict[str, Any], agent: Agent) -> None:
    if task["assignee"] != agent.id:
        raise Forbidden("only the task's assignee may do this")


def list_runs(conn: sqlite3.Connection, task_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,))]


def usage(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    """Budget consumed so far across all runs of a task."""
    runs = list_runs(conn, task_id)
    minutes = 0.0
    for r in runs:
        if r["ended_at"]:
            minutes += (_ts(r["ended_at"]) - _ts(r["started_at"])) / 60
    last_session = next((r["session_id"] for r in reversed(runs) if r["session_id"]), None)
    return {"runs": len(runs), "turns": sum(r["num_turns"] or 0 for r in runs),
            "wall_clock_minutes": round(minutes, 2), "last_session_id": last_session}


def _ts(value: str) -> float:
    from datetime import datetime

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()


def start_run(conn: sqlite3.Connection, task_id: int, agent: Agent, *, device: str | None = None) -> dict[str, Any]:
    task = get_task(conn, task_id)
    _require_assignee(task, agent)
    if task["state"] != "in_progress":
        raise Conflict(f"task {task_id} must be in_progress to start a run (it is {task['state']})")
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL", (task_id,)).fetchone():
        raise Conflict(f"task {task_id} already has an active run")
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, agent_id, started_at) VALUES (?, ?, ?)", (task_id, agent.id, now_iso())
    )
    number = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()[0]
    events.emit(conn, type="task.run_started", actor=agent.id, project=task["project"], device=device,
                payload={"task_id": task_id, "run_id": cur.lastrowid, "number": number})
    return {"id": cur.lastrowid, "number": number}


def end_run(
    conn: sqlite3.Connection,
    task_id: int,
    run_id: int,
    agent: Agent,
    *,
    outcome: str,
    session_id: str | None,
    num_turns: int | None,
    cost_usd: float | None,
    device: str | None = None,
) -> dict[str, Any]:
    task = get_task(conn, task_id)
    _require_assignee(task, agent)
    cur = conn.execute(
        "UPDATE task_runs SET ended_at = ?, outcome = ?, session_id = COALESCE(?, session_id), "
        "num_turns = ?, cost_usd = ? WHERE id = ? AND task_id = ? AND agent_id = ? AND ended_at IS NULL",
        (now_iso(), outcome, session_id, num_turns, cost_usd, run_id, task_id, agent.id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"run {run_id} is not an active run of task {task_id}")
    events.emit(conn, type="task.run_ended", actor=agent.id, project=task["project"], device=device,
                payload={"task_id": task_id, "run_id": run_id, "outcome": outcome, "num_turns": num_turns,
                         "cost_usd": cost_usd})
    return usage(conn, task_id)


def set_run_session(conn: sqlite3.Connection, task_id: int, run_id: int, agent: Agent, session_id: str) -> None:
    conn.execute(
        "UPDATE task_runs SET session_id = ? WHERE id = ? AND task_id = ? AND agent_id = ?",
        (session_id, run_id, task_id, agent.id),
    )


def record_progress(
    conn: sqlite3.Connection, task_id: int, agent: Agent, items: list[dict[str, Any]],
    *, run_id: int | None = None, device: str | None = None,
) -> int:
    task = get_task(conn, task_id)
    _require_assignee(task, agent)
    clean = [
        {"kind": str(i.get("kind", "note"))[:32], "text": str(i.get("text", ""))[:MAX_PROGRESS_TEXT]}
        for i in items[:MAX_PROGRESS_ITEMS]
    ]
    return events.emit(conn, type="task.progress", actor=agent.id, project=task["project"], device=device,
                       payload={"task_id": task_id, "run_id": run_id, "items": clean})


def budget_extension(task: dict[str, Any], limit: str) -> int:
    """The new value an approved budget_exceeded approval sets (computed by the hub, not the agent)."""
    if limit not in BUDGET_LIMITS:
        raise Invalid(f"unknown budget limit {limit!r}")
    return min(task["budget"][limit] * 2 or 1, EXTENSION_CAPS[limit])


def extend_budget(conn: sqlite3.Connection, task_id: int, limit: str, value: int, *, actor: str) -> dict[str, Any]:
    task = get_task(conn, task_id)
    budget = {**task["budget"], limit: value}
    conn.execute("UPDATE tasks SET budget = ?, updated_at = ? WHERE id = ?", (json.dumps(budget), now_iso(), task_id))
    events.emit(conn, type="task.budget_extended", actor=actor, project=task["project"],
                payload={"task_id": task_id, "limit": limit, "value": value, "assignee": task["assignee"]})
    return get_task(conn, task_id)
