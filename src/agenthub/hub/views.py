"""Read-side queries for the operator pages: dicts in, dicts out, no HTTP."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agenthub.hub import avatars, private

TERMINAL_STATES = ("done", "rejected", "cancelled")


def agent_rows(conn: sqlite3.Connection, *, agent_id: str | None = None) -> list[dict[str, Any]]:
    """Every non-revoked agent with its device, liveness and open tasks, ordered by name.

    Shared by the Dashboard, Agents and Devices pages. ``avatar`` is the agent's
    :func:`avatars.board` entry. ``checkouts`` (project -> path) and ``current_dir`` are
    agent-written text the runner reported. With ``agent_id`` the result is that one agent
    (revoked or not), or empty if there is none.
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
    cols = "id, name, status, device_name, device_node_id, connected, last_heartbeat, checkouts, current_dir FROM agents "
    rows = (conn.execute("SELECT " + cols + "WHERE id = ?", (agent_id,)) if agent_id is not None
            else conn.execute("SELECT " + cols + "WHERE status != 'revoked' ORDER BY name")).fetchall()
    return [{"id": r["id"], "name": r["name"], "status": r["status"], "device": r["device_name"], "device_node_id": r["device_node_id"],
             "connected": bool(r["connected"]), "last_heartbeat": r["last_heartbeat"],
             "checkouts": json.loads(r["checkouts"] or "{}"), "current_dir": r["current_dir"],
             "tasks": open_tasks.get(r["id"], []), "avatar": board.get(r["id"])} for r in rows]


def devices(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Machines that host a non-revoked agent, one entry per ``device_node_id``.

    ``name`` is the device name of the agent that heartbeat most recently; ``online`` is true
    if any agent on it is connected; ``last_seen`` is the latest heartbeat. Online devices
    come first, then by name.
    """
    by_node: dict[str, list[dict[str, Any]]] = {}
    for a in agent_rows(conn):
        by_node.setdefault(a["device_node_id"], []).append(a)
    out = []
    for node_id, agents in by_node.items():
        latest = max(agents, key=lambda a: a["last_heartbeat"] or "")
        out.append({"node_id": node_id, "name": latest["device"], "online": any(a["connected"] for a in agents),
                    "last_seen": latest["last_heartbeat"], "agents": agents})
    return sorted(out, key=lambda d: (not d["online"], d["name"].lower(), d["node_id"]))


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


def chat_list(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """The Chats page: non-revoked agents that have a private chat, newest message first, then the rest.

    Each ``threads`` entry has the agent's ``id``, ``name``, ``avatar``, ``unread`` (its messages the
    operator hasn't read) and ``last_ts`` / ``last_author`` of the newest message; never message text.
    """
    board, unread = avatars.board(conn), private.unread_counts(conn)
    last = {r["agent_id"]: r for r in conn.execute(
        "SELECT p.agent_id, p.id, p.ts, p.author FROM private_messages p "
        "JOIN (SELECT agent_id, MAX(id) AS id FROM private_messages GROUP BY agent_id) m ON m.id = p.id")}
    threads, others = [], []
    for r in conn.execute("SELECT id, name, status FROM agents WHERE status != 'revoked' ORDER BY name"):
        row = {"id": r["id"], "name": r["name"], "status": r["status"], "avatar": board.get(r["id"]),
               "unread": unread.get(r["id"], 0)}
        if r["id"] in last:
            threads.append(row | {"last_ts": last[r["id"]]["ts"], "last_author": last[r["id"]]["author"],
                                  "last_id": last[r["id"]]["id"]})
        else:
            others.append(row)
    threads.sort(key=lambda t: t["last_id"], reverse=True)
    return {"threads": threads, "others": others}
