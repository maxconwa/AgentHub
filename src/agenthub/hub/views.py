"""Read-side queries for the operator pages: dicts in, dicts out, no HTTP."""

from __future__ import annotations

import sqlite3
from typing import Any

from agenthub.hub import avatars

TERMINAL_STATES = ("done", "rejected", "cancelled")


def agent_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every non-revoked agent with its device, liveness and open tasks, ordered by name.

    Shared by the Dashboard, Agents and Devices pages. ``avatar`` is the agent's
    :func:`avatars.board` entry.
    """
    board = avatars.board(conn)
    open_tasks: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
            "SELECT ta.agent_id, t.id, t.title, t.project, t.state, t.created_by, ta.role, ta.stage FROM task_agents ta "
            "JOIN tasks t ON t.id = ta.task_id WHERE t.state NOT IN ('done', 'rejected', 'cancelled') "
            "ORDER BY t.id"):
        open_tasks.setdefault(r["agent_id"], []).append(
            {"id": r["id"], "title": r["title"], "project": r["project"], "state": r["state"],
             "role": r["role"], "stage": r["stage"], "created_by": r["created_by"]})
    rows = conn.execute(
        "SELECT id, name, status, device_name, connected, last_heartbeat FROM agents "
        "WHERE status != 'revoked' ORDER BY name").fetchall()
    return [{"id": r["id"], "name": r["name"], "status": r["status"], "device": r["device_name"],
             "connected": bool(r["connected"]), "last_heartbeat": r["last_heartbeat"],
             "tasks": open_tasks.get(r["id"], []), "avatar": board.get(r["id"])} for r in rows]


def project_tree(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Projects that have any task, each with its live tasks and the agents on them.

    Terminal tasks are left out of ``tasks``. Projects with no live task come after the
    others, so the page can collapse them.
    """
    board = avatars.board(conn)
    members: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(
            "SELECT ta.task_id, ta.agent_id, a.name, ta.role, ta.stage, a.device_name FROM task_agents ta "
            "JOIN agents a ON a.id = ta.agent_id ORDER BY ta.added_at, ta.rowid"):
        members.setdefault(r["task_id"], []).append(
            {"agent_id": r["agent_id"], "name": r["name"], "role": r["role"], "stage": r["stage"],
             "device": r["device_name"], "avatar": board.get(r["agent_id"])})
    by_project: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
            "SELECT id, project, title, state, created_by FROM tasks ORDER BY id"):
        live = by_project.setdefault(r["project"], [])
        if r["state"] not in TERMINAL_STATES:
            live.append({"id": r["id"], "title": r["title"], "state": r["state"], "created_by": r["created_by"],
                         "agents": members.get(r["id"], [])})
    out = [{"name": p["name"], "description": p["description"], "tasks": by_project[p["name"]]}
           for p in conn.execute("SELECT name, description FROM projects ORDER BY name") if p["name"] in by_project]
    return [p for p in out if p["tasks"]] + [p for p in out if not p["tasks"]]
