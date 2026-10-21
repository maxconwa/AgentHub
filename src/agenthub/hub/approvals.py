"""Approvals: requests that wait for a human decision.

Only the operator decides (CLI in Phase 2, dashboard in Phase 4); no agent
endpoint calls :func:`decide`. Deciding applies the approval's side effect in
the same transaction (open a task, activate an agent, ...).

Command approvals are bound to (agent, cwd, exact command). Once approved,
:func:`consume_command` lets that command through exactly once, within
``COMMAND_APPROVAL_TTL`` of the decision.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthub import security
from agenthub.hub import events, store, tasks
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound

KINDS = ("command", "task_create", "task_reassign", "budget_exceeded", "agent_enrollment", "tool_permission")
# What an agent may file through the generic request endpoint. task_create goes
# through create_task; agent_enrollment is filed by the hub itself.
AGENT_REQUESTABLE = frozenset({"command", "task_reassign", "budget_exceeded", "tool_permission"})
MAX_DISCUSSION_MESSAGES = 200
COMMAND_APPROVAL_TTL = timedelta(hours=1)


def command_key(agent_id: str, cwd: str, command: str) -> str:
    return security.sha256_hex("\0".join(["command", agent_id, cwd, command]))


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["details"] = json.loads(out["details"])
    out.pop("command_key", None)
    return out


def discussion(conn: sqlite3.Connection, approval_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.id, m.author, a.name AS author_name, m.body, m.ts FROM approval_messages m "
        "LEFT JOIN agents a ON a.id = m.author WHERE m.approval_id = ? ORDER BY m.id",
        (approval_id,),
    )
    return [dict(r) for r in rows]


def awaiting_reply(approval: dict[str, Any]) -> bool:
    """True when the operator's message is the latest one in a pending approval's thread."""
    thread = approval.get("discussion") or []
    return approval["status"] == "pending" and bool(thread) and thread[-1]["author"].startswith("human:")


def get(conn: sqlite3.Connection, approval_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT ap.*, a.name AS requester_name FROM approvals ap "
        "LEFT JOIN agents a ON a.id = ap.requester WHERE ap.id = ?",
        (approval_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"no approval {approval_id}")
    out = _row(row)
    out["discussion"] = discussion(conn, approval_id)
    out["awaiting_reply"] = awaiting_reply(out)
    return out


def post_discussion(
    conn: sqlite3.Connection, approval_id: int, *, author: str, body: str, device: str | None = None
) -> dict[str, Any]:
    """Add a message to an approval's thread.

    Only the operator (``human:*``) and the agent that filed the approval may
    take part, and only while the approval is still pending.
    """
    approval = get(conn, approval_id)
    if not author.startswith("human:") and author != approval["requester"]:
        raise Forbidden("only the operator and the requesting agent can discuss this approval")
    if approval["status"] != "pending":
        raise Conflict(f"approval {approval_id} is already {approval['status']}")
    if len(approval["discussion"]) >= MAX_DISCUSSION_MESSAGES:
        raise Conflict("discussion is full")
    cur = conn.execute(
        "INSERT INTO approval_messages (approval_id, author, body, ts) VALUES (?, ?, ?, ?)",
        (approval_id, author, body, now_iso()),
    )
    events.emit(
        conn, type="approval.discussed", actor=author, project=approval["project"], device=device,
        payload={"approval_id": approval_id, "message_id": cur.lastrowid, "requester": approval["requester"],
                 "body": body},
    )
    return get(conn, approval_id)


def list_for_agent(conn: sqlite3.Connection, agent_id: str, *, awaiting_reply_only: bool = False) -> list[dict]:
    rows = conn.execute(
        "SELECT id FROM approvals WHERE requester = ? AND status = 'pending' ORDER BY id", (agent_id,)
    ).fetchall()
    out = [get(conn, r["id"]) for r in rows]
    return [a for a in out if a["awaiting_reply"]] if awaiting_reply_only else out


def list_approvals(conn: sqlite3.Connection, *, status: str | None = "pending", limit: int = 100) -> list[dict]:
    sql = "SELECT ap.*, a.name AS requester_name FROM approvals ap LEFT JOIN agents a ON a.id = ap.requester "
    params: list[Any] = []
    if status is not None:
        sql += "WHERE ap.status = ? "
        params.append(status)
    sql += "ORDER BY ap.id DESC LIMIT ?"
    params.append(limit)
    rows = [_row(r) for r in conn.execute(sql, params)]
    for r in rows:
        r["discussion"] = discussion(conn, r["id"])
        r["awaiting_reply"] = awaiting_reply(r)
    return rows


def create(
    conn: sqlite3.Connection,
    *,
    kind: str,
    requester: str,
    details: dict[str, Any],
    project: str | None = None,
    reason: str = "",
    command_key: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise Invalid(f"unknown approval kind {kind!r}")
    cur = conn.execute(
        "INSERT INTO approvals (kind, project, requester, details, reason, status, created_at, command_key) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (kind, project, requester, json.dumps(details), reason, now_iso(), command_key),
    )
    approval_id = cur.lastrowid
    events.emit(
        conn, type="approval.requested", actor=requester, project=project, device=device,
        payload={"approval_id": approval_id, "kind": kind, "details": details, "reason": reason},
    )
    return get(conn, approval_id)


def request(
    conn: sqlite3.Connection,
    agent: Agent,
    *,
    kind: str,
    details: dict[str, Any],
    project: str | None,
    reason: str,
    device: str | None = None,
) -> dict[str, Any]:
    """Generic agent-filed approval (the request_approval MCP tool)."""
    if kind not in AGENT_REQUESTABLE:
        raise Forbidden(f"agents cannot request {kind!r} approvals directly")
    if kind == "command":
        command, cwd = details.get("command"), details.get("cwd")
        if not isinstance(command, str) or not isinstance(cwd, str):
            raise Invalid("command approvals need string 'command' and 'cwd'")
        return request_command(conn, agent, command=command, cwd=cwd, project=project, reason=reason, device=device)
    if kind == "task_reassign":
        task = tasks.get_task(conn, _int(details, "task_id"))
        if task["project"] not in agent.allowed_projects:
            raise Forbidden("no access to that task's project")
        project = task["project"]
        details = {"task_id": task["id"], "assignee": str(details.get("assignee") or "") or None}
    if kind in ("budget_exceeded", "tool_permission") and "task_id" in details:
        task = tasks.get_task(conn, _int(details, "task_id"))
        if task["assignee"] != agent.id:
            raise Forbidden("only the task's assignee can file this approval for it")
        project = task["project"]
        if kind == "budget_exceeded":
            limit = details.get("limit")
            if limit not in tasks.BUDGET_LIMITS:
                raise Invalid(f"details.limit must be one of {', '.join(tasks.BUDGET_LIMITS)}")
            # The hub, not the agent, decides how much an approval extends the budget.
            details = {"task_id": task["id"], "limit": limit, "current": task["budget"][limit],
                       "used": tasks.usage(conn, task["id"]), "extend_to": tasks.budget_extension(task, limit)}
    elif kind == "budget_exceeded":
        raise Invalid("budget_exceeded approvals need details.task_id")
    if kind == "tool_permission":
        tool = details.get("tool_name")
        if not isinstance(tool, str) or not tool:
            raise Invalid("tool_permission approvals need details.tool_name")
        details = {"tool_name": tool, "input": details.get("input"), "task_id": details.get("task_id")}
    if project is not None and project not in agent.allowed_projects:
        raise Forbidden(f"no access to project {project!r}")
    return create(conn, kind=kind, requester=agent.id, details=details, project=project, reason=reason,
                  device=device)


def _int(details: dict[str, Any], key: str) -> int:
    value = details.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise Invalid(f"details.{key} must be an integer")
    return value


def request_command(
    conn: sqlite3.Connection,
    agent: Agent,
    *,
    command: str,
    cwd: str,
    project: str | None = None,
    reason: str = "",
    device: str | None = None,
) -> dict[str, Any]:
    """File (or reuse a pending) approval for one exact command in one directory."""
    if project is not None and project not in agent.allowed_projects:
        project = None  # a hint only; never grants anything
    key = command_key(agent.id, cwd, command)
    row = conn.execute(
        "SELECT id FROM approvals WHERE command_key = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    if row:
        return get(conn, row["id"])
    return create(
        conn, kind="command", requester=agent.id, project=project, reason=reason,
        details={"command": command, "cwd": cwd}, command_key=key, device=device,
    )


def consume_command(
    conn: sqlite3.Connection, agent: Agent, *, command: str, cwd: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """Atomically use up one approved, unexpired approval for exactly this command."""
    return _consume(conn, agent, "command", command_key(agent.id, cwd, command), now)


def tool_key(agent_id: str, task_id: int | None, tool_name: str, tool_input: Any) -> str:
    canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    return security.sha256_hex("\0".join(["tool", agent_id, str(task_id), tool_name, canonical]))


def request_tool_permission(
    conn: sqlite3.Connection,
    agent: Agent,
    *,
    task_id: int,
    tool_name: str,
    tool_input: Any,
    reason: str = "",
    device: str | None = None,
) -> dict[str, Any]:
    """File (or reuse) a live permission request from a headless task session."""
    task = tasks.get_task(conn, task_id)
    if task["assignee"] != agent.id:
        raise Forbidden("only the task's assignee can request permissions for it")
    key = tool_key(agent.id, task_id, tool_name, tool_input)
    row = conn.execute(
        "SELECT id FROM approvals WHERE command_key = ? AND status = 'pending' ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    if row:
        return get(conn, row["id"])
    return create(
        conn, kind="tool_permission", requester=agent.id, project=task["project"], reason=reason, device=device,
        details={"task_id": task_id, "tool_name": tool_name, "input": tool_input}, command_key=key,
    )


def consume_tool_permission(
    conn: sqlite3.Connection, agent: Agent, *, task_id: int, tool_name: str, tool_input: Any,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    return _consume(conn, agent, "tool_permission", tool_key(agent.id, task_id, tool_name, tool_input), now)


def _consume(
    conn: sqlite3.Connection, agent: Agent, kind: str, key: str, now: datetime | None
) -> dict[str, Any] | None:
    now = now or datetime.now(UTC)
    oldest_valid = now_iso(now - COMMAND_APPROVAL_TTL)
    row = conn.execute(
        "UPDATE approvals SET consumed_at = ? WHERE id = ("
        "  SELECT id FROM approvals WHERE command_key = ? AND requester = ? AND kind = ? "
        "  AND status = 'approved' AND consumed_at IS NULL AND decided_at > ? ORDER BY id LIMIT 1"
        ") AND consumed_at IS NULL RETURNING id",
        (now_iso(now), key, agent.id, kind, oldest_valid),
    ).fetchone()
    if row is None:
        return None
    events.emit(conn, type="approval.consumed", actor=agent.id, payload={"approval_id": row["id"]})
    return get(conn, row["id"])


def decide(
    conn: sqlite3.Connection, approval_id: int, *, approve: bool, actor: str, note: str = ""
) -> dict[str, Any]:
    """Operator decision. Applies side effects in the caller's transaction."""
    if not actor.startswith("human:"):
        raise Forbidden("only a human operator can decide approvals")
    approval = get(conn, approval_id)
    status = "approved" if approve else "rejected"
    cur = conn.execute(
        "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? "
        "WHERE id = ? AND status = 'pending'",
        (status, actor, now_iso(), note, approval_id),
    )
    if cur.rowcount == 0:
        raise Conflict(f"approval {approval_id} is already {approval['status']}")
    events.emit(
        conn, type="approval.decided", actor=actor, project=approval["project"],
        payload={"approval_id": approval_id, "kind": approval["kind"], "status": status, "note": note},
    )
    _apply(conn, approval, approve, actor)
    return get(conn, approval_id)


def _apply(conn: sqlite3.Connection, approval: dict[str, Any], approve: bool, actor: str) -> None:
    kind, details = approval["kind"], approval["details"]
    if kind == "task_create":
        task = tasks.get_task(conn, details["task_id"])
        if task["state"] == "awaiting_approval":
            tasks.transition(conn, task["id"], "open" if approve else "rejected", actor=actor, role="approval")
    elif kind == "task_reassign" and approve:
        tasks.assign(conn, details["task_id"], details.get("assignee"), actor=actor)
    elif kind == "agent_enrollment":
        agent = store.find_agent(conn, details["agent_id"])
        if agent.status == "pending":
            store.set_agent_status(conn, agent.id, "active" if approve else "revoked", actor=actor)
    elif kind == "budget_exceeded" and approve:
        tasks.extend_budget(conn, details["task_id"], details["limit"], details["extend_to"], actor=actor)
    # command, tool_permission, rejected budgets: the requester acts on the decision itself.


def agent_create_task(
    conn: sqlite3.Connection,
    agent: Agent,
    *,
    project: str,
    title: str,
    body: str,
    assignee: str | None = None,
    budget: dict[str, Any] | None = None,
    reason: str = "",
    budget_defaults: dict[str, Any] | None = None,
    device: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Agent-created tasks always land in awaiting_approval with a task_create approval."""
    task = tasks.create_task(
        conn, actor=agent.id, project=project, title=title, body=body, assignee=assignee,
        budget=budget, budget_defaults=budget_defaults, state="awaiting_approval", device=device,
    )
    approval = create(
        conn, kind="task_create", requester=agent.id, project=project, reason=reason, device=device,
        details={"task_id": task["id"], "title": title, "assignee": task["assignee"], "budget": task["budget"]},
    )
    conn.execute("UPDATE tasks SET approval_id = ? WHERE id = ?", (approval["id"], task["id"]))
    return tasks.get_task(conn, task["id"]), approval


def pending_enrollment(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id FROM approvals WHERE kind = 'agent_enrollment' AND status = 'pending' "
        "AND json_extract(details, '$.agent_id') = ?",
        (agent_id,),
    ).fetchone()
    return get(conn, row["id"]) if row else None


def set_agent_status(conn: sqlite3.Connection, id_or_name: str, status: str, *, actor: str, note: str = "") -> Agent:
    """Approve or revoke an agent, resolving its pending enrollment approval if any.

    Use this rather than :func:`store.set_agent_status` so the approval queue
    never keeps a stale enrollment request.
    """
    agent = store.find_agent(conn, id_or_name)
    pending = pending_enrollment(conn, agent.id)
    if pending:
        decide(conn, pending["id"], approve=status == "active", actor=actor, note=note)
    else:
        store.set_agent_status(conn, agent.id, status, actor=actor)
    return store.find_agent(conn, agent.id)
