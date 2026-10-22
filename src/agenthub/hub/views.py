"""Read-side queries for the operator pages: dicts in, dicts out, no HTTP."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from agenthub.hub import approvals, avatars, device_board, private, questions, store, tasks

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
    """Every project, each with its live tasks and the agents on them.

    Terminal tasks are left out of ``tasks``. Projects with no live task (finished ones, or
    none yet) come after the others, so the page can collapse them.
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
    out = [{"name": p["name"], "description": p["description"], "tasks": by_project.get(p["name"], [])}
           for p in conn.execute("SELECT name, description FROM projects ORDER BY name")]
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


def prompt_state(conn: sqlite3.Connection, target: str, found: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a Prompt button to ``target`` needs: ``{target, reason}``, ``reason`` saying why it is off (no
    :func:`recipients` agent is connected), else None. ``found`` is that :func:`recipients` result if known."""
    agents = (found or recipients(conn, target))["agents"]
    if any(a["connected"] for a in agents):
        reason = None
    elif not agents:
        reason = "Prompt is off: there is no active agent here to wake."
    elif len(agents) == 1:
        reason = f"Prompt is off: {agents[0]['name']} is not connected right now."
    else:
        reason = "Prompt is off: none of the agents here is connected right now."
    return {"target": target, "reason": reason}


# --- Prompts ----------------------------------------------------------------------------------

PANEL_PROMPTS = 3  # a target's latest prompts in its panel
RECENT_PROMPTS = 5  # finished prompts on the Dashboard
OPEN_PROMPT_STATUSES = ("pending", "running", "discussing")


def prompt(conn: sqlite3.Connection, pid: int) -> dict[str, Any]:
    """:func:`prompts.get` for the pages, plus where its target and thread live: ``target_label``,
    ``target_item`` (its panel), ``target_href``, ``thread_href`` (None for a single prompt) and, on each
    recipient, ``task_by`` (who wrote its share task). Raises store.NotFound."""
    from agenthub.hub import prompts  # imported here: prompts imports this module

    p = prompts.get(conn, pid)
    kind, _, key = p["target"].partition(":")
    if kind == "agent":
        row = conn.execute("SELECT name FROM agents WHERE id = ?", (key,)).fetchone()
        label, href = f"agent {row['name'] if row else key}", f"/ui/agents/{key}"
    elif kind == "task":
        label, href = f"task #{key}", f"/ui/tasks/{key}"
    elif kind == "project":
        label, href = f"project {key}", f"/ui/projects/{quote(key, safe='')}"
    else:
        name = device_board.device_labels(conn).get(f"device:{key}", key)
        label, href = f"device {name}", f"/ui/devices/{quote(key, safe='')}"
    thread = p["thread"]
    if thread is None:
        thread_href = None
    elif thread.startswith("device:"):
        thread_href = f"/ui/devices/{quote(thread.removeprefix('device:'), safe='')}"
    else:
        thread_href = f"/ui/board/{thread.removeprefix('board:')}"
    for r in p["recipients"]:
        by = conn.execute("SELECT created_by FROM tasks WHERE id = ?", (r["task_id"],)).fetchone() \
            if r["task_id"] else None
        r["task_by"] = by["created_by"] if by else None
    return p | {"target_label": label, "target_item": p["target"], "target_href": href, "thread_href": thread_href}


def prompts_for(conn: sqlite3.Connection, target: str, limit: int = PANEL_PROMPTS) -> list[dict[str, Any]]:
    """The newest ``limit`` prompts sent to ``target``, newest first (as :func:`prompt`)."""
    rows = conn.execute("SELECT id FROM prompts WHERE target = ? ORDER BY id DESC LIMIT ?", (target, limit))
    return [prompt(conn, r["id"]) for r in rows.fetchall()]


def prompt_overview(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """The Dashboard's Prompts section: ``open`` (pending, running or discussing; newest first) and ``recent``,
    the :data:`RECENT_PROMPTS` latest finished (done or failed) ones."""
    marks = ",".join("?" * len(OPEN_PROMPT_STATUSES))
    live = conn.execute(f"SELECT id FROM prompts WHERE status IN ({marks}) ORDER BY id DESC",
                        OPEN_PROMPT_STATUSES).fetchall()
    done = conn.execute(f"SELECT id FROM prompts WHERE status NOT IN ({marks}) ORDER BY id DESC LIMIT ?",
                        (*OPEN_PROMPT_STATUSES, RECENT_PROMPTS)).fetchall()
    return {"open": [prompt(conn, r["id"]) for r in live], "recent": [prompt(conn, r["id"]) for r in done]}


# --- Board: public channels; Chats: agent-to-agent threads -----------------------------------

BOARD_LIMIT = 200
RECENT_AUTHORS_WINDOW = device_board.RECENT_AUTHORS_WINDOW
ACTIVITY_LINK = "/ui/activity"
_TASK_CHANNEL = re.compile(r"task-(\d+)")
_PAIR_KEY = re.compile(r"pair-(ag[0-9a-f]+)-(ag[0-9a-f]+)")
_TASK_BOARD = re.compile(r"task-([1-9][0-9]{0,17})")  # a task board's channel as tasks name it (no leading zeros)
# The messages an ask and its answer post into the ``asks`` channel; a pair thread shows the ask itself.
_ASK_ECHO = "NOT (c.name = 'asks' AND (m.body LIKE '[ask #%' OR m.body LIKE '[answer to ask #%'))"


def _ts_key(item: dict[str, Any]) -> str:
    return item["last_ts"] or ""


def agent_pairs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The Chats page's "Between agents" rows: one per pair of agents that have addressed each other.

    Built from addressed agent-to-agent messages plus asks, newest first. Each item is
    ``{key, title, last_body, last_ts, avatars, agents}``; ``last_body`` is always None (agent-written
    snippets stay out of lists). Keys are ``pair-<a>-<b>`` with the agent ids sorted.
    """
    board = avatars.board(conn)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")}
    pairs: dict[tuple[str, str], str] = {}
    # Addressed messages walk the partial index messages_to_agent, not every message.
    for r in conn.execute(
            f"SELECT m.author, m.to_agent, MAX(m.ts) AS ts FROM messages m JOIN channels c ON c.id = m.channel_id "
            f"WHERE m.to_agent IS NOT NULL AND {_ASK_ECHO} GROUP BY m.author, m.to_agent "
            f"UNION ALL SELECT from_agent, to_agent, MAX(created_at) FROM asks GROUP BY from_agent, to_agent"):
        if r["author"] in names and r["to_agent"] in names and r["author"] != r["to_agent"]:
            k = tuple(sorted((r["author"], r["to_agent"])))
            pairs[k] = max(pairs.get(k, ""), r["ts"])
    out = [{"key": f"pair-{x}-{y}", "title": f"{names[x]} ↔ {names[y]}", "last_body": None, "last_ts": ts,
            "avatars": [board[i] for i in (x, y) if i in board],
            "agents": [{"id": i, "name": names[i]} for i in (x, y)]} for (x, y), ts in pairs.items()]
    return sorted(out, key=_ts_key, reverse=True)


def board_index(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """The Board page's thread list: project boards, task boards, other channels, device boards.

    Each item is ``{key, title, created_by, last_body, last_ts, avatars}`` newest first. ``last_body`` is always
    None: agent-written snippets stay out of lists, so a row shows who spoke last and when. Device items
    carry ``href`` (their device page) and ``node_id``.
    """
    board = avatars.board(conn)
    task_titles = {(r["project"], r["id"]): (r["title"], r["created_by"])
                   for r in conn.execute("SELECT id, project, title, created_by FROM tasks")}
    out: dict[str, list[dict[str, Any]]] = {"projects": [], "tasks": [], "threads": []}
    for c in conn.execute("SELECT id, project, name, created_by FROM channels").fetchall():
        # The channel's latest posts, one short range of messages_channel_id: when, and who (faces of the last few).
        recent = conn.execute("SELECT author, ts FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
                              (c["id"], RECENT_AUTHORS_WINDOW)).fetchall()
        authors = dict.fromkeys(r["author"] for r in recent)
        item = {"key": f"ch-{c['project']}~{c['name']}", "created_by": c["created_by"], "title": f"{c['project']} / {c['name']}", "last_body": None,
                "last_ts": recent[0]["ts"] if recent else None, "project": c["project"],
                "avatars": [board[a] for a in authors if a in board][:3]}
        m = _TASK_CHANNEL.fullmatch(c["name"])
        if c["name"] == "general":
            out["projects"].append(item | {"title": c["project"]})
        elif m and (c["project"], int(m[1])) in task_titles:
            title, by = task_titles[(c["project"], int(m[1]))]
            out["tasks"].append(item | {"title": f"#{m[1]} {title}", "created_by": by, "task_id": int(m[1])})
        else:
            out["threads"].append(item)
    machines = devices(conn)
    posts = device_board.last_posts(conn, [d["node_id"] for d in machines])
    out["devices"] = []
    for d in machines:  # every machine with a non-revoked agent, so the operator can start its board
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


def _unstarted_board(conn: sqlite3.Connection, key: str) -> tuple[str, str] | None:
    """The (project, channel) of a project's or task's board nobody has posted on yet (its channel doesn't exist)."""
    project, sep, channel = key.removeprefix("ch-").partition("~")
    if not key.startswith("ch-") or not sep:
        return None
    m = _TASK_BOARD.fullmatch(channel)
    if channel == "general":
        found = conn.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone()
    elif m:
        found = conn.execute("SELECT 1 FROM tasks WHERE id = ? AND project = ?", (int(m[1]), project)).fetchone()
    else:
        found = None
    return (project, channel) if found else None


def board_thread(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    """One Board thread: ``{title, messages, can_post, project, channel, created_by}``.

    Keys are ``ch-<project>~<channel>``. A project's ``general`` board and a task's ``task-<id>`` board
    resolve before anyone has posted (empty; the first post creates the channel). Raises store.NotFound if unknown.
    """
    found = _channel_for_key(conn, key)
    if found:
        project, channel, created_by = found
        msgs = [{"author": m["author"], "ts": m["ts"], "body": m["body"]}
                for m in store.recent_messages(conn, project=project, channel=channel, limit=BOARD_LIMIT)]
        return {"title": f"{project} / {channel}", "messages": msgs, "can_post": True,
                "project": project, "channel": channel, "created_by": created_by}
    unstarted = _unstarted_board(conn, key)
    if unstarted:
        project, channel = unstarted
        return {"title": f"{project} / {channel}", "messages": [], "can_post": True,
                "project": project, "channel": channel, "created_by": None}
    raise store.NotFound(f"no board thread {key!r}")


def pair_thread(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    """One agent-to-agent chat, ``pair-<a>-<b>`` (agent ids sorted): ``{title, messages, can_post}``.

    Addressed messages both ways plus their asks and answers, read-only. Raises store.NotFound if unknown.
    """

    m = _PAIR_KEY.fullmatch(key)
    if not m or m[1] >= m[2]:
        raise store.NotFound(f"no agent chat {key!r}")
    x, y = m[1], m[2]
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents WHERE id IN (?, ?)", (x, y))}
    if len(names) != 2:
        raise store.NotFound(f"no agent chat {key!r}")
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


# --- Slide-in panel ---------------------------------------------------------------------------

PANEL_LIMIT = 50  # messages in a panel's chat; the full page shows more
_PANEL_ITEM = re.compile(r"(agent|task|project|device|approval|question):(.+)")
_ID = re.compile(r"[0-9]{1,18}")


def panel(conn: sqlite3.Connection, item: str) -> dict[str, Any]:
    """What the slide-in panel shows for ``item`` (``<kind>:<id>``): ``{kind, href, ...}``.

    ``href`` is the item's full page. agent: ``ag`` (an :func:`agent_rows` entry, revoked or not),
    its pending ``approvals`` and ``questions`` and its private ``chat``; task: ``t`` and its board
    ``messages``; project: ``project``, its live ``tasks`` and board ``messages``; device: ``d`` and
    its board ``messages``; approval: ``a`` and its ``task``; question: ``q``, its ``task`` and the
    ``answer_prompt`` that carried the answer. Agent, task, project and device panels also carry
    ``prompt`` and ``prompts`` (:func:`_prompting`). Raises store.NotFound for a malformed or unknown item.
    """
    m = _PANEL_ITEM.fullmatch(item)
    if not m:
        raise store.NotFound(f"no panel item {item!r}")
    kind, key = m[1], m[2]
    if kind in ("task", "approval", "question") and not _ID.fullmatch(key):
        raise store.NotFound(f"no {kind} {key!r}")
    if kind == "agent":
        rows = agent_rows(conn, agent_id=key)
        if not rows:
            raise store.NotFound(f"no agent {key!r}")
        return {"kind": kind, "href": f"/ui/agents/{key}", "ag": rows[0],
                "approvals": approvals.list_for_agent(conn, key),
                "questions": [q for q in questions.pending(conn) if q["agent_id"] == key],
                "chat": private.thread(conn, key, limit=PANEL_LIMIT)} | _prompting(conn, item)
    if kind == "task":
        t = tasks.get_task(conn, int(key))
        return {"kind": kind, "href": f"/ui/tasks/{t['id']}", "t": t,
                "messages": store.recent_messages(conn, project=t["project"], channel=f"task-{t['id']}",
                                                  limit=PANEL_LIMIT)} | _prompting(conn, f"task:{t['id']}")
    if kind == "project":
        row = conn.execute("SELECT name, description FROM projects WHERE name = ?", (key,)).fetchone()
        if row is None:
            raise store.NotFound(f"no project {key!r}")
        live = next((p["tasks"] for p in project_tree(conn) if p["name"] == key), [])
        return {"kind": kind, "href": f"/ui/projects/{quote(key, safe='')}", "project": dict(row), "tasks": live,
                "messages": store.recent_messages(conn, project=key, channel="general", limit=PANEL_LIMIT)} \
            | _prompting(conn, item)
    if kind == "device":
        d = device(conn, key)
        return {"kind": kind, "href": f"/ui/devices/{quote(key, safe='')}", "d": d,
                "messages": device_messages(conn, key)[-PANEL_LIMIT:]} | _prompting(conn, item)
    if kind == "approval":
        a = approvals.get(conn, int(key))
        task_id = a["details"].get("task_id")
        return {"kind": kind, "href": f"/ui/approvals/{a['id']}", "a": a,
                "task": tasks.get_task(conn, task_id) if isinstance(task_id, int) else None}
    q = questions.get(conn, int(key))
    return {"kind": kind, "href": f"/ui/questions/{q['id']}", "q": q,
            "task": tasks.get_task(conn, q["task_id"]) if q["task_id"] else None,
            "answer_prompt": answer_prompt(conn, q)}


def _prompting(conn: sqlite3.Connection, target: str, limit: int = PANEL_PROMPTS) -> dict[str, Any]:
    """A panel's or conversation's Prompt parts: ``prompt`` (:func:`prompt_state`) and ``prompts``, the target's
    latest ``limit`` (:func:`prompts_for`)."""
    return {"prompt": prompt_state(conn, target), "prompts": prompts_for(conn, target, limit)}


def answer_prompt(conn: sqlite3.Connection, q: dict[str, Any]) -> dict[str, Any] | None:
    """The prompt that carried the operator's answer to question ``q`` (:func:`questions.answer_prompt_id`), as
    :func:`prompt`, or None (unanswered, answered before prompts, or its agent was revoked)."""
    pid = questions.answer_prompt_id(conn, q)
    return prompt(conn, pid) if pid is not None else None


# --- Chat window ------------------------------------------------------------------------------

SNIPPET_CHARS = 200  # of the last private message in the chat window's list (the row clips it further)
_CONVERSATION = re.compile(r"(agent|pair|board|device):(.+)")
_PROMPT_THREAD = re.compile(r"prompt-[0-9]+")  # a group prompt's discussion channel


def messenger(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """The chat window's list: ``{agents, pairs, groups, devices}``, each newest first.

    Every row has ``key`` (``agent:<id>``, ``pair:<pair-key>``, ``board:<board key>``, ``device:<node_id>``, which
    :func:`conversation` opens), ``href`` (the full page) and ``last_ts``. agents: every non-revoked agent with
    ``avatar``, ``status``, ``unread`` (its unread private messages plus its pending questions) and ``last``,
    the newest private message (agent-written when its author isn't ``human:``). pairs: :func:`agent_pairs`.
    groups: every project's board and every task's board that is live or has posts, with ``kind``, ``item``
    (its panel), ``title``, ``created_by`` and ``members`` (avatars of the agents on it). devices: every machine
    with a non-revoked agent, ``members`` being its agents.
    """
    board = avatars.board(conn)
    unread = private.unread_counts(conn)
    for q in questions.pending(conn):
        unread[q["agent_id"]] = unread.get(q["agent_id"], 0) + 1
    last = {r["agent_id"]: dict(r) for r in conn.execute(
        "SELECT p.agent_id, p.id, p.ts, p.author, substr(p.body, 1, ?) AS body FROM private_messages p "
        "JOIN (SELECT agent_id, MAX(id) AS id FROM private_messages GROUP BY agent_id) m ON m.id = p.id",
        (SNIPPET_CHARS,))}
    agents = [{"key": f"agent:{a['id']}", "href": f"/ui/chats/{a['id']}", "id": a["id"], "name": a["name"],
               "status": a["status"], "avatar": a["avatar"], "unread": unread.get(a["id"], 0),
               "last": last.get(a["id"]), "last_ts": (last.get(a["id"]) or {}).get("ts")}
              for a in agent_rows(conn)]
    agents.sort(key=lambda a: (a["last"] or {}).get("id", 0), reverse=True)

    pairs = [p | {"key": f"pair:{p['key']}", "href": f"/ui/chats/pair/{p['key']}"} for p in agent_pairs(conn)]

    on_task: dict[int, list[str]] = {}
    for r in conn.execute("SELECT task_id, agent_id FROM task_agents ORDER BY added_at, rowid"):
        on_task.setdefault(r["task_id"], []).append(r["agent_id"])
    index = board_index(conn)
    posted = {i["key"]: i["last_ts"] for i in index["projects"] + index["tasks"]}
    groups, live_members = [], {}
    tasks_rows = conn.execute("SELECT id, project, title, state, created_by FROM tasks ORDER BY id").fetchall()
    for t in tasks_rows:
        if t["state"] not in TERMINAL_STATES:
            live_members.setdefault(t["project"], []).extend(on_task.get(t["id"], []))
    for p in conn.execute("SELECT name FROM projects ORDER BY name"):
        key = f"ch-{p['name']}~general"
        groups.append({"key": f"board:{key}", "href": f"/ui/board/{key}", "kind": "project",
                       "item": f"project:{p['name']}", "title": p["name"], "created_by": None,
                       "last_ts": posted.get(key), "members": _faces(board, live_members.get(p["name"], []))})
    for t in tasks_rows:
        key = f"ch-{t['project']}~task-{t['id']}"
        if t["state"] in TERMINAL_STATES and key not in posted:
            continue
        groups.append({"key": f"board:{key}", "href": f"/ui/board/{key}", "kind": "task", "item": f"task:{t['id']}",
                       "title": f"#{t['id']} {t['title']}", "created_by": t["created_by"], "last_ts": posted.get(key),
                       "members": _faces(board, on_task.get(t["id"], []))})
    groups.sort(key=_ts_key, reverse=True)

    machines = devices(conn)
    posts = device_board.last_posts(conn, [d["node_id"] for d in machines])
    devs = [{"key": f"device:{d['node_id']}", "href": f"/ui/devices/{quote(d['node_id'], safe='')}",
             "title": d["name"], "last_ts": posts.get(d["node_id"], {}).get("ts"),
             "members": _faces(board, [a["id"] for a in d["agents"]])} for d in machines]
    devs.sort(key=_ts_key, reverse=True)
    return {"agents": agents, "pairs": pairs, "groups": groups, "devices": devs}


def _faces(board: dict[str, dict[str, Any]], agent_ids: list[str], n: int = 3) -> list[dict[str, Any]]:
    """The avatars of the first ``n`` distinct non-revoked agents in ``agent_ids``."""
    return [board[i] for i in dict.fromkeys(agent_ids) if i in board][:n]


def conversation(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    """One conversation in the chat window: ``{kind, key, title, messages, action, href, item, ...}``.

    ``action`` is where its form posts (None when read-only), ``href`` the full page and ``item`` the slide-in
    panel its header opens (None for agent-to-agent chats and other threads). agent: ``ag`` (revoked agents
    are read-only) and its pending ``questions``; pair: read-only; board: ``created_by``; device: ``d``.
    Agent, device and board conversations (but not a prompt's own discussion thread) carry ``prompt`` and
    their target's latest prompt in ``prompts`` (:func:`_prompting`). Raises store.NotFound for a malformed or
    unknown key.
    """
    m = _CONVERSATION.fullmatch(key)
    if not m:
        raise store.NotFound(f"no conversation {key!r}")
    kind, ref = m[1], m[2]
    if kind == "agent":
        rows = agent_rows(conn, agent_id=ref)
        if not rows:
            raise store.NotFound(f"no agent {ref!r}")
        ag = rows[0]
        return {"kind": kind, "key": key, "title": ag["name"], "ag": ag, "messages": private.thread(conn, ref),
                "questions": [q for q in questions.pending(conn) if q["agent_id"] == ref],
                "action": f"/ui/chats/{ref}" if ag["status"] != "revoked" else None,
                "href": f"/ui/agents/{ref}", "item": f"agent:{ref}"} | _prompting(conn, f"agent:{ref}", 1)
    if kind == "pair":
        thread = pair_thread(conn, ref)
        x, y = _PAIR_KEY.fullmatch(ref).groups()
        return {"kind": kind, "key": key, "title": thread["title"], "messages": thread["messages"], "action": None,
                "href": f"/ui/chats/pair/{ref}", "item": None, "members": _faces(avatars.board(conn), [x, y])}
    if kind == "board":
        thread = board_thread(conn, ref)
        project, channel = thread["project"], thread["channel"]
        out = {"kind": kind, "key": key, "messages": thread["messages"], "action": f"/ui/board/{ref}",
               "title": thread["title"], "created_by": thread["created_by"], "href": f"/ui/board/{ref}", "item": None}
        m = _TASK_BOARD.fullmatch(channel)
        t = (conn.execute("SELECT id, title, created_by FROM tasks WHERE id = ? AND project = ?",
                          (int(m[1]), project)).fetchone() if m else None)
        if channel == "general":
            out |= {"title": project, "href": f"/ui/projects/{quote(project, safe='')}", "item": f"project:{project}"}
        elif t:  # the title and its "by" label are the task's
            out |= {"title": f"#{t['id']} {t['title']}", "created_by": t["created_by"], "href": f"/ui/tasks/{t['id']}",
                    "item": f"task:{t['id']}"}
        # Prompt wakes the board's task or project; a prompt's own discussion thread has none.
        if not _PROMPT_THREAD.fullmatch(channel):
            out |= _prompting(conn, f"task:{t['id']}" if t else f"project:{project}", 1)
        return out
    d = device(conn, ref)
    return {"kind": kind, "key": key, "title": d["name"], "d": d, "messages": device_messages(conn, ref),
            "action": f"/ui/devices/{quote(ref, safe='')}/notes", "href": f"/ui/devices/{quote(ref, safe='')}",
            "item": f"device:{ref}", "members": _faces(avatars.board(conn), [a["id"] for a in d["agents"]])} \
        | _prompting(conn, key, 1)
