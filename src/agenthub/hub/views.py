"""Read-side queries for the operator pages: dicts in, dicts out, no HTTP."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from agenthub.hub import avatars, device_board, private, store

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


def device(conn: sqlite3.Connection, node_id: str) -> dict[str, Any]:
    """One :func:`devices` entry; raises store.NotFound if no non-revoked agent runs on ``node_id``."""
    found = next((d for d in devices(conn) if d["node_id"] == node_id), None)
    if found is None:
        raise store.NotFound(f"no device {node_id!r}")
    return found


def device_messages(conn: sqlite3.Connection, node_id: str) -> list[dict[str, Any]]:
    """The device board as :func:`board_thread` messages; a note addressed to one agent is labeled ``to <name>``."""
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")}
    return [{"author": m["author"], "ts": m["ts"], "body": m["body"],
             "label": f"to {names.get(m['to_agent'], m['to_agent'])}" if m["to_agent"] else None}
            for m in device_board.recent(conn, node_id, limit=BOARD_LIMIT)]


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


# --- Tap to message ------------------------------------------------------------------------


def recipients(conn: sqlite3.Connection, target: str) -> dict[str, Any]:
    """Who a message to ``target`` (``agent:<id>``, ``task:<id>``, ``project:<name>``, ``device:<node_id>``) reaches.

    ``{label, agents, board}``: ``agents`` are the :func:`agent_rows` entries with status ``active`` (offline ones
    included; they read it at their next session), ``board`` the ``(project, channel)`` a public post would go to,
    or None for agent and device targets. Raises store.NotFound for a malformed or unknown target.
    """
    kind, _, key = target.partition(":")
    pool = {a["id"]: a for a in agent_rows(conn)}
    board: tuple[str, str] | None = None
    if kind == "agent" and key:
        found = agent_rows(conn, agent_id=key)
        if not found:
            raise store.NotFound(f"no agent {key!r}")
        label, ids, pool = f"agent {found[0]['name']}", [key], {key: found[0]}  # revoked agents are filtered below
    elif kind == "task" and key.isdecimal():
        row = conn.execute("SELECT id, project FROM tasks WHERE id = ?", (int(key),)).fetchone()
        if row is None:
            raise store.NotFound(f"no task {key}")
        label, board = f"task #{row['id']}", (row["project"], f"task-{row['id']}")
        ids = [r["agent_id"] for r in conn.execute("SELECT agent_id FROM task_agents WHERE task_id = ?", (row["id"],))]
    elif kind == "project" and key:
        if not conn.execute("SELECT 1 FROM projects WHERE name = ?", (key,)).fetchone():
            raise store.NotFound(f"no project {key!r}")
        label, board = f"project {key}", (key, "general")
        ids = [r["agent_id"] for r in conn.execute(
            "SELECT DISTINCT ta.agent_id FROM task_agents ta JOIN tasks t ON t.id = ta.task_id "
            "WHERE t.project = ? AND t.state NOT IN ('done', 'rejected', 'cancelled')", (key,))]
    elif kind == "device" and key:
        device = next((d for d in devices(conn) if d["node_id"] == key), None)
        if device is None:
            raise store.NotFound(f"no device {key!r}")
        label, ids = f"device {device['name']}", [a["id"] for a in device["agents"]]
    else:
        raise store.NotFound(f"no message target {target!r}")
    agents = sorted((pool[i] for i in set(ids) if i in pool and pool[i]["status"] == "active"),
                    key=lambda a: (a["name"], a["id"]))
    return {"label": label, "agents": agents, "board": board}


# --- Board: public channels and agent-to-agent threads -----------------------------------

BOARD_LIMIT = 200
ACTIVITY_LINK = "/ui/feed"  # Task 11 moves "All activity"
_TASK_CHANNEL = re.compile(r"task-(\d+)")
_PAIR_KEY = re.compile(r"pair-(ag[0-9a-f]+)-(ag[0-9a-f]+)")
# The messages an ask and its answer post into the ``asks`` channel; a pair thread shows the ask itself.
_ASK_ECHO = "NOT (c.name = 'asks' AND (m.body LIKE '[ask #%' OR m.body LIKE '[answer to ask #%'))"


def _ts_key(item: dict[str, Any]) -> str:
    return item["last_ts"] or ""


def board_index(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """The Board page's thread list: project boards, task boards, other channels, agent pairs, device boards.

    Each item is ``{key, title, created_by, last_body, last_ts, avatars}`` newest first. ``last_body`` is always
    None: agent-written snippets stay out of lists, so a row shows who spoke last and when. Device items
    carry ``href`` (their device page) and ``node_id``.
    """
    board = avatars.board(conn)
    task_titles = {(r["project"], r["id"]): (r["title"], r["created_by"])
                   for r in conn.execute("SELECT id, project, title, created_by FROM tasks")}
    authors: dict[int, list[str]] = {}
    for r in conn.execute("SELECT channel_id, author FROM messages GROUP BY channel_id, author ORDER BY MAX(id) DESC"):
        authors.setdefault(r["channel_id"], []).append(r["author"])
    last = {r["channel_id"]: r["ts"] for r in conn.execute(
        "SELECT channel_id, ts FROM messages WHERE id IN (SELECT MAX(id) FROM messages GROUP BY channel_id)")}
    out: dict[str, list[dict[str, Any]]] = {"projects": [], "tasks": [], "threads": [], "between_agents": []}
    for c in conn.execute("SELECT id, project, name, created_by FROM channels"):
        item = {"key": f"ch-{c['project']}~{c['name']}", "created_by": c["created_by"], "title": f"{c['project']} / {c['name']}", "last_body": None,
                "last_ts": last.get(c["id"]), "project": c["project"],
                "avatars": [board[a] for a in authors.get(c["id"], []) if a in board][:3]}
        m = _TASK_CHANNEL.fullmatch(c["name"])
        if c["name"] == "general":
            out["projects"].append(item | {"title": c["project"]})
        elif m and (c["project"], int(m[1])) in task_titles:
            title, by = task_titles[(c["project"], int(m[1]))]
            out["tasks"].append(item | {"title": f"#{m[1]} {title}", "created_by": by, "task_id": int(m[1])})
        else:
            out["threads"].append(item)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")}
    pairs: dict[tuple[str, str], str] = {}
    for r in conn.execute(
            f"SELECT m.author, m.to_agent, m.ts FROM messages m JOIN channels c ON c.id = m.channel_id "
            f"WHERE m.to_agent IS NOT NULL AND {_ASK_ECHO} UNION ALL SELECT from_agent, to_agent, created_at FROM asks"):
        if r["author"] in names and r["to_agent"] in names and r["author"] != r["to_agent"]:
            k = tuple(sorted((r["author"], r["to_agent"])))
            pairs[k] = max(pairs.get(k, ""), r["ts"])
    for (x, y), ts in pairs.items():
        out["between_agents"].append({"key": f"pair-{x}-{y}", "title": f"{names[x]} ↔ {names[y]}", "last_body": None,
                                      "last_ts": ts, "avatars": [board[i] for i in (x, y) if i in board],
                                      "agents": [{"id": i, "name": names[i]} for i in (x, y)]})
    posts = device_board.last_posts(conn)
    out["devices"] = []
    for d in devices(conn):  # every machine with a non-revoked agent, so the operator can start its board
        last = posts.get(d["node_id"], {"ts": None, "authors": []})
        out["devices"].append({"key": f"dev-{d['node_id']}", "title": d["name"], "last_body": None,
                               "last_ts": last["ts"], "node_id": d["node_id"],
                               "href": f"/ui/devices/{quote(d['node_id'], safe='')}",
                               "avatars": [board[a] for a in last["authors"] if a in board][:3]})
    for g in out.values():
        g.sort(key=_ts_key, reverse=True)
    return out


def _channel_for_key(conn: sqlite3.Connection, key: str) -> tuple[str, str, str] | None:
    """The existing (project, channel, created_by) behind ``ch-<project>~<channel>``.

    ``~`` can't appear in a name, so the split is unambiguous (``-`` can).
    """
    project, sep, channel = key.removeprefix("ch-").partition("~")
    if not key.startswith("ch-") or not sep:
        return None
    row = conn.execute("SELECT created_by FROM channels WHERE project = ? AND name = ?", (project, channel)).fetchone()
    return (project, channel, row["created_by"]) if row else None


def board_thread(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    """One Board thread: ``{title, messages, can_post}`` (plus ``project``/``channel`` for channels).

    Channel keys are ``ch-<project>~<channel>``; pair keys ``pair-<a>-<b>`` (agent ids sorted) show
    addressed messages between two agents and their asks, read-only. Raises store.NotFound if unknown.
    """
    found = _channel_for_key(conn, key)
    if found:
        project, channel, created_by = found
        msgs = [{"author": m["author"], "ts": m["ts"], "body": m["body"]}
                for m in store.recent_messages(conn, project=project, channel=channel, limit=BOARD_LIMIT)]
        return {"title": f"{project} / {channel}", "messages": msgs, "can_post": True,
                "project": project, "channel": channel, "created_by": created_by}
    m = _PAIR_KEY.fullmatch(key)
    if not m or m[1] >= m[2]:
        raise store.NotFound(f"no board thread {key!r}")
    x, y = m[1], m[2]
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents WHERE id IN (?, ?)", (x, y))}
    if len(names) != 2:
        raise store.NotFound(f"no board thread {key!r}")
    entries: list[tuple[str, int, dict[str, Any]]] = []
    both = f"((m.author = ? AND m.to_agent = ?) OR (m.author = ? AND m.to_agent = ?))"
    for r in conn.execute(
            f"SELECT m.id, m.author, m.ts, m.body FROM messages m JOIN channels c ON c.id = m.channel_id "
            f"WHERE {both} AND {_ASK_ECHO} ORDER BY m.id DESC LIMIT ?", (x, y, y, x, BOARD_LIMIT)):
        entries.append((r["ts"], 0, {"author": r["author"], "ts": r["ts"], "body": r["body"]}))
    for r in conn.execute(
            "SELECT id, from_agent, to_agent, question, answer, created_at, answered_at FROM asks "
            "WHERE (from_agent = ? AND to_agent = ?) OR (from_agent = ? AND to_agent = ?) ORDER BY id DESC LIMIT ?",
            (x, y, y, x, BOARD_LIMIT)):
        entries.append((r["created_at"], 1, {"author": r["from_agent"], "ts": r["created_at"], "label": "ask",
                                             "body": r["question"]}))
        if r["answer"] is not None:
            entries.append((r["answered_at"], 2, {"author": r["to_agent"], "ts": r["answered_at"],
                                                  "label": "answer to ask", "body": r["answer"]}))
    entries.sort(key=lambda e: e[:2])
    return {"title": f"{names[x]} ↔ {names[y]}", "messages": [e[2] for e in entries[-BOARD_LIMIT:]], "can_post": False}
