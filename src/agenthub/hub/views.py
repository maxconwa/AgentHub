"""Read-side queries for the operator pages: dicts in, dicts out, no HTTP."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from agenthub.hub import approvals, avatars, chats, questions, store, tasks

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
    cols = ("id, name, status, device_name, device_node_id, connected, last_heartbeat, checkouts, current_dir, "
            "prompt_support, chat_support, model, effort FROM agents ")
    rows = (conn.execute("SELECT " + cols + "WHERE id = ?", (agent_id,)) if agent_id is not None
            else conn.execute("SELECT " + cols + "WHERE status != 'revoked' ORDER BY name")).fetchall()
    return [{"id": r["id"], "name": r["name"], "status": r["status"], "device": r["device_name"], "device_node_id": r["device_node_id"],
             "connected": bool(r["connected"]), "prompts": bool(r["prompt_support"]), "chat": bool(r["chat_support"]),
             "last_heartbeat": r["last_heartbeat"], "model": r["model"], "effort": r["effort"],
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
        # Every agent allowed into the project, not only those on its open tasks: a new project has no tasks yet,
        # and prompting it is how its agents split the work into some. An agent on a task whose project access
        # was since withdrawn can't act on the project, so it isn't reached either.
        ids = [r["id"] for r in conn.execute(
            "SELECT a.id FROM agents a, json_each(a.allowed_projects) p WHERE p.value = ?", (key,))]
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


def wakeable(agent: dict[str, Any]) -> bool:
    """Whether a prompt wakes this :func:`agent_rows` agent: connected, with a runner that runs prompts."""
    return agent["connected"] and agent["prompts"]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# --- Slide-in panel ---------------------------------------------------------------------------

_PANEL_ITEM = re.compile(r"(agent|task|project|device|approval|question):(.+)")
_ID = re.compile(r"[0-9]{1,18}")


def panel(conn: sqlite3.Connection, item: str) -> dict[str, Any]:
    """What the slide-in panel shows for ``item`` (``<kind>:<id>``): ``{kind, href, ...}``.

    ``href`` is the item's full page. An agent's, task's or project's panel is its chat, ``c`` (:func:`chat`), under
    a few facts. agent: ``ag`` (an :func:`agent_rows` entry, revoked or not); task: ``t``; project: ``project`` and
    its live ``tasks``; device: ``d`` (no chat: devices have none); approval: ``a`` and its ``task``; question: ``q``
    and its ``task``. Raises store.NotFound for a malformed or unknown item.
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
        return {"kind": kind, "href": f"/ui/agents/{key}", "ag": rows[0], "c": chat(conn, "agent", key)}
    if kind == "task":
        t = tasks.get_task(conn, int(key))
        return {"kind": kind, "href": f"/ui/tasks/{t['id']}", "t": t, "c": chat(conn, "task", str(t["id"]))}
    if kind == "project":
        row = conn.execute("SELECT name, description FROM projects WHERE name = ?", (key,)).fetchone()
        if row is None:
            raise store.NotFound(f"no project {key!r}")
        live = next((p["tasks"] for p in project_tree(conn) if p["name"] == key), [])
        return {"kind": kind, "href": f"/ui/projects/{quote(key, safe='')}", "project": dict(row), "tasks": live,
                "c": chat(conn, "project", key)}
    if kind == "device":
        return {"kind": kind, "href": f"/ui/devices/{quote(key, safe='')}", "d": device(conn, key)}
    if kind == "approval":
        a = approvals.get(conn, int(key))
        task_id = a["details"].get("task_id")
        return {"kind": kind, "href": f"/ui/approvals/{a['id']}", "a": a,
                "task": tasks.get_task(conn, task_id) if isinstance(task_id, int) else None}
    q = questions.get(conn, int(key))
    return {"kind": kind, "href": f"/ui/questions/{q['id']}", "q": q,
            "task": tasks.get_task(conn, q["task_id"]) if q["task_id"] else None}


# --- The chat list: the Chats page and the Chat button ------------------------------------------

SNIPPET_CHARS = 200  # of a chat's last text in the list (the row clips it further)


def _ts_key(item: dict[str, Any]) -> str:
    return item.get("last_ts") or ""


def _last_text(conn: sqlite3.Connection, kind: str, ref: str) -> dict[str, Any] | None:
    """A chat's newest row that says something (text, or a card), for the list: ``{author, body, ts}``."""
    for m in reversed(chats.messages(conn, kind, ref, limit=20)):
        if m["kind"] == "text":
            return {"author": m["author"], "body": m["body"][:SNIPPET_CHARS], "ts": m["ts"]}
        if m["kind"] in ("approval", "question"):
            return {"author": m["author"], "body": f"asks for your {'approval' if m['kind'] == 'approval' else 'answer'}",
                    "ts": m["ts"], "card": True}
    return None


def chat_index(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Every chat for the list: ``{agents, tasks, projects}``, each newest first.

    Every row has ``item`` (the panel that is its chat), ``href`` (its page), ``title``, ``last`` (:func:`_last_text`,
    agent-written unless its author is ``human:``), ``last_ts`` and ``unread`` (agent messages the operator hasn't
    seen). agents (not revoked): ``avatar``, ``status`` and
    ``waiting`` (its pending questions); tasks (live ones, and finished ones that have a chat): ``state``,
    ``created_by`` and ``members`` (avatars); projects: ``members``, the agents on its live tasks.
    """
    board = avatars.board(conn)
    unread = {(o["kind"], str(o["ref"])): o["unread"] or 0 for o in chats.overview(conn)}
    waiting: dict[str, int] = {}
    for q in questions.pending(conn):
        waiting[q["agent_id"]] = waiting.get(q["agent_id"], 0) + 1
    agents = []
    for a in agent_rows(conn):
        last = _last_text(conn, "agent", a["id"])
        agents.append({"item": f"agent:{a['id']}", "href": f"/ui/agents/{a['id']}", "id": a["id"], "title": a["name"],
                       "status": a["status"], "avatar": a["avatar"], "waiting": waiting.get(a["id"], 0),
                       "last": last, "last_ts": last and last["ts"], "unread": unread.get(("agent", a["id"]), 0)})
    on_task: dict[int, list[str]] = {}
    for r in conn.execute("SELECT task_id, agent_id FROM task_agents ORDER BY added_at, rowid"):
        on_task.setdefault(r["task_id"], []).append(r["agent_id"])
    task_rows, live_members = [], {}
    for t in conn.execute("SELECT id, project, title, state, created_by FROM tasks ORDER BY id"):
        live = t["state"] not in TERMINAL_STATES
        if live:
            live_members.setdefault(t["project"], []).extend(on_task.get(t["id"], []))
        last = _last_text(conn, "task", str(t["id"]))
        if not live and last is None:
            continue
        task_rows.append({"item": f"task:{t['id']}", "href": f"/ui/tasks/{t['id']}", "title": f"#{t['id']} {t['title']}",
                          "state": t["state"], "created_by": t["created_by"], "last": last, "last_ts": last and last["ts"],
                          "unread": unread.get(("task", str(t["id"])), 0), "members": _faces(board, on_task.get(t["id"], []))})
    projects = []
    for p in conn.execute("SELECT name FROM projects ORDER BY name"):
        last = _last_text(conn, "project", p["name"])
        projects.append({"item": f"project:{p['name']}", "href": f"/ui/projects/{quote(p['name'], safe='')}",
                         "title": p["name"], "last": last, "last_ts": last and last["ts"],
                         "unread": unread.get(("project", p["name"]), 0), "members": _faces(board, live_members.get(p["name"], []))})
    for rows in (agents, task_rows, projects):
        rows.sort(key=_ts_key, reverse=True)
    return {"agents": agents, "tasks": task_rows, "projects": projects}


def _faces(board: dict[str, dict[str, Any]], agent_ids: list[str], n: int = 3) -> list[dict[str, Any]]:
    """The avatars of the first ``n`` distinct non-revoked agents in ``agent_ids``."""
    return [board[i] for i in dict.fromkeys(agent_ids) if i in board][:n]


# --- Chats ------------------------------------------------------------------------------------

CHAT_ROWS = 300  # the newest rows a chat shows (the log scrolls)
CHAT_MAX_CHARS = 8000
_CHAT_REF = {"agent": re.compile(r"ag[0-9a-f]{1,64}"), "task": re.compile(r"[1-9][0-9]{0,17}"),
             "project": store.NAME_RE}


def _too_old(conn: sqlite3.Connection, agent_ids: list[str]) -> str:
    """Why some of a chat's agents won't get a message for now: connected with a runner older than 0.3.8, which
    doesn't say ``"chat": 1`` in its heartbeat (the message waits for it, as for an offline agent). Empty if none."""
    if not agent_ids:
        return ""
    old = [r["name"] for r in conn.execute(
        f"SELECT name FROM agents WHERE id IN ({','.join('?' * len(agent_ids))}) AND status = 'active' "
        "AND connected = 1 AND chat_support = 0 ORDER BY name", agent_ids)]
    if not old:
        return ""
    whose = f"{old[0]}'s runner is" if len(old) == 1 else f"The runners of {_and(old)} are"
    return (f"{whose} too old for chat (before 0.3.8): upgrade {'it' if len(old) == 1 else 'them'}. "
            "Until then your messages wait for {}.".format("it" if len(old) == 1 else "them"))


def _names(members: list[dict[str, Any]]) -> str:
    return _and(sorted(m["name"] for m in members)) if members else "nobody yet"


def chat(conn: sqlite3.Connection, kind: str, ref: str, *, after: int = 0, rows: bool = True) -> dict[str, Any]:
    """One chat (an agent's, a task's or a project's) for _chat.html: ``{id, kind, ref, title, url, page, item, rows,
    send, readonly, hint, placeholder, empty, max_chars}``.

    ``too_old`` says which connected agents run a runner too old for chat (:func:`_too_old`). ``rows`` are
    :func:`chat_rows`, oldest first (only those after ``after`` when given; none with ``rows=False``,
    which only checks the chat and says who it reaches). ``send`` says whether its message box shows; when it
    doesn't, ``readonly`` says why. ``hint`` says whom a message reaches. Raises store.NotFound for an unknown or
    malformed chat.
    """
    pattern = _CHAT_REF.get(kind)
    if pattern is None or not pattern.fullmatch(ref):
        raise store.NotFound(f"no chat {kind}:{ref}")
    c = chats.chat(conn, kind, ref)
    out: dict[str, Any] = {"id": c["id"], "kind": kind, "ref": ref, "url": f"/ui/chat/{kind}/{quote(ref, safe='')}",
                           "item": f"{kind}:{ref}", "max_chars": CHAT_MAX_CHARS, "send": True, "readonly": "",
                           "too_old": _too_old(conn, [m["agent_id"] for m in c["members"]])}
    if kind == "agent":
        status = conn.execute("SELECT status FROM agents WHERE id = ?", (ref,)).fetchone()["status"]
        name = c["title"]
        out |= {"title": name, "page": f"/ui/agents/{ref}", "placeholder": f"Message {name}…",
                "empty": f"No messages with {name} yet. A message wakes it up.",
                "hint": f"Wakes {name} if it's idle; if it's busy on a task, your message goes into that session."}
        if status == "revoked":
            out |= {"send": False, "readonly": f"{name} is revoked, so this chat is read-only."}
        elif status == "pending":
            out |= {"send": False, "readonly": f"{name} is waiting for your approval, so it can't take messages yet."}
    elif kind == "task":
        state = conn.execute("SELECT state FROM tasks WHERE id = ?", (int(ref),)).fetchone()["state"]
        out |= {"title": f"task #{ref}", "page": f"/ui/tasks/{ref}", "placeholder": f"Message task #{ref}…",
                "empty": "No messages on this task yet.",
                "hint": f"Goes to everyone on task #{ref}: {_names(c['members'])}."}
        if state in TERMINAL_STATES:
            out |= {"send": False, "readonly": f"Task #{ref} is {state}, so its chat is read-only."}
    else:
        out |= {"title": ref, "page": f"/ui/projects/{quote(ref, safe='')}", "placeholder": f"Message everyone in {ref}…",
                "empty": f"No messages in {ref} yet.",
                "hint": f"Goes to every agent in {ref}: {_names(c['members'])}. They split it into tasks among themselves."}
    out["rows"] = chat_rows(conn, chats.messages(conn, kind, ref, after_id=after, limit=CHAT_ROWS)) if rows else []
    return out


def chat_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat rows as _chat.html draws them: a card row gets ``card``, the approval or question as it is now (so
    deciding on the Approvals page shows on the card too), or None when it's gone; a took_task row gets
    ``task`` ({id, title}) when the task exists; a reply gets ``reply`` ({id, same_chat, question_id}) when the row
    it answers exists."""
    out = []
    for m in rows:
        m = dict(m)
        meta = m.get("meta") or {}
        m["meta"] = meta
        try:
            if m["kind"] == "approval" and isinstance(meta.get("approval_id"), int):
                m["card"] = approvals.get(conn, meta["approval_id"])
            elif m["kind"] == "question" and isinstance(meta.get("question_id"), int):
                m["card"] = questions.get(conn, meta["question_id"])
        except store.NotFound:
            m["card"] = None
        if isinstance(meta.get("reply_to"), int):
            try:
                to = chats.get_message(conn, meta["reply_to"])
                m["reply"] = {"id": to["id"], "same_chat": to["chat_id"] == m.get("chat_id"),
                              "question_id": (to["meta"] or {}).get("question_id") if to["kind"] == "question" else None}
            except store.NotFound:
                m["reply"] = None
        if m["kind"] == "took_task" and isinstance(meta.get("task_id"), int):
            t = conn.execute("SELECT id, title FROM tasks WHERE id = ?", (meta["task_id"],)).fetchone()
            m["task"] = dict(t) if t else None
        out.append(m)
    return out


def chat_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    """One :func:`chat_rows` row by id (it carries its chat's ``chat_kind`` and ``chat_ref``); raises store.NotFound."""
    return chat_rows(conn, [chats.get_message(conn, message_id)])[0]
