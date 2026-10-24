"""Tasks and their state machine.

All state changes go through :func:`transition`, which is the only place that
knows the legal moves and who may make them:

  proposed ──▶ awaiting_approval ──▶ open ──▶ claimed ──▶ in_progress ──▶ review ──▶ done
                     │                 ▲         │          │   ▲            │
                     ▼                 └─────────┴──────────┤   │            │ (reopened: the operator,
                  rejected                                  ▼   │            ▼  a reviewer, an implementer)
                                                          blocked         in_progress
  Any non-terminal state ──▶ cancelled (operator only).
  Moving to done archives a task (``archived_at``): the at-a-glance lists leave it out, its page and chat stay.

Roles:
  agent     a ``works`` member of the task (claiming an unstaffed task joins it); may not
            move a task to review: agents finish, and the hub moves it. Any member, ``works`` or
            ``reviews``, may reopen it (0.3.11): review → in_progress, for its implementers to finish again
  system    the hub itself: in_progress → review once every active implementer finished
  reviewer  a ``reviews`` member whose ``changes`` verdict sends the task back: review → in_progress
            (``task_agents.record_verdict``); ``approve`` never moves a task, only the operator marks it done
  operator  a human; any legal move
  approval  the outcome of an approval decision (awaiting_approval → open / rejected)
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agenthub.hub import events, limits
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound, find_agent, require_project_name

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
    ("in_progress", "blocked"), ("in_progress", "open"),
    ("blocked", "in_progress"), ("blocked", "open"),
    ("review", "in_progress"),  # a reopen (0.3.11): any member, while an implementer is active to finish it again
})
APPROVAL_MOVES = frozenset({("awaiting_approval", "open"), ("awaiting_approval", "rejected")})
SYSTEM_MOVES = frozenset({("in_progress", "review")})
REVIEWER_MOVES = frozenset({("review", "in_progress")})

BUDGET_LIMITS = {"max_turns": (1, 500), "wall_clock_minutes": (1, 24 * 60), "max_subtasks": (0, 50)}
DEFAULT_BUDGET = {"max_turns": 50, "wall_clock_minutes": 60, "max_subtasks": 5}


def check_transition(current: str, new: str, role: str) -> None:
    if new not in STATES:
        raise Invalid(f"unknown task state {new!r}")
    if new not in TRANSITIONS.get(current, frozenset()):
        raise Conflict(f"illegal task transition {current} → {new}")
    if role == "operator":
        return
    allowed = {"agent": AGENT_MOVES, "approval": APPROVAL_MOVES, "system": SYSTEM_MOVES,
               "reviewer": REVIEWER_MOVES}.get(role)
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
    "SELECT t.*, c.name AS created_by_name, ap.status AS approval_status, "
    "(SELECT COUNT(*) FROM task_runs r WHERE r.task_id = t.id) AS run_count FROM tasks t "
    "LEFT JOIN agents c ON c.id = t.created_by "
    "LEFT JOIN approvals ap ON ap.id = t.approval_id "
)


# Columns still in the table but superseded; never exposed on task dicts.
_LEGACY_COLUMNS = ("assignee",)


def _row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    from agenthub.hub import task_agents  # task_agents imports this module

    out = {k: v for k, v in dict(row).items() if k not in _LEGACY_COLUMNS}  # membership: task_agents
    out["budget"] = json.loads(out["budget"])
    out["agents"] = task_agents.list_agents(conn, out["id"])
    return out


def get_task(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    row = conn.execute(_SELECT + "WHERE t.id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"no task {task_id}")
    return _row(conn, row)


def list_tasks(
    conn: sqlite3.Connection,
    *,
    projects: list[str] | None,
    state: str | None = None,
    agent: str | None = None,
    limit: int = 100,
    newest_first: bool = False,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Tasks, oldest first (``newest_first`` reverses it). Archived tasks are left out unless ``include_archived`` or
    an explicit ``state='done'`` (every archived task is done, so that filter would otherwise never match)."""
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
    if not include_archived and state != "done":
        sql += "AND t.archived_at IS NULL "
    if agent is not None:
        sql += "AND EXISTS (SELECT 1 FROM task_agents ta WHERE ta.task_id = t.id AND ta.agent_id = ?) "
        params.append(agent)
    sql += "ORDER BY t.id DESC LIMIT ?" if newest_first else "ORDER BY t.id LIMIT ?"
    params.append(limit)
    return [_row(conn, r) for r in conn.execute(sql, params)]


def archive_done(conn: sqlite3.Connection) -> int:
    """Archive done tasks that aren't yet, as of their last move; returns how many. Migration 28 does this once; the
    hub runs it at every start too, for tasks an older hub (which ignores ``archived_at``) marked done while rolled
    back."""
    return conn.execute(
        "UPDATE tasks SET archived_at = updated_at WHERE state = 'done' AND archived_at IS NULL").rowcount


def archived_tasks(conn: sqlite3.Connection, agent_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """The archived tasks ``agent_id`` is on (either stage), the most recently archived first, for its agent page."""
    rows = conn.execute(
        _SELECT + "WHERE t.archived_at IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM task_agents ta WHERE ta.task_id = t.id AND ta.agent_id = ?) "
        "ORDER BY t.archived_at DESC, t.id DESC LIMIT ?", (agent_id, limit))
    return [_row(conn, r) for r in rows]


def create_task(
    conn: sqlite3.Connection,
    *,
    actor: str,
    project: str,
    title: str,
    body: str = "",
    assignee: str | None = None,
    role: str = "",
    budget: dict[str, Any] | None = None,
    budget_defaults: dict[str, Any] | None = None,
    state: str = "open",
    device: str | None = None,
    prompt_id: int | None = None,
) -> dict[str, Any]:
    """Insert a task. Callers choose the initial state (``open`` for operators and for an agent's share of an
    operator's prompt, ``prompt_id``; ``awaiting_approval`` for agents via :func:`approvals.agent_create_task`)."""
    if state not in ("proposed", "awaiting_approval", "open"):
        raise Invalid(f"tasks cannot be created in state {state!r}")
    project = require_project_name(conn, project)
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO tasks (project, title, body, created_by, state, budget, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project, title, body, actor, state,
         json.dumps(normalize_budget(budget, budget_defaults)), ts, ts),
    )
    task_id = cur.lastrowid
    payload: dict[str, Any] = {"task_id": task_id, "title": title, "body": body, "state": state, "agents": []}
    if prompt_id is not None:
        payload["prompt_id"] = prompt_id
    events.emit(conn, type="task.created", actor=actor, project=project, device=device, payload=payload)
    if assignee is not None:
        from agenthub.hub import task_agents

        task_agents.add_agent(conn, task_id, assignee, role=role, stage="works", actor=actor)
    return get_task(conn, task_id)


MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = limits.AGENT_TEXT_CHARS  # an agent's task description; the operator's has no limit


def set_description(conn: sqlite3.Connection, task_id: int, title: str, body: str, *, actor: str) -> dict[str, Any]:
    """Edit a task's title and description. Operator only (the route enforces it), so the description has no length
    limit; the event names the changed fields but carries no text."""
    title = title.strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise Invalid(f"title must be 1 to {MAX_TITLE_CHARS} characters")
    task = get_task(conn, task_id)
    conn.execute("UPDATE tasks SET title = ?, body = ?, updated_at = ? WHERE id = ?", (title, body, now_iso(), task_id))
    events.emit(conn, type="task.updated", actor=actor, project=task["project"],
                payload={"task_id": task_id, "fields": ["title", "body"]})
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
    from agenthub.hub import task_agents

    released = False
    if role == "agent":
        if agent is None:
            raise Invalid("agent role requires the acting agent")
        if new_state == "claimed" and not any(m["stage"] == "works" for m in task["agents"]):
            task_agents.add_agent(conn, task_id, agent.id, role="", stage="works", actor=actor)
        elif current == "review":  # a reopen: by any member, for its implementers to finish again
            _require_member(task, agent)
            if not any(m["stage"] == "works" and m["status"] == "active" for m in task["agents"]):
                raise Conflict(f"task {task_id} has no active implementer to reopen it for; ask the operator")
        else:
            _require_member(task, agent, "works")
        released = new_state == "open" and current in ("claimed", "in_progress", "blocked")
    elif role == "reviewer":
        if agent is None:
            raise Invalid("reviewer role requires the acting agent")
        _require_member(task, agent, "reviews")
    if released:
        assert agent is not None
        task_agents.remove_agent(conn, task_id, agent.id, actor=actor, advance=False)
        if any(
            m["stage"] == "works" and m["status"] == "active" and m["agent_id"] != agent.id
            for m in task["agents"]
        ):
            # other active implementers remain: only the membership changed (this may complete their part)
            return task_agents.maybe_advance(conn, task_id, actor=actor)
    elif new_state == "open" and current in ("claimed", "in_progress", "blocked"):
        for m in task["agents"]:  # operator unclaim: implementers leave, reviewers stay
            if m["stage"] == "works":
                task_agents.remove_agent(conn, task_id, m["agent_id"], actor=actor, advance=False)
    ts = now_iso()
    cur = conn.execute(  # done archives the task; any other state un-archives it
        "UPDATE tasks SET state = ?, updated_at = ?, last_note = ?, last_moved_by = ?, archived_at = ? "
        "WHERE id = ? AND state = ?",
        (new_state, ts, note, actor, ts if new_state == "done" else None, task_id, current),
    )
    if cur.rowcount == 0:
        raise Conflict(f"task {task_id} changed concurrently; re-read and retry")
    if current == "review" and new_state == "in_progress":  # sent back or reopened: a new round
        conn.execute("UPDATE task_agents SET finished_at = NULL WHERE task_id = ? AND stage = 'works'", (task_id,))
        conn.execute(
            "UPDATE task_agents SET verdict = NULL, verdict_notes = '' WHERE task_id = ? AND stage = 'reviews'",
            (task_id,),
        )
        if role != "reviewer":  # a changes verdict says so in its own messages to the builders
            _reopened_line(conn, task_id, actor, agent if role == "agent" else None, note)
    if new_state == "review":  # reviewers are always there: the project's, if the task has none
        from agenthub.hub import reviewers

        reviewers.fill(conn, get_task(conn, task_id))
    fresh = get_task(conn, task_id)
    events.emit(
        conn, type="task.transitioned", actor=actor, project=task["project"], device=device,
        payload={"task_id": task_id, "from": current, "to": new_state, "note": note,
                 "agents": [m["agent_id"] for m in fresh["agents"]]},
    )
    if new_state == "in_progress" and current != "review":
        # an unblock may find every remaining implementer already finished
        return task_agents.maybe_advance(conn, task_id, actor=actor)
    return fresh


def _reopened_line(conn: sqlite3.Connection, task_id: int, actor: str, agent: Agent | None, note: str) -> None:
    """The task chat's status line for a reopen (review → in_progress by the operator, an approval or an agent on the
    task). An agent's note goes in as ``detail``, which the chat shows as agent text."""
    from agenthub.hub import chats

    meta: dict[str, Any] = {"state": "reopened", "task_id": task_id, "by": actor}
    if agent is not None:
        meta |= {"agent_id": agent.id, "agent_name": agent.name}
        if note:
            meta["detail"] = note[:chats.MAX_DETAIL_CHARS]
    chats.hub_status(conn, chats.chat(conn, "task", str(task_id)), meta)


def assign(
    conn: sqlite3.Connection, task_id: int, agent: str | None, *, actor: str, role: str | None = None
) -> dict[str, Any]:
    """Operator (or approved reassignment) makes ``agent`` the only ``works`` agent.

    Other ``works`` agents are removed and reviewers stay. A task that was already
    claimed goes back to ``open`` for the new agent, so the runner's launch check
    applies afresh. ``agent=None`` just clears the ``works`` agents. ``role=None`` keeps an
    existing member's role (a new member gets an empty one).
    """
    from agenthub.hub import task_agents

    task = get_task(conn, task_id)
    if task["state"] in TERMINAL:
        raise Conflict(f"task {task_id} is {task['state']}")
    target = find_agent(conn, agent) if agent is not None else None
    for m in task["agents"]:
        if m["stage"] == "works" and (target is None or m["agent_id"] != target.id):
            task_agents.remove_agent(conn, task_id, m["agent_id"], actor=actor, advance=False)
    if target is not None:
        if task_agents.member(task, target.id):
            task_agents.update_agent(conn, task_id, target.id, actor=actor, role=role, stage="works")
        else:
            task_agents.add_agent(conn, task_id, target.id, role=role or "", stage="works", actor=actor)
    state = "open" if task["state"] in ("claimed", "in_progress", "blocked") else task["state"]
    conn.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?", (state, now_iso(), task_id))
    fresh = get_task(conn, task_id)
    events.emit(
        conn, type="task.assigned", actor=actor, project=task["project"],
        payload={"task_id": task_id, "agent_id": target.id if target else None,
                 "from_state": task["state"], "state": state, "agents": _member_ids(fresh)},
    )
    return fresh


def add_note(conn: sqlite3.Connection, task_id: int, note: str, *, agent: Agent, device: str | None = None) -> None:
    task = get_task(conn, task_id)
    _require_member(task, agent)
    events.emit(conn, type="task.note", actor=agent.id, project=task["project"], device=device,
                payload={"task_id": task_id, "note": note, "agents": _member_ids(task)})


# --- runs, progress, budgets (used by the runner) --------------------------------

MAX_PROGRESS_ITEMS = 20
MAX_PROGRESS_TEXT = 500
# Approved extensions double a limit, up to these hard caps.
EXTENSION_CAPS = {"max_turns": 5000, "wall_clock_minutes": 7 * 24 * 60, "max_subtasks": 200}


def _member_ids(task: dict[str, Any]) -> list[str]:
    return [m["agent_id"] for m in task["agents"]]


def _require_member(task: dict[str, Any], agent: Agent, stage: str | None = None) -> dict[str, Any]:
    from agenthub.hub import task_agents

    m = task_agents.member(task, agent.id)
    if m is None or (stage is not None and m["stage"] != stage):
        raise Forbidden("only agents on this task may do this")
    return m


def list_runs(conn: sqlite3.Connection, task_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,))]


def usage(conn: sqlite3.Connection, task_id: int, agent_id: str | None = None) -> dict[str, Any]:
    """Budget consumed so far by a task's runs (only ``agent_id``'s when given).

    Runs stopped by the usage limit use up turns and minutes but not the run count.
    """
    runs = [r for r in list_runs(conn, task_id) if agent_id is None or r["agent_id"] == agent_id]
    minutes = 0.0
    for r in runs:
        if r["ended_at"]:
            minutes += (_ts(r["ended_at"]) - _ts(r["started_at"])) / 60
    last_session = next((r["session_id"] for r in reversed(runs) if r["session_id"]), None)
    return {"runs": sum(1 for r in runs if r["outcome"] != "usage_limit"),
            "turns": sum(r["num_turns"] or 0 for r in runs),
            "wall_clock_minutes": round(minutes, 2), "last_session_id": last_session}


def _ts(value: str) -> float:
    from datetime import datetime

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()


def start_run(conn: sqlite3.Connection, task_id: int, agent: Agent, *, device: str | None = None) -> dict[str, Any]:
    task = get_task(conn, task_id)
    m = _require_member(task, agent)
    needed = "review" if m["stage"] == "reviews" else "in_progress"
    if task["state"] != needed:
        raise Conflict(f"task {task_id} must be {needed} to start a run (it is {task['state']})")
    # One active run per (task, agent): several implementers may run on one task at once.
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id = ? AND agent_id = ? AND ended_at IS NULL",
                    (task_id, agent.id)).fetchone():
        raise Conflict(f"you already have an active run on task {task_id}")
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, agent_id, started_at) VALUES (?, ?, ?)", (task_id, agent.id, now_iso())
    )
    # This agent's run number; runs stopped by the usage limit don't count (as in usage()).
    number = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND agent_id = ? AND outcome IS NOT 'usage_limit'",
        (task_id, agent.id),
    ).fetchone()[0]
    events.emit(conn, type="task.run_started", actor=agent.id, project=task["project"], device=device,
                payload={"task_id": task_id, "run_id": cur.lastrowid, "number": number,
                         "agents": _member_ids(task)})
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
    _require_member(task, agent)
    cur = conn.execute(
        "UPDATE task_runs SET ended_at = ?, outcome = ?, session_id = COALESCE(?, session_id), "
        "num_turns = ?, cost_usd = ? WHERE id = ? AND task_id = ? AND agent_id = ? AND ended_at IS NULL",
        (now_iso(), outcome, session_id, num_turns, cost_usd, run_id, task_id, agent.id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"run {run_id} is not an active run of task {task_id}")
    events.emit(conn, type="task.run_ended", actor=agent.id, project=task["project"], device=device,
                payload={"task_id": task_id, "run_id": run_id, "outcome": outcome, "num_turns": num_turns,
                         "cost_usd": cost_usd, "agents": _member_ids(task)})
    found = usage(conn, task_id, agent.id)  # the ending agent's own consumption
    m = _require_member(get_task(conn, task_id), agent)
    if outcome == "success" and m["stage"] == "reviews" and task["state"] == "review" and m["verdict"] is None:
        from agenthub.hub import task_agents  # a reviewer's session ended without its verdict (0.3.11)

        task_agents.warn(conn, task_id, agent, "no_verdict")
        found["warning"] = "no_verdict"
    return found


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
    _require_member(task, agent)
    clean = [
        {"kind": str(i.get("kind", "note"))[:32], "text": str(i.get("text", ""))[:MAX_PROGRESS_TEXT]}
        for i in items[:MAX_PROGRESS_ITEMS]
    ]
    return events.emit(conn, type="task.progress", actor=agent.id, project=task["project"], device=device,
                       payload={"task_id": task_id, "run_id": run_id, "items": clean,
                               "agents": _member_ids(task)})


def budget_extension(member: dict[str, Any], limit: str) -> int:
    """The new value an approved budget_exceeded approval sets (computed by the hub, not the agent)."""
    if limit not in BUDGET_LIMITS:
        raise Invalid(f"unknown budget limit {limit!r}")
    return min(member["budget"][limit] * 2 or 1, EXTENSION_CAPS[limit])


def extend_budget(
    conn: sqlite3.Connection, task_id: int, agent_id: str, limit: str, value: int, *, actor: str
) -> dict[str, Any]:
    """Set one agent's budget limit; other members' budgets are untouched."""
    from agenthub.hub import task_agents

    task = get_task(conn, task_id)
    m = task_agents.member(task, agent_id)
    if m is None:
        raise NotFound(f"agent {agent_id} is not on task {task_id}")
    budget = {**m["budget"], limit: value}
    conn.execute("UPDATE task_agents SET budget = ? WHERE task_id = ? AND agent_id = ?",
                 (json.dumps(budget), task_id, agent_id))
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now_iso(), task_id))
    events.emit(conn, type="task.budget_extended", actor=actor, project=task["project"],
                payload={"task_id": task_id, "agent_id": agent_id, "limit": limit, "value": value,
                         "agents": _member_ids(task)})
    return get_task(conn, task_id)
