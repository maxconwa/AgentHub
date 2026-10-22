"""Prompts: the operator wakes agents now.

A prompt goes to the recipients of a message target (:func:`views.recipients`). Recipients
that aren't connected are kept as ``skipped`` rows so the operator sees who missed it. One
live recipient makes a ``single`` prompt: its runner runs a session on it. Several make a
``group`` prompt: they discuss it as equals in one thread (the project channel
``prompt-<id>``, or the device board for a device target), and each claims a share (a new,
already approved task) or declines. Undecided recipients time out after
``prompt_discussion_minutes`` (:func:`expire`).

Prompt status: ``pending`` -> ``running`` (single, when its recipient starts) or
``discussing`` (group, from creation) -> ``done`` once no recipient is ``pending`` or
``running``; ``failed`` when nobody was live, or the single recipient finished not ok.

Only a human operator creates prompts; no agent route does. ``prompt.*`` events carry ids,
statuses and agent ids, never the body.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from agenthub.hub import device_board, events, private, store, task_agents, tasks, views
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound

MAX_BODY_CHARS = 8000
DEFAULT_DISCUSSION_MINUTES = 30
OPEN = ("pending", "running")  # recipient statuses that keep a prompt going
FINISHED = ("done", "failed")  # prompt statuses
SYSTEM = "system:hub"


def _labeled(pid: int, body: str, limit: int) -> str:
    """``Prompt #N: <body>``, cut to ``limit`` characters with a pointer to the full text."""
    text = f"Prompt #{pid}: {body}"
    if len(text) > limit:
        note = f"… [truncated; full prompt: call prompt_status with prompt_id={pid}]"
        text = text[: limit - len(note)] + note
    return text


def _thread(key: str | None) -> dict[str, Any] | None:
    """A prompt's thread key (a chat-window conversation key) as the parts an agent posts to."""
    if key is None:
        return None
    if key.startswith("device:"):
        return {"kind": "device", "node_id": key.removeprefix("device:"), "key": key}
    project, _, channel = key.removeprefix("board:ch-").partition("~")
    return {"kind": "channel", "project": project, "channel": channel, "key": key}


def _row(conn: sqlite3.Connection, pid: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM prompts WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"no prompt {pid}")
    return dict(row)


def _recipients(conn: sqlite3.Connection, pid: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT r.agent_id, a.name, a.device_name AS device, a.connected, r.status, r.task_id, t.title AS task_title, "
        "r.note, r.updated_at FROM prompt_recipients r JOIN agents a ON a.id = r.agent_id "
        "LEFT JOIN tasks t ON t.id = r.task_id WHERE r.prompt_id = ? ORDER BY a.name, a.id", (pid,))]


def get(conn: sqlite3.Connection, pid: int) -> dict[str, Any]:
    """The operator's view: the prompt with every recipient's status, note and share task."""
    p = _row(conn, pid)
    recipients = _recipients(conn, pid)
    for r in recipients:
        r["connected"] = bool(r["connected"])
    return p | {"recipients": recipients, "thread_parts": _thread(p["thread"])}


def _own(conn: sqlite3.Connection, pid: int, agent: Agent) -> tuple[dict[str, Any], dict[str, Any]]:
    """The prompt and ``agent``'s recipient row, or NotFound when it isn't a recipient."""
    row = conn.execute("SELECT * FROM prompt_recipients WHERE prompt_id = ? AND agent_id = ?",
                       (pid, agent.id)).fetchone()
    if row is None:
        raise NotFound(f"no prompt {pid}")
    return _row(conn, pid), dict(row)


def get_for_agent(conn: sqlite3.Connection, pid: int, agent: Agent) -> dict[str, Any]:
    """A recipient's view: the body, mode, thread, its own status, and every recipient's name, status,
    device and current tasks (only tasks in projects ``agent`` may see). NotFound for non-recipients."""
    p, mine = _own(conn, pid, agent)
    rows = {a["id"]: a for a in views.agent_rows(conn)}
    recipients = []
    for r in _recipients(conn, pid):
        live = rows.get(r["agent_id"], {"tasks": []})
        recipients.append({
            "agent_id": r["agent_id"], "name": r["name"], "status": r["status"], "device": r["device"],
            "connected": bool(r["connected"]), "task_id": r["task_id"],
            "tasks": [{"id": t["id"], "title": t["title"], "project": t["project"], "state": t["state"]}
                      for t in live["tasks"] if t["project"] in agent.allowed_projects]})
    return {"id": p["id"], "target": p["target"], "body": p["body"], "mode": p["mode"], "status": p["status"],
            "project": p["project"], "thread": _thread(p["thread"]), "created_by": p["created_by"],
            "created_at": p["created_at"], "finished_at": p["finished_at"], "my_status": mine["status"],
            "my_task_id": mine["task_id"], "recipients": recipients}


def pending_for(conn: sqlite3.Connection, agent: Agent) -> list[dict[str, Any]]:
    """Prompts ``agent`` still has to act on (``pending``, or ``running`` to resume after a restart), oldest first."""
    rows = conn.execute(
        "SELECT p.id, p.target, p.body, p.mode, p.status, p.project, p.thread, p.created_at, r.status AS my_status "
        "FROM prompt_recipients r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.agent_id = ? AND r.status IN ('pending', 'running') ORDER BY p.id", (agent.id,))
    return [dict(r) | {"thread": _thread(r["thread"])} for r in rows]


def _emit(conn: sqlite3.Connection, p: dict[str, Any], *, actor: str, **extra: Any) -> None:
    agents = [r["agent_id"] for r in conn.execute(
        "SELECT agent_id FROM prompt_recipients WHERE prompt_id = ? AND status != 'skipped' ORDER BY agent_id",
        (p["id"],))]
    events.emit(conn, type="prompt.updated", actor=actor, project=p["project"],
                payload={"prompt_id": p["id"], "mode": p["mode"], "status": p["status"], "agents": agents, **extra})


def _settle(conn: sqlite3.Connection, pid: int) -> dict[str, Any]:
    """Finish the prompt once no recipient is pending or running; returns the fresh prompt row."""
    p = _row(conn, pid)
    if p["status"] in FINISHED:
        return p
    statuses = [r["status"] for r in conn.execute("SELECT status FROM prompt_recipients WHERE prompt_id = ? "
                                                  "AND status != 'skipped'", (pid,))]
    if any(s in OPEN for s in statuses):
        return p
    status = "failed" if p["mode"] == "single" and statuses and statuses[0] in ("failed", "timed_out") else "done"
    conn.execute("UPDATE prompts SET status = ?, finished_at = ? WHERE id = ?", (status, now_iso(), pid))
    return _row(conn, pid)


def _set_recipient(conn: sqlite3.Connection, pid: int, agent_id: str, status: str, *, note: str | None = None,
                   task_id: int | None = None) -> None:
    """Compare-and-set an open recipient row; Conflict if it was already decided."""
    cur = conn.execute(
        "UPDATE prompt_recipients SET status = ?, note = COALESCE(?, note), task_id = COALESCE(?, task_id), "
        "updated_at = ? WHERE prompt_id = ? AND agent_id = ? AND status IN ('pending', 'running')",
        (status, note, task_id, now_iso(), pid, agent_id))
    if cur.rowcount == 0:
        current = conn.execute("SELECT status FROM prompt_recipients WHERE prompt_id = ? AND agent_id = ?",
                               (pid, agent_id)).fetchone()
        raise Conflict(f"you are already {current['status'] if current else 'gone'} on prompt {pid}")


def create(conn: sqlite3.Connection, *, target: str, body: str, actor: str) -> dict[str, Any]:
    """Record a prompt to ``target``'s recipients and wake the live ones (operator only)."""
    if not actor.startswith("human:"):
        raise Forbidden("only a human operator can prompt agents")
    body = body.strip()
    if not body or len(body) > MAX_BODY_CHARS:
        raise Invalid(f"a prompt must be 1 to {MAX_BODY_CHARS} characters")
    found = views.recipients(conn, target)
    live = [a for a in found["agents"] if a["connected"]]
    offline = [a for a in found["agents"] if not a["connected"]]
    kind, _, key = target.partition(":")
    project = found["board"][0] if found["board"] else None  # task and project targets
    mode = "group" if len(live) > 1 else "single"
    status = "failed" if not live else "discussing" if mode == "group" else "pending"
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO prompts (target, body, mode, project, status, created_by, created_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (target, body, mode, project, status, actor, ts, ts if not live else None))
    pid = cur.lastrowid
    for a in found["agents"]:
        conn.execute("INSERT INTO prompt_recipients (prompt_id, agent_id, status, note, updated_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (pid, a["id"], "pending" if a["connected"] else "skipped",
                      "" if a["connected"] else "offline", ts))
    if mode == "group":
        if kind == "device":
            device_board.post(conn, key, author=actor, body=_labeled(pid, body, device_board.MAX_BODY_CHARS))
            thread = f"device:{key}"
        else:
            assert project is not None
            store.post_message(conn, author=actor, project=project, channel=f"prompt-{pid}", body=body)
            thread = f"board:ch-{project}~prompt-{pid}"
        conn.execute("UPDATE prompts SET thread = ? WHERE id = ?", (thread, pid))
    for a in live:  # the agent's private chat shows the prompt too
        private.send(conn, a["id"], author=actor, body=_labeled(pid, body, private.MAX_BODY_CHARS))
    events.emit(conn, type="prompt.created", actor=actor, project=project,
                payload={"prompt_id": pid, "mode": mode, "status": status, "agents": [a["id"] for a in live],
                         "skipped": [a["id"] for a in offline]})
    return get(conn, pid)


def start(conn: sqlite3.Connection, pid: int, agent: Agent) -> dict[str, Any]:
    """``agent``'s runner starts its session on the prompt."""
    p, mine = _own(conn, pid, agent)
    if mine["status"] != "pending":
        raise Conflict(f"prompt {pid} is already {mine['status']} for you")
    _set_recipient(conn, pid, agent.id, "running")
    if p["mode"] == "single" and p["status"] == "pending":
        conn.execute("UPDATE prompts SET status = 'running' WHERE id = ? AND status = 'pending'", (pid,))
    _emit(conn, _row(conn, pid), actor=agent.id, agent_id=agent.id, recipient_status="running")
    return get_for_agent(conn, pid, agent)


def finish(conn: sqlite3.Connection, pid: int, agent: Agent, *, ok: bool, note: str = "") -> dict[str, Any]:
    """``agent``'s session on the prompt ended. A recipient that already claimed, declined or timed out
    keeps that status (the call changes nothing)."""
    _, mine = _own(conn, pid, agent)
    if mine["status"] not in OPEN:
        return get_for_agent(conn, pid, agent)
    status = "done" if ok else "failed"
    _set_recipient(conn, pid, agent.id, status, note=note)
    _emit(conn, _settle(conn, pid), actor=agent.id, agent_id=agent.id, recipient_status=status)
    return get_for_agent(conn, pid, agent)


def _share_project(conn: sqlite3.Connection, p: dict[str, Any], agent: Agent, project: str | None) -> str:
    """Where ``agent``'s share goes: the prompt's project, else (device prompts) ``project`` if given, else the
    project of its current task, else its first allowed project."""
    if p["project"] is not None:
        if project is not None and project != p["project"]:
            raise Invalid(f"prompt {p['id']} is about project {p['project']!r}")
        return p["project"]
    if project is not None:
        if project not in agent.allowed_projects:
            raise Forbidden(f"no access to project {project!r}")
        return project
    current = views.agent_rows(conn, agent_id=agent.id)
    for t in current[0]["tasks"] if current else []:
        if t["project"] in agent.allowed_projects:
            return t["project"]
    if not agent.allowed_projects:
        raise Invalid("you have no project to put a share in")
    return agent.allowed_projects[0]


def _decidable(conn: sqlite3.Connection, pid: int, agent: Agent) -> dict[str, Any]:
    p, mine = _own(conn, pid, agent)
    if p["mode"] != "group":
        raise Conflict(f"prompt {pid} has a single recipient; there are no shares to claim or decline")
    if mine["status"] not in OPEN:
        raise Conflict(f"you are already {mine['status']} on prompt {pid}")
    return p


def claim(
    conn: sqlite3.Connection, pid: int, agent: Agent, *, title: str, body: str = "", project: str | None = None,
) -> dict[str, Any]:
    """``agent`` takes a share of a group prompt: a new task, already approved (``open`` with no approval row, as the
    prompt came from the operator), written and created by ``agent`` (its title and body are agent text), with
    ``agent`` working on it in role ``Share of prompt #N``."""
    p = _decidable(conn, pid, agent)
    title = title.strip()
    if not title or len(title) > tasks.MAX_TITLE_CHARS:
        raise Invalid(f"title must be 1 to {tasks.MAX_TITLE_CHARS} characters")
    if len(body) > tasks.MAX_BODY_CHARS:
        raise Invalid(f"description must be at most {tasks.MAX_BODY_CHARS} characters")
    where = _share_project(conn, p, agent, project)
    task = tasks.create_task(conn, actor=agent.id, project=where, title=title, body=body, state="open",
                             prompt_id=pid)
    task = task_agents.add_agent(conn, task["id"], agent.id, role=f"Share of prompt #{pid}", stage="works",
                                 actor=agent.id)
    _set_recipient(conn, pid, agent.id, "claimed", task_id=task["id"])
    _emit(conn, _settle(conn, pid), actor=agent.id, agent_id=agent.id, recipient_status="claimed",
          task_id=task["id"])
    return task


def decline(conn: sqlite3.Connection, pid: int, agent: Agent, *, reason: str) -> dict[str, Any]:
    """``agent`` takes no share of a group prompt; ``reason`` is shown to the operator."""
    _decidable(conn, pid, agent)
    _set_recipient(conn, pid, agent.id, "declined", note=reason)
    _emit(conn, _settle(conn, pid), actor=agent.id, agent_id=agent.id, recipient_status="declined")
    return get_for_agent(conn, pid, agent)


def expire(conn: sqlite3.Connection, now: datetime, *, minutes: int = DEFAULT_DISCUSSION_MINUTES) -> list[int]:
    """Settle prompts nobody will act on, so nothing waits forever. Returns the prompts changed.

    Older than ``minutes``: a group discussion's undecided recipients, and a single prompt's recipient that never
    started and is disconnected, become ``timed_out`` (the single prompt fails). In any open prompt, recipients
    that were revoked fail, and so do running recipients whose agent has been disconnected for longer than
    ``minutes`` (its runner never came back to finish)."""
    cutoff = now_iso(now - timedelta(minutes=minutes))
    changed: list[int] = []
    stale = conn.execute(
        "SELECT DISTINCT p.id FROM prompts p JOIN prompt_recipients r ON r.prompt_id = p.id "
        "JOIN agents a ON a.id = r.agent_id WHERE p.status NOT IN ('done', 'failed') "
        "AND r.status IN ('pending', 'running') AND (a.status = 'revoked' OR (p.created_at < ? AND "
        "(p.mode = 'group' OR (r.status = 'pending' AND a.connected = 0))) OR (r.status = 'running' "
        "AND a.connected = 0 AND COALESCE(a.last_heartbeat, '') < ?)) ORDER BY p.id", (cutoff, cutoff)).fetchall()
    for (pid,) in stale:
        p = _row(conn, pid)
        open_rows = conn.execute(
            "SELECT r.agent_id, r.status, a.status AS agent_status, a.connected, a.last_heartbeat "
            "FROM prompt_recipients r "
            "JOIN agents a ON a.id = r.agent_id WHERE r.prompt_id = ? AND r.status IN ('pending', 'running') "
            "ORDER BY r.agent_id", (pid,)).fetchall()
        timed_out, failed = [], []
        old = p["created_at"] < cutoff
        for r in open_rows:
            if r["agent_status"] == "revoked":
                _set_recipient(conn, pid, r["agent_id"], "failed", note="agent revoked")
                failed.append(r["agent_id"])
            elif old and p["mode"] == "group":
                _set_recipient(conn, pid, r["agent_id"], "timed_out", note="discussion timed out")
                timed_out.append(r["agent_id"])
            elif old and r["status"] == "pending" and not r["connected"]:
                _set_recipient(conn, pid, r["agent_id"], "timed_out", note="offline; never started")
                timed_out.append(r["agent_id"])
            elif r["status"] == "running" and not r["connected"] and (r["last_heartbeat"] or "") < cutoff:
                _set_recipient(conn, pid, r["agent_id"], "failed", note="runner disconnected and did not come back")
                failed.append(r["agent_id"])
        if timed_out or failed:
            _emit(conn, _settle(conn, pid), actor=SYSTEM, timed_out=timed_out, failed=failed)
            changed.append(pid)
    return changed
