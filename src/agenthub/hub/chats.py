"""Chats: how the operator and agents talk (0.3.8; they replace prompts, private chats and boards).

There is one chat per agent (the operator and that agent), one per task (the operator and every agent on it) and one
per project (the operator and every agent allowed into it); no device chats. A chat row is made the first time the
chat is used. Its messages (``chat_messages``) are, by ``kind``:

* ``text``: the operator's or an agent's words (Markdown). ``meta.to`` addresses one agent (everyone in the chat
  still sees it), ``meta.task_id`` marks text a task session streamed, ``meta.reply_to`` is a message id.
* ``tools``: one stretch of an agent's tool calls, as compact lines (``meta.lines``, ``meta.count``), updated in
  place while ``meta.open``.
* ``status``: a thin line (``meta.state``: :data:`STATES`) the UI words itself; ``meta.detail`` is agent text.
* ``approval`` / ``question``: a card for an approval or a question (``meta.approval_id`` / ``meta.question_id``);
  its live state is read from those tables, so deciding on the card or the Approvals page shows in both.
* ``took_task`` / ``sit_out``: an agent's answer to the operator's request in a project chat (``meta.request_id``):
  it took a pre-approved task (``meta.task_id``), or sits it out with a reason (``body``).

Routing: the operator's message (:func:`send`) gets one ``chat_deliveries`` row per recipient (an agent chat's
agent, a task's members, a project's allowed agents; never a revoked one). The runner claims each delivery
(:func:`claim_delivery`, compare-and-set) and puts the text into a running session, wakes the agent's chat
session, or queues it for the next turn; a session can also claim it through ``read_inbox``. Nothing times out.
Only a human operator writes operator text: no agent route reaches :func:`send`.

Events carry ids only (``chat.message``, ``chat.updated``, ``chat.deliver``), never a body. The full-text index holds
the text of task and project chats (so ``search_history`` finds it, for agents in that project), never of an agent's
own chat. The bus sends the operator's socket the full row; agents get ids.

:func:`backfill` copies 0.3.7's private chats, prompts, channel posts, approvals and questions into chats, keyed on
``legacy`` so it can run on every start and never copies a row twice. It reads those tables and never changes them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthub.hub import events
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound, find_agent, get_agent

KINDS = ("agent", "task", "project")
MAX_BODY_CHARS = 16_000
MAX_LINE_CHARS = 300
MAX_TOOL_LINES = 200  # kept per tools row; meta.count has the true total
MAX_DETAIL_CHARS = 500
MAX_STREAM_ITEMS = 50
MAX_TITLE_CHARS = 200
MAX_TASK_BODY_CHARS = 16_000
STATES = ("started", "working", "usage_limit", "approval_wait", "finished", "failed", "queued")
CLAIM_MODES = ("live", "queued", "woke", "failed")
SYSTEM = "system:hub"
# What reaches agents as mail (read_inbox, chat.message pushes): words and decisions, not tool lines or statuses.
MAIL_KINDS = ("text", "took_task", "sit_out")


def _is_operator(actor: str) -> bool:
    return actor.startswith("human:")


# --- chats -------------------------------------------------------------------------------------------------------


def _resolve(conn: sqlite3.Connection, kind: str, ref: str) -> tuple[str, str | None, str]:
    """``(ref, project, title)`` of an existing agent, task or project; the agent ref may be its name."""
    if kind == "agent":
        agent = find_agent(conn, ref)
        return agent.id, None, agent.name
    if kind == "task":
        if not str(ref).isdigit():
            raise NotFound(f"no task {ref!r}")
        row = conn.execute("SELECT id, project FROM tasks WHERE id = ?", (int(ref),)).fetchone()
        if row is None:
            raise NotFound(f"no task {ref}")
        return str(row["id"]), row["project"], f"task #{row['id']}"
    if kind == "project":
        if not conn.execute("SELECT 1 FROM projects WHERE name = ?", (ref,)).fetchone():
            raise NotFound(f"no project {ref!r}")
        return ref, ref, f"project {ref}"
    raise NotFound(f"no chat kind {kind!r}")


def _ensure(conn: sqlite3.Connection, kind: str, ref: str, project: str | None) -> int:
    conn.execute("INSERT INTO chats (kind, ref, project, created_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                 (kind, ref, project, now_iso()))
    return conn.execute("SELECT id FROM chats WHERE kind = ? AND ref = ?", (kind, ref)).fetchone()["id"]


def chat(conn: sqlite3.Connection, kind: str, ref: str) -> dict[str, Any]:
    """The chat of an agent (id or name), task (id) or project (name), made if it doesn't exist yet:
    ``{id, kind, ref, project, title, members: [{agent_id, name}]}``. NotFound for an unknown one."""
    ref, project, title = _resolve(conn, kind, str(ref))
    chat_id = _ensure(conn, kind, ref, project)
    ids = members(conn, kind, ref)
    names = _names(conn, ids)
    return {"id": chat_id, "kind": kind, "ref": ref, "project": project, "title": title,
            "members": [{"agent_id": i, "name": names.get(i, i)} for i in ids]}


def _chat_by_id(conn: sqlite3.Connection, chat_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        raise NotFound(f"no chat {chat_id}")
    return dict(row)


def members(conn: sqlite3.Connection, kind: str, ref: str, *, active_only: bool = True) -> list[str]:
    """The agents in a chat (not the operator): the agent; the task's members; the project's allowed agents.
    Revoked (and, with ``active_only``, pending) agents are left out."""
    statuses = ("active",) if active_only else ("active", "pending")
    marks = ",".join("?" * len(statuses))
    if kind == "agent":
        row = conn.execute(f"SELECT id FROM agents WHERE id = ? AND status IN ({marks})", (ref, *statuses)).fetchone()
        return [row["id"]] if row else []
    if kind == "task":
        rows = conn.execute(f"SELECT ta.agent_id FROM task_agents ta JOIN agents a ON a.id = ta.agent_id "
                            f"WHERE ta.task_id = ? AND a.status IN ({marks}) ORDER BY ta.added_at, ta.rowid",
                            (int(ref), *statuses))
        return [r["agent_id"] for r in rows]
    rows = conn.execute(f"SELECT id, allowed_projects FROM agents WHERE status IN ({marks}) ORDER BY created_at",
                        statuses)
    return [r["id"] for r in rows if ref in json.loads(r["allowed_projects"])]


def _names(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = conn.execute(f"SELECT id, name FROM agents WHERE id IN ({','.join('?' * len(ids))})", ids)
    return {r["id"]: r["name"] for r in rows}


def overview(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every chat with a message: ``{id, kind, ref, project, title, last_ts, unread}``, the latest first. ``unread``
    counts what agents and the hub wrote (not tool lines or statuses) past the operator's read mark."""
    rows = conn.execute(
        "SELECT c.id, c.kind, c.ref, c.project, MAX(m.created_at) AS last_ts, "
        "SUM(CASE WHEN m.id > c.operator_read_id AND m.author NOT LIKE 'human:%' "
        "AND m.kind IN ('text', 'took_task', 'sit_out', 'approval', 'question') THEN 1 ELSE 0 END) AS unread "
        "FROM chats c JOIN chat_messages m ON m.chat_id = c.id GROUP BY c.id ORDER BY last_ts DESC, c.id DESC")
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")}
    out = []
    for r in rows:
        title = (names.get(r["ref"], r["ref"]) if r["kind"] == "agent"
                 else f"task #{r['ref']}" if r["kind"] == "task" else f"project {r['ref']}")
        out.append(dict(r) | {"title": title})
    return out


def mark_read(conn: sqlite3.Connection, chat_id: int, upto_id: int) -> None:
    """The operator has seen the chat up to message ``upto_id``."""
    conn.execute("UPDATE chats SET operator_read_id = MAX(operator_read_id, ?) WHERE id = ?", (upto_id, chat_id))


def session(conn: sqlite3.Connection, agent: Agent) -> str | None:
    """The Claude session id the agent's runner keeps its chat in (None until it sets one)."""
    chat_id = _ensure(conn, "agent", agent.id, None)
    return conn.execute("SELECT session_id FROM chats WHERE id = ?", (chat_id,)).fetchone()["session_id"]


def set_session(conn: sqlite3.Connection, agent: Agent, session_id: str | None) -> None:
    chat_id = _ensure(conn, "agent", agent.id, None)
    conn.execute("UPDATE chats SET session_id = ? WHERE id = ?", (session_id, chat_id))


# --- messages ----------------------------------------------------------------------------------------------------

_SELECT = (
    "SELECT m.id, m.chat_id, c.kind AS chat_kind, c.ref AS chat_ref, m.author, a.name AS author_name, m.kind, "
    "m.body, m.meta, m.legacy, m.created_at AS ts, m.updated_at FROM chat_messages m "
    "JOIN chats c ON c.id = m.chat_id LEFT JOIN agents a ON a.id = m.author "
)


def _rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        row = dict(r)
        row["meta"] = json.loads(row["meta"])
        out.append(row)
    operator = [m["id"] for m in out if m["kind"] == "text" and _is_operator(m["author"])]
    if operator:
        found: dict[int, list[dict[str, Any]]] = {}
        for d in conn.execute(
                "SELECT d.message_id, d.agent_id, a.name, d.status, d.mode, d.decision, d.task_id, d.note "
                f"FROM chat_deliveries d JOIN agents a ON a.id = d.agent_id WHERE d.message_id IN "
                f"({','.join('?' * len(operator))}) ORDER BY d.id", operator):
            found.setdefault(d["message_id"], []).append({k: d[k] for k in d.keys() if k != "message_id"})
        for m in out:
            if m["id"] in found or m["id"] in operator:
                m["deliveries"] = found.get(m["id"], [])
    return out


def get_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    rows = _rows(conn, conn.execute(_SELECT + "WHERE m.id = ?", (message_id,)).fetchall())
    if not rows:
        raise NotFound(f"no chat message {message_id}")
    return rows[0]


def messages(
    conn: sqlite3.Connection, kind: str, ref: str, *, after_id: int = 0, before_id: int | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """A chat's messages, oldest first: those with an id above ``after_id`` (new since a client's last look), else
    the ``limit`` latest (before message ``before_id``, to page back)."""
    chat_id = chat(conn, kind, ref)["id"]
    limit = max(1, min(limit, 1000))
    if after_id:
        rows = conn.execute(_SELECT + "WHERE m.chat_id = ? AND m.id > ? ORDER BY m.created_at, m.id LIMIT ?",
                            (chat_id, after_id, limit)).fetchall()
        return _rows(conn, rows)
    where, params = "WHERE m.chat_id = ? ", [chat_id]
    if before_id is not None:
        edge = conn.execute("SELECT created_at, id FROM chat_messages WHERE id = ? AND chat_id = ?",
                            (before_id, chat_id)).fetchone()
        if edge is None:
            raise NotFound(f"no message {before_id} in this chat")
        where += "AND (m.created_at < ? OR (m.created_at = ? AND m.id < ?)) "
        params += [edge["created_at"], edge["created_at"], edge["id"]]
    rows = conn.execute(_SELECT + where + "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
                        [*params, limit]).fetchall()
    return _rows(conn, list(reversed(rows)))


def _emit(conn: sqlite3.Connection, etype: str, chat_row: dict[str, Any], message_id: int, kind: str, *,
          actor: str, device: str | None = None, index_text: str | None = None, **extra: Any) -> None:
    events.emit(conn, type=etype, actor=actor, project=chat_row["project"], device=device, index_text=index_text,
                payload={"chat_id": chat_row["id"], "chat_kind": chat_row["kind"], "chat_ref": chat_row["ref"],
                         "message_id": message_id, "kind": kind, **extra})


def _insert(
    conn: sqlite3.Connection, chat_row: dict[str, Any], *, author: str, kind: str, body: str = "",
    meta: dict[str, Any] | None = None, device: str | None = None, legacy: str | None = None,
) -> int:
    ts = now_iso()
    meta = meta or {}
    cur = conn.execute("INSERT INTO chat_messages (chat_id, author, kind, body, meta, legacy, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (chat_row["id"], author, kind, body, json.dumps(meta, separators=(",", ":")), legacy, ts, ts))
    extra: dict[str, Any] = {"to": meta["to"]} if meta.get("to") else {}
    if meta.get("source"):
        extra["source"] = meta["source"]
    # What agents write in task and project chats is searchable (search_history) by the agents in that project;
    # an agent's own chat with the operator never is.
    index = body if chat_row["kind"] != "agent" and kind in ("text", "sit_out") else None
    _emit(conn, "chat.message", chat_row, cur.lastrowid, kind, actor=author, device=device, index_text=index,
          **extra)
    return cur.lastrowid


def _update_meta(conn: sqlite3.Connection, chat_row: dict[str, Any], message_id: int, meta: dict[str, Any], *,
                 actor: str, kind: str) -> None:
    conn.execute("UPDATE chat_messages SET meta = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(meta, separators=(",", ":")), now_iso(), message_id))
    _emit(conn, "chat.updated", chat_row, message_id, kind, actor=actor)


def _touch(conn: sqlite3.Connection, message_id: int, *, actor: str) -> None:
    """Tell the operator's view a message changed (e.g. its deliveries)."""
    row = conn.execute("SELECT chat_id, kind FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    conn.execute("UPDATE chat_messages SET updated_at = ? WHERE id = ?", (now_iso(), message_id))
    _emit(conn, "chat.updated", _chat_by_id(conn, row["chat_id"]), message_id, row["kind"], actor=actor)


def _check_body(body: str, limit: int = MAX_BODY_CHARS) -> str:
    if not isinstance(body, str) or not body.strip() or len(body) > limit:
        raise Invalid(f"a chat message must be 1 to {limit} characters")
    return body


def _close_stretches(conn: sqlite3.Connection, chat_row: dict[str, Any], *, author: str | None = None) -> None:
    """Close the open tool stretches in the chat (only ``author``'s, when given)."""
    sql = "SELECT id, author, meta FROM chat_messages WHERE chat_id = ? AND kind = 'tools' " \
          "AND json_extract(meta, '$.open') = 1"
    params: list[Any] = [chat_row["id"]]
    if author is not None:
        sql += " AND author = ?"
        params.append(author)
    for r in conn.execute(sql, params).fetchall():
        meta = json.loads(r["meta"]) | {"open": False}
        _update_meta(conn, chat_row, r["id"], meta, actor=r["author"], kind="tools")


def send(
    conn: sqlite3.Connection, kind: str, ref: str, body: str, actor: str, *, reply_to: int | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The operator writes in a chat: the message is stored and delivered to every agent in it (a pending
    ``chat_deliveries`` row and a ``chat.deliver`` event each). Forbidden for an agent and in a revoked agent's
    chat (read-only); a chat with no live agent still keeps the message."""
    if not _is_operator(actor):
        raise Forbidden("only a human operator can send operator messages")
    _check_body(body)
    if kind == "agent":
        agent = find_agent(conn, ref)
        if agent.status == "revoked":
            raise Forbidden(f"agent {agent.name!r} is revoked; its chat is read-only")
    chat_row = chat(conn, kind, ref)
    meta = dict(meta or {})
    if reply_to is not None:
        if not conn.execute("SELECT 1 FROM chat_messages WHERE id = ? AND chat_id = ?",
                            (reply_to, chat_row["id"])).fetchone():
            raise NotFound(f"no message {reply_to} in this chat")
        meta["reply_to"] = reply_to
    _close_stretches(conn, chat_row)  # tool calls after this message start a new stretch below it
    message_id = _insert(conn, chat_row, author=actor, kind="text", body=body, meta=meta)
    ts = now_iso()
    for agent_id in members(conn, chat_row["kind"], chat_row["ref"]):
        cur = conn.execute("INSERT INTO chat_deliveries (message_id, agent_id, status, created_at) "
                           "VALUES (?, ?, 'pending', ?)", (message_id, agent_id, ts))
        _emit(conn, "chat.deliver", chat_row, message_id, "text", actor=actor, delivery_id=cur.lastrowid,
              agent_id=agent_id)
    return get_message(conn, message_id)


def _member_chat(conn: sqlite3.Connection, agent: Agent, kind: str, ref: str) -> dict[str, Any]:
    """A chat ``agent`` is in (its own, ``me``; a task it is on; a project it may use). NotFound for any other, so an
    agent learns nothing about chats it isn't in."""
    if kind == "agent":
        if ref not in ("me", agent.id, agent.name):
            raise NotFound(f"no chat agent/{ref}")
        return chat(conn, "agent", agent.id)
    if kind == "task":
        if not str(ref).isdigit() or not conn.execute(
                "SELECT 1 FROM task_agents WHERE task_id = ? AND agent_id = ?", (int(ref), agent.id)).fetchone():
            raise NotFound(f"no chat task/{ref} you are in")
        return chat(conn, "task", ref)
    if kind == "project":
        if ref not in agent.allowed_projects:
            raise NotFound(f"no chat project/{ref} you are in")
        return chat(conn, "project", ref)
    raise NotFound(f"no chat kind {kind!r}")


def parse_chat(spec: str) -> tuple[str, str]:
    """``me`` / ``agent``, ``task:<id>``, ``project:<name>`` (as agents name chats) -> ``(kind, ref)``."""
    spec = spec.strip()
    if spec in ("me", "agent", "operator"):
        return "agent", "me"
    kind, sep, ref = spec.partition(":")
    if not sep or kind not in KINDS or not ref:
        raise Invalid(f"name a chat as 'me', 'task:<id>' or 'project:<name>', not {spec!r}")
    return kind, ref


def post(
    conn: sqlite3.Connection, agent: Agent, kind: str, ref: str, body: str, *, to: str | None = None,
    device: str | None = None, meta: dict[str, Any] | None = None, max_chars: int = MAX_BODY_CHARS,
) -> dict[str, Any]:
    """``agent`` writes in a chat it is in; ``to`` (an agent in the chat) addresses one agent. ``meta`` and
    ``max_chars`` are for the hub's own callers (asks)."""
    _check_body(body, max_chars)
    chat_row = _member_chat(conn, agent, kind, ref)
    meta = dict(meta or {})
    if to is not None:
        target = find_agent(conn, to)
        if target.id not in members(conn, chat_row["kind"], chat_row["ref"]):
            raise Invalid(f"agent {to!r} is not in this chat")
        meta["to"] = target.id
    _close_stretches(conn, chat_row, author=agent.id)
    return get_message(conn, _insert(conn, chat_row, author=agent.id, kind="text", body=body, meta=meta,
                                     device=device))


def post_as_hub(conn: sqlite3.Connection, kind: str, ref: str, body: str, *, to: str | None = None,
                meta: dict[str, Any] | None = None) -> int:
    """The hub's own words in a chat (an ask's or an intent overlap's kickoff), addressed to ``to`` if given."""
    chat_row = chat(conn, kind, ref)
    meta = dict(meta or {})
    if to is not None:
        meta["to"] = to
    return _insert(conn, chat_row, author=SYSTEM, kind="text", body=body, meta=meta)


def history(conn: sqlite3.Connection, agent: Agent, kind: str, ref: str, *, limit: int = 30,
            after_id: int = 0) -> dict[str, Any]:
    """A chat ``agent`` is in, with its latest ``limit`` messages (or those after ``after_id``)."""
    chat_row = _member_chat(conn, agent, kind, ref)
    rows = messages(conn, chat_row["kind"], chat_row["ref"], after_id=after_id, limit=min(max(limit, 1), 200))
    for m in rows:
        m.pop("legacy", None)
        if "deliveries" in m:  # who else got the operator's message, but not their notes
            m["deliveries"] = [{"agent_id": d["agent_id"], "name": d["name"], "status": d["status"],
                                "decision": d["decision"], "task_id": d["task_id"]} for d in m["deliveries"]]
    return {"chat": chat_row, "messages": rows}


_OUTCOME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _iso_time(value: Any) -> str | None:
    """An ISO-8601 date and time (a naive one is taken as UTC) in the hub's fixed-width UTC format, else None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return now_iso(parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC))


# --- live output from sessions ----------------------------------------------------------------------------------


def _open_stretch(conn: sqlite3.Connection, chat_id: int, author: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT id, kind, meta FROM chat_messages WHERE chat_id = ? AND author = ? "
                       "ORDER BY id DESC LIMIT 1", (chat_id, author)).fetchone()
    if row is None or row["kind"] != "tools" or not json.loads(row["meta"]).get("open"):
        return None
    return row


def stream(
    conn: sqlite3.Connection, agent: Agent, kind: str, ref: str, items: list[dict[str, Any]], *,
    task_id: int | None = None, device: str | None = None,
) -> dict[str, Any]:
    """What ``agent``'s session did, as the runner saw it: ``text`` items become bubbles, consecutive ``tool``
    items one ``tools`` row (appended in place), ``status`` items status lines. Items of an unknown type or an
    unknown status are skipped and counted in ``ignored``. ``task_id`` (the session's task, if ``agent`` is on it)
    tags the rows."""
    chat_row = _member_chat(conn, agent, kind, ref)
    if len(items) > MAX_STREAM_ITEMS:
        raise Invalid(f"at most {MAX_STREAM_ITEMS} items per call")
    if task_id is not None and not conn.execute("SELECT 1 FROM task_agents WHERE task_id = ? AND agent_id = ?",
                                                (task_id, agent.id)).fetchone():
        task_id = None
    tag = {"task_id": task_id} if task_id is not None else {}
    ids: list[int] = []
    ignored = 0
    stretch = _open_stretch(conn, chat_row["id"], agent.id)
    stretch_meta = json.loads(stretch["meta"]) if stretch else None
    grown = False

    def close() -> None:  # one update writes the stretch's new lines and closes it
        nonlocal stretch, stretch_meta, grown
        if stretch is not None:
            _update_meta(conn, chat_row, stretch["id"], stretch_meta | {"open": False}, actor=agent.id,
                         kind="tools")
        stretch, stretch_meta, grown = None, None, False

    for item in items:
        itype = item.get("type") if isinstance(item, dict) else None
        if itype == "tool" and isinstance(item.get("line"), str) and item["line"].strip():
            line = item["line"][:MAX_LINE_CHARS]
            if stretch is None:
                rid = _insert(conn, chat_row, author=agent.id, kind="tools", device=device,
                              meta={"lines": [line], "count": 1, "open": True, **tag})
                stretch = conn.execute("SELECT id, kind, meta FROM chat_messages WHERE id = ?", (rid,)).fetchone()
                stretch_meta = json.loads(stretch["meta"])
                ids.append(rid)
            else:
                if len(stretch_meta["lines"]) < MAX_TOOL_LINES:
                    stretch_meta["lines"].append(line)
                stretch_meta["count"] = stretch_meta.get("count", len(stretch_meta["lines"]) - 1) + 1
                grown = True
                if stretch["id"] not in ids:
                    ids.append(stretch["id"])
        elif itype == "text" and isinstance(item.get("text"), str) and item["text"].strip():
            close()
            ids.append(_insert(conn, chat_row, author=agent.id, kind="text", body=item["text"][:MAX_BODY_CHARS],
                               meta={"source": "session", **tag}, device=device))
        elif itype == "status" and item.get("state") in STATES:
            close()
            meta: dict[str, Any] = {"state": item["state"], **tag}
            until = _iso_time(item.get("until"))
            if until is not None:  # only a real time, stored in the hub's own format: never free agent text
                meta["until"] = until
            approval_id = item.get("approval_id")
            if isinstance(approval_id, int) and not isinstance(approval_id, bool) and conn.execute(
                    "SELECT 1 FROM approvals WHERE id = ? AND requester = ?", (approval_id, agent.id)).fetchone():
                meta["approval_id"] = approval_id  # only an approval this agent asked for
            message_id = item.get("message_id")
            if isinstance(message_id, int) and not isinstance(message_id, bool) and conn.execute(
                    "SELECT 1 FROM chat_deliveries WHERE message_id = ? AND agent_id = ?",
                    (message_id, agent.id)).fetchone():
                meta["message_id"] = message_id  # e.g. queued: which of the operator's messages waits
            if isinstance(item.get("detail"), str) and item["detail"].strip():
                meta["detail"] = item["detail"][:MAX_DETAIL_CHARS]
            ids.append(_insert(conn, chat_row, author=agent.id, kind="status", meta=meta, device=device))
        else:
            ignored += 1
    if grown and stretch is not None:
        _update_meta(conn, chat_row, stretch["id"], stretch_meta, actor=agent.id, kind="tools")
    return {"ids": ids, "ignored": ignored}


MAX_RUN_SECONDS = 7 * 24 * 3600


def record_run(
    conn: sqlite3.Connection, agent: Agent, kind: str, ref: str, *, outcome: str, seconds: float,
    session_id: str | None = None, num_turns: int | None = None, cost_usd: float | None = None,
    project: str | None = None, task_id: int | None = None,
) -> dict[str, Any]:
    """``agent``'s runner ran one session for a chat it is in; it ended now, after ``seconds`` (the hub's clock places
    it). Shown on the Usage page. It counts in the chat's project; an agent chat's session counts in ``project`` if
    ``agent`` may see that project, and on ``task_id`` if ``agent`` is on that task (a task chat's on its task)."""
    if not isinstance(outcome, str) or not _OUTCOME_RE.fullmatch(outcome):
        raise Invalid("outcome must be a word like success, error or usage_limit (a-z, 0-9, _)")
    chat_row = _member_chat(conn, agent, kind, ref)
    if chat_row["kind"] == "task":
        task_id = int(chat_row["ref"])
    elif task_id is not None and not conn.execute("SELECT 1 FROM task_agents WHERE task_id = ? AND agent_id = ?",
                                                  (task_id, agent.id)).fetchone():
        task_id = None
    if chat_row["project"] is not None:
        project = chat_row["project"]
    elif project is not None:
        known = conn.execute("SELECT 1 FROM projects WHERE name = ?", (project,)).fetchone()
        project = project if known and project in agent.allowed_projects else None
    now = datetime.now(UTC)
    started = now - timedelta(seconds=min(max(seconds, 0.0), MAX_RUN_SECONDS))
    cur = conn.execute(
        "INSERT INTO chat_runs (chat_id, agent_id, project, task_id, session_id, started_at, ended_at, outcome, "
        "num_turns, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_row["id"], agent.id, project, task_id, session_id, now_iso(started), now_iso(now), outcome, num_turns,
         cost_usd))
    return {"id": cur.lastrowid}


# --- deliveries --------------------------------------------------------------------------------------------------


def _delivery_rows(conn: sqlite3.Connection, where: str, params: list[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.id, d.message_id, d.status, d.mode, d.decision, d.task_id AS decision_task_id, d.created_at, "
        "m.author, m.body, m.created_at AS ts, m.meta, c.id AS chat_id, c.kind AS chat_kind, c.ref AS chat_ref, "
        "c.project FROM chat_deliveries d JOIN chat_messages m ON m.id = d.message_id "
        "JOIN chats c ON c.id = m.chat_id " + where, params).fetchall()
    out = []
    for r in rows:
        title = (f"task #{r['chat_ref']}" if r["chat_kind"] == "task"
                 else f"project {r['chat_ref']}" if r["chat_kind"] == "project" else "your chat with the operator")
        out.append({"id": r["id"], "message_id": r["message_id"], "status": r["status"], "mode": r["mode"],
                    "decision": r["decision"], "task_id": r["decision_task_id"],
                    "chat": {"id": r["chat_id"], "kind": r["chat_kind"], "ref": r["chat_ref"],
                             "project": r["project"],
                             "task_id": int(r["chat_ref"]) if r["chat_kind"] == "task" else None, "title": title},
                    "author": r["author"], "author_name": None, "body": r["body"],
                    "reply_to": json.loads(r["meta"]).get("reply_to"),
                    # The hub sets the author of every delivered message from the operator's identity.
                    "verified": _is_operator(r["author"]), "ts": r["ts"]})
    return out


def pending_deliveries(conn: sqlite3.Connection, agent: Agent, limit: int | None = None) -> list[dict[str, Any]]:
    """The operator's messages ``agent`` hasn't been given yet, oldest first."""
    sql, params = "WHERE d.agent_id = ? AND d.status = 'pending' ORDER BY d.id", [agent.id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return _delivery_rows(conn, sql, params)


def get_delivery(conn: sqlite3.Connection, delivery_id: int, agent: Agent) -> dict[str, Any]:
    rows = _delivery_rows(conn, "WHERE d.id = ? AND d.agent_id = ?", [delivery_id, agent.id])
    if not rows:
        raise NotFound(f"no delivery {delivery_id}")
    return rows[0]


def claim_delivery(
    conn: sqlite3.Connection, agent: Agent, delivery_id: int, *, mode: str, note: str = "",
) -> dict[str, Any]:
    """``agent``'s runner takes the operator's message to give it to a session now (``live``: into the running
    session; ``woke``: it starts or resumes its chat session; ``queued``: for the running session's next turn, said
    in the chat) or couldn't (``failed``, ``note`` says why, shown as agent text). Compare-and-set: Conflict when it
    was delivered already (e.g. a session read it through read_inbox), so the runner skips it."""
    if mode not in CLAIM_MODES:
        raise Invalid(f"mode must be one of {', '.join(CLAIM_MODES)}")
    found = get_delivery(conn, delivery_id, agent)
    cur = conn.execute("UPDATE chat_deliveries SET status = 'delivered', mode = ?, note = ?, delivered_at = ? "
                       "WHERE id = ? AND agent_id = ? AND status = 'pending'",
                       (mode, note[:MAX_DETAIL_CHARS], now_iso(), delivery_id, agent.id))
    if cur.rowcount == 0:
        raise Conflict(f"delivery {delivery_id} was already delivered ({found['mode']})", mode=found["mode"])
    chat_row = _chat_by_id(conn, found["chat"]["id"])
    if mode in ("queued", "failed"):
        meta: dict[str, Any] = {"state": mode, "agent_id": agent.id, "message_id": found["message_id"]}
        if mode == "failed" and note.strip():
            meta["detail"] = note[:MAX_DETAIL_CHARS]
        _insert(conn, chat_row, author=agent.id if mode == "failed" else SYSTEM, kind="status", meta=meta)
    _touch(conn, found["message_id"], actor=agent.id)
    return get_delivery(conn, delivery_id, agent)


def _deliver_by_inbox(conn: sqlite3.Connection, agent: Agent, deliveries: list[dict[str, Any]]) -> None:
    """A session read these through read_inbox: they are delivered (the runner's claim then gets Conflict)."""
    for d in deliveries:
        cur = conn.execute("UPDATE chat_deliveries SET status = 'delivered', mode = 'inbox', delivered_at = ? "
                           "WHERE id = ? AND status = 'pending'", (now_iso(), d["id"]))
        if cur.rowcount:
            _touch(conn, d["message_id"], actor=agent.id)


# --- the agents' inbox -------------------------------------------------------------------------------------------


def _agent_chat_ids(conn: sqlite3.Connection, agent: Agent, projects: list[str] | None = None) -> list[int]:
    """The group chats ``agent`` reads mail from: its tasks' chats and its projects' chats (``projects`` narrows)."""
    projects = list(agent.allowed_projects) if projects is None else projects
    if not projects:
        return []
    marks = ",".join("?" * len(projects))
    rows = conn.execute(
        f"SELECT id FROM chats WHERE (kind = 'project' AND ref IN ({marks})) OR (kind = 'task' AND project IN "
        f"({marks}) AND ref IN (SELECT CAST(task_id AS TEXT) FROM task_agents WHERE agent_id = ?))",
        [*projects, *projects, agent.id])
    return [r["id"] for r in rows]


def operator_mail(conn: sqlite3.Connection, agent: Agent, *, limit: int, mark_read: bool) -> list[dict[str, Any]]:
    """The operator's messages not yet delivered to ``agent`` as inbox entries, oldest first: ``kind`` ``private``
    from its own chat, ``operator`` from a task or project chat. Reading them (``mark_read``) delivers them (mode
    ``inbox``), so the runner won't give them to a session again."""
    if limit <= 0:
        return []
    found = pending_deliveries(conn, agent, limit=limit)
    if mark_read:
        _deliver_by_inbox(conn, agent, found)
    return [{"id": d["message_id"], "kind": "private" if d["chat"]["kind"] == "agent" else "operator",
             "delivery_id": d["id"], "chat": _spec(d["chat"]["kind"], d["chat"]["ref"]),
             "project": d["chat"]["project"], "channel": None, "author": d["author"], "author_name": None,
             "to_agent": agent.id, "body": d["body"], "ts": d["ts"], "addressed_to_me": True, "msg_kind": "text"}
            for d in found]


def chat_mail(
    conn: sqlite3.Connection, agent: Agent, *, projects: list[str] | None = None, limit: int,
    mark_read: bool, addressed_only: bool = False,
) -> list[dict[str, Any]]:
    """What other agents and the hub wrote in ``agent``'s task and project chats (in ``projects``) since it last
    read them, as inbox entries, oldest first (only rows addressed to it, with ``addressed_only``, which never marks
    read: moving the cursor would skip the rest). Tool lines, statuses, cards, a session's live text, the operator's
    text (it comes as deliveries), text addressed to another agent and backfilled history are not mail."""
    chat_ids = _agent_chat_ids(conn, agent, projects)
    if limit <= 0 or not chat_ids:
        return []
    rows = _chat_mail(conn, agent, chat_ids, limit, addressed_only)
    entries = []
    for r in rows:
        meta = json.loads(r["meta"])
        entries.append({"id": r["id"], "kind": "chat", "chat": _spec(r["chat_kind"], r["chat_ref"]),
                        "project": r["project"], "channel": None, "author": r["author"],
                        "author_name": r["author_name"], "to_agent": meta.get("to"), "body": _mail_body(r, meta),
                        "ts": r["ts"], "addressed_to_me": meta.get("to") == agent.id, "msg_kind": r["kind"]})
    if mark_read and rows and not addressed_only:
        latest: dict[int, int] = {}
        for r in rows:
            latest[r["chat_id"]] = max(latest.get(r["chat_id"], 0), r["id"])
        for chat_id, last_id in latest.items():
            conn.execute("INSERT INTO chat_cursors (agent_id, chat_id, last_read_id) VALUES (?, ?, ?) "
                         "ON CONFLICT (agent_id, chat_id) DO UPDATE SET last_read_id = "
                         "MAX(last_read_id, excluded.last_read_id)", (agent.id, chat_id, last_id))
    return entries


def _chat_mail(conn: sqlite3.Connection, agent: Agent, chat_ids: list[int], limit: int,
               addressed_only: bool) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(chat_ids))
    kinds = ",".join("?" * len(MAIL_KINDS))
    # Text addressed to another agent stays visible in the chat but isn't this agent's mail (as with 0.3.7's `to`).
    # addressed_only binds one agent id (`to` must be this agent); otherwise two: a row without `to` counts as
    # addressed to this agent (COALESCE(to, me) = me), and a row with `to` only when it is this agent.
    addressed = ("AND json_extract(m.meta, '$.to') = ? " if addressed_only
                 else "AND COALESCE(json_extract(m.meta, '$.to'), ?) = ? ")
    # A session's live narration (meta.source 'session') is for the operator to watch, not mail: only what an agent
    # posts on purpose (chat_post, took a task, sat out) and the hub's addressed notes reach other agents.
    return conn.execute(
        "SELECT m.id, m.chat_id, c.kind AS chat_kind, c.ref AS chat_ref, c.project, m.author, a.name AS author_name, "
        "m.kind, m.body, m.meta, m.created_at AS ts FROM chat_messages m JOIN chats c ON c.id = m.chat_id "
        "LEFT JOIN agents a ON a.id = m.author LEFT JOIN chat_cursors k ON k.chat_id = m.chat_id AND k.agent_id = ? "
        f"WHERE m.chat_id IN ({marks}) AND m.id > COALESCE(k.last_read_id, 0) AND m.author != ? "
        f"AND m.author NOT LIKE 'human:%' AND m.legacy IS NULL AND m.kind IN ({kinds}) "
        f"AND json_extract(m.meta, '$.source') IS NULL {addressed}"
        "ORDER BY m.id LIMIT ?",
        [agent.id, *chat_ids, agent.id, *MAIL_KINDS, *([agent.id] if addressed_only else [agent.id, agent.id]),
         limit]).fetchall()


def _mail_body(r: sqlite3.Row, meta: dict[str, Any]) -> str:
    """What an inbox entry says: the text, or (written by the hub) a took-task line, or the sit-out reason."""
    if r["kind"] == "took_task":
        return f"(took task #{meta.get('task_id')} for the operator's request #{meta.get('request_id')})"
    if r["kind"] == "sit_out":
        return f"(sits out the operator's request #{meta.get('request_id')}) {r['body']}"
    return r["body"]


def _spec(kind: str, ref: str) -> str:
    return "me" if kind == "agent" else f"{kind}:{ref}"


# --- the operator's requests in project chats: take a task or sit it out ----------------------------------------


def _request(conn: sqlite3.Connection, agent: Agent, request_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """The operator's project-chat message ``request_id`` and ``agent``'s delivery of it; NotFound if it wasn't
    sent to ``agent``, Conflict once ``agent`` has taken a task for it or sat it out."""
    row = conn.execute(
        "SELECT d.id, d.status, d.decision, d.task_id, m.author, m.kind, c.kind AS chat_kind, c.ref AS chat_ref "
        "FROM chat_deliveries d JOIN chat_messages m ON m.id = d.message_id JOIN chats c ON c.id = m.chat_id "
        "WHERE d.message_id = ? AND d.agent_id = ?", (request_id, agent.id)).fetchone()
    if row is None:
        raise NotFound(f"the operator sent you no request #{request_id}")
    if row["chat_kind"] != "project" or row["kind"] != "text" or not _is_operator(row["author"]):
        raise Invalid(f"message #{request_id} is not the operator's request in a project chat")
    if row["chat_ref"] not in agent.allowed_projects:
        raise NotFound(f"the operator sent you no request #{request_id}")
    if row["decision"] is not None:
        what = f"took task #{row['task_id']}" if row["decision"] == "took" else "sat it out"
        raise Conflict(f"you already {what} for request #{request_id}")
    return dict(row), chat(conn, "project", row["chat_ref"])


def _decide(conn: sqlite3.Connection, delivery: dict[str, Any], decision: str, task_id: int | None) -> None:
    conn.execute("UPDATE chat_deliveries SET decision = ?, task_id = ?, status = 'delivered', "
                 "mode = COALESCE(mode, 'inbox'), delivered_at = COALESCE(delivered_at, ?) WHERE id = ?",
                 (decision, task_id, now_iso(), delivery["id"]))


def take_task(
    conn: sqlite3.Connection, agent: Agent, request_id: int, *, title: str, body: str = "",
    device: str | None = None,
) -> dict[str, Any]:
    """``agent`` takes a share of the operator's request in a project chat: a new task in that project, already
    approved (``open``: the request came from the operator), written by ``agent`` (its title and body are agent
    text), with ``agent`` working on it; the chat shows a ``took_task`` row. Once per agent and request."""
    from agenthub.hub import task_agents, tasks  # tasks imports nothing of chats, but keep the hub's import order

    title = title.strip() if isinstance(title, str) else ""
    if not title or len(title) > MAX_TITLE_CHARS:
        raise Invalid(f"title must be 1 to {MAX_TITLE_CHARS} characters")
    if len(body) > MAX_TASK_BODY_CHARS:
        raise Invalid(f"description must be at most {MAX_TASK_BODY_CHARS} characters")
    delivery, chat_row = _request(conn, agent, request_id)
    task = tasks.create_task(conn, actor=agent.id, project=chat_row["ref"], title=title, body=body, state="open",
                             device=device)
    task = task_agents.add_agent(conn, task["id"], agent.id, role=f"Took from the operator's request #{request_id}",
                                 stage="works", actor=agent.id)
    _decide(conn, delivery, "took", task["id"])
    _close_stretches(conn, chat_row, author=agent.id)
    _insert(conn, chat_row, author=agent.id, kind="took_task", meta={"task_id": task["id"], "request_id": request_id},
            device=device)
    _touch(conn, request_id, actor=agent.id)
    return task


def sit_out(conn: sqlite3.Connection, agent: Agent, request_id: int, *, reason: str,
            device: str | None = None) -> dict[str, Any]:
    """``agent`` takes no share of the operator's request in a project chat; ``reason`` shows in the chat."""
    _check_body(reason, MAX_DETAIL_CHARS * 4)
    delivery, chat_row = _request(conn, agent, request_id)
    _decide(conn, delivery, "sat_out", None)
    _close_stretches(conn, chat_row, author=agent.id)
    message_id = _insert(conn, chat_row, author=agent.id, kind="sit_out", body=reason,
                         meta={"request_id": request_id}, device=device)
    _touch(conn, request_id, actor=agent.id)
    return get_message(conn, message_id)


# --- cards -------------------------------------------------------------------------------------------------------


def _card_chat(conn: sqlite3.Connection, task_id: Any, agent_id: str) -> dict[str, Any] | None:
    """Where a card goes: its task's chat, else the agent's own chat (None if neither exists)."""
    if isinstance(task_id, int) and not isinstance(task_id, bool):
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            return chat(conn, "task", str(task_id))
    if get_agent(conn, agent_id) is not None:
        return chat(conn, "agent", agent_id)
    return None


def approval_card(conn: sqlite3.Connection, approval: dict[str, Any]) -> int | None:
    """A new approval's card, in its task's chat or its requester's chat."""
    chat_row = _card_chat(conn, approval["details"].get("task_id"), approval["requester"])
    if chat_row is None:
        return None
    return _insert(conn, chat_row, author=approval["requester"], kind="approval",
                   meta={"approval_id": approval["id"]}, legacy=f"ap:{approval['id']}")


def question_card(conn: sqlite3.Connection, question: dict[str, Any]) -> int | None:
    """A new question's card, in its task's chat or the asking agent's chat."""
    chat_row = _card_chat(conn, question["task_id"], question["agent_id"])
    if chat_row is None:
        return None
    return _insert(conn, chat_row, author=question["agent_id"], kind="question",
                   meta={"question_id": question["id"]}, legacy=f"q:{question['id']}")


def card(conn: sqlite3.Connection, kind: str, item_id: int) -> dict[str, Any] | None:
    """The chat message that shows approval or question ``item_id`` (``kind`` approval|question), if any."""
    if kind not in ("approval", "question"):
        raise Invalid(f"no card kind {kind!r}")
    row = conn.execute(f"SELECT id FROM chat_messages WHERE kind = ? AND json_extract(meta, '$.{kind}_id') = ? "
                       "ORDER BY id LIMIT 1", (kind, item_id)).fetchone()
    return get_message(conn, row["id"]) if row else None


def with_bodies(conn: sqlite3.Connection, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Search hits with the text of the task- and project-chat messages they point at (their events carry ids;
    the caller already limited the hits to its projects, and agent chats have no project)."""
    ids = [e["payload"].get("message_id") for e in found
           if e["type"] == "chat.message" and e["payload"].get("chat_kind") in ("task", "project")]
    if not ids:
        return found
    rows = conn.execute(f"SELECT id, body FROM chat_messages WHERE id IN ({','.join('?' * len(ids))})", ids)
    bodies = {r["id"]: r["body"] for r in rows}
    for e in found:
        if e["type"] == "chat.message" and e["payload"].get("message_id") in bodies:
            e["payload"] = e["payload"] | {"body": bodies[e["payload"]["message_id"]]}
    return found


# --- backfill from 0.3.7 -----------------------------------------------------------------------------------------


def _prompt_chat(target: str, project: str | None, agent_id: str) -> tuple[str, str]:
    """Where a prompt's group outcome (a share taken, a share declined) goes: its task's or project's chat, else the
    recipient's own."""
    kind, _, key = target.partition(":")
    if kind == "task" and key.isdigit():
        return "task", key
    if kind == "project" and project:
        return "project", project
    return "agent", agent_id


Legacy = tuple[str, str, str, str, str, str, dict[str, Any], str, tuple[str, ...]]
OPEN_RECIPIENT = ("pending", "running")


def _legacy_rows(conn: sqlite3.Connection) -> list[Legacy]:
    """``(ts, legacy, chat kind, chat ref, author, kind, meta, body, deliver_to)`` for every 0.3.7 row a chat should
    show. ``deliver_to``: the agents a prompt was still pending or running for when the hub was upgraded (they get
    it as the operator's chat message, since no runner runs prompts any more)."""
    out: list[Legacy] = []
    tasks_ = {r["id"]: r["project"] for r in conn.execute("SELECT id, project FROM tasks")}
    for r in conn.execute("SELECT id, agent_id, author, body, ts FROM private_messages WHERE prompt_id IS NULL"):
        out.append((r["ts"], f"pm:{r['id']}", "agent", r["agent_id"], r["author"], "text",
                    {"legacy": {"from": "private", "id": r["id"]}}, r["body"], ()))
    prompts_ = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM prompts")}
    still_open: dict[int, list[str]] = {}
    for r in conn.execute("SELECT * FROM prompt_recipients ORDER BY prompt_id, agent_id"):
        p = prompts_[r["prompt_id"]]
        pid, agent_id = p["id"], r["agent_id"]
        legacy_meta = {"from": "prompt", "prompt_id": pid, "target": p["target"], "mode": p["mode"]}
        kind, ref = _prompt_chat(p["target"], p["project"], agent_id)
        if kind == "task" and int(ref) not in tasks_:
            kind, ref = "agent", agent_id
        is_open = r["status"] in OPEN_RECIPIENT
        if is_open:
            still_open.setdefault(pid, []).append(agent_id)
        # A group prompt with a task or project chat is delivered there (so its recipients can take_task); any
        # other open prompt in the recipient's own chat.
        deliver = (agent_id,) if is_open and not (p["mode"] == "group" and kind != "agent") else ()
        out.append((p["created_at"], f"prompt:{pid}:{agent_id}", "agent", agent_id, p["created_by"], "text",
                    {"legacy": legacy_meta}, p["body"], deliver))
        key, status = f"pr:{pid}:{agent_id}:{r['status']}", r["status"]
        meta: dict[str, Any] = {"legacy": legacy_meta}
        if status == "claimed" and r["task_id"] is not None:
            out.append((r["updated_at"], key, kind, ref, agent_id, "took_task", meta | {"task_id": r["task_id"]}, "",
                        ()))
        elif status == "declined":
            out.append((r["updated_at"], key, kind, ref, agent_id, "sit_out", meta, r["note"] or "(no reason)", ()))
        elif status in ("done", "failed", "timed_out", "skipped"):
            meta |= {"state": "finished" if status == "done" else "failed"}
            if r["note"]:
                meta |= {"detail": r["note"][:MAX_DETAIL_CHARS], "detail_by_hub": bool(r["note_by_hub"])}
            out.append((r["updated_at"], key, "agent", agent_id, SYSTEM if r["note_by_hub"] else agent_id,
                        "status", meta, "", ()))
    for p in prompts_.values():  # a group prompt's text also opens its task's or project's chat
        kind, ref = _prompt_chat(p["target"], p["project"], "")
        if p["mode"] == "group" and kind != "agent" and (kind != "task" or int(ref) in tasks_):
            out.append((p["created_at"], f"prompt:{p['id']}:group", kind, ref, p["created_by"], "text",
                        {"legacy": {"from": "prompt", "prompt_id": p["id"], "target": p["target"],
                                    "mode": p["mode"]}}, p["body"], tuple(still_open.get(p["id"], ()))))
    for r in conn.execute("SELECT m.id, m.project, c.name AS channel, m.author, m.to_agent, m.body, m.ts "
                          "FROM messages m JOIN channels c ON c.id = m.channel_id"):
        kind, ref = "project", r["project"]
        name = r["channel"]
        if name.startswith("task-") and name[5:].isdigit() and tasks_.get(int(name[5:])) == r["project"]:
            kind, ref = "task", name[5:]
        elif name.startswith("prompt-") and name[7:].isdigit() and int(name[7:]) in prompts_:
            p = prompts_[int(name[7:])]
            pk, pref = _prompt_chat(p["target"], p["project"], "")
            if pk == "task" and tasks_.get(int(pref)) == r["project"]:
                kind, ref = "task", pref
        meta = {"legacy": {"from": "channel", "channel": name, "id": r["id"]}}
        if r["to_agent"]:
            meta["to"] = r["to_agent"]
        out.append((r["ts"], f"msg:{r['id']}", kind, ref, r["author"], "text", meta, r["body"], ()))
    for r in conn.execute("SELECT id, requester, details, created_at FROM approvals"):
        task_id = json.loads(r["details"]).get("task_id")
        if isinstance(task_id, int) and task_id in tasks_:
            kind, ref = "task", str(task_id)
        else:
            kind, ref = "agent", r["requester"]
        out.append((r["created_at"], f"ap:{r['id']}", kind, ref, r["requester"], "approval",
                    {"approval_id": r["id"]}, "", ()))
    for r in conn.execute("SELECT id, agent_id, task_id, created_at FROM questions"):
        kind, ref = ("task", str(r["task_id"])) if r["task_id"] in tasks_ else ("agent", r["agent_id"])
        out.append((r["created_at"], f"q:{r['id']}", kind, ref, r["agent_id"], "question",
                    {"question_id": r["id"]}, "", ()))
    return out


def backfill(conn: sqlite3.Connection) -> int:
    """Copy what 0.3.7 kept elsewhere into chats (call it inside a transaction); returns how many rows it added.

    Private messages (a prompt's copy is left for its prompt), each prompt recipient's prompt text and outcome, a
    group prompt's text in its task's or project's chat, channel posts (``task-<id>`` to the task's chat, a prompt's
    thread to its task's chat, the rest to the project's chat, ``to`` kept), and a card for every approval and
    question. Keyed on ``legacy``, so running it again (every start, or after a rollback to 0.3.7 and back) adds only
    what is new; rows are added oldest first. A prompt still pending or running for an active agent becomes a
    pending delivery of its copy, made once with the row, so the work the operator asked for before the upgrade still
    reaches the agent. No events: the bus starts after this, and an agent's runner asks for its deliveries when it
    connects."""
    agents = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM agents")}
    projects = {r["name"] for r in conn.execute("SELECT name FROM projects")}
    tasks_ = {str(r["id"]): r["project"] for r in conn.execute("SELECT id, project FROM tasks")}
    done = {r["legacy"] for r in conn.execute("SELECT legacy FROM chat_messages WHERE legacy IS NOT NULL")}
    rows = sorted((r for r in _legacy_rows(conn) if r[1] not in done), key=lambda r: (r[0], r[1]))
    chat_ids: dict[tuple[str, str], int] = {}
    added = 0
    now = now_iso()
    for ts, legacy, kind, ref, author, mkind, meta, body, deliver_to in rows:
        if (kind == "agent" and ref not in agents) or (kind == "project" and ref not in projects) or \
                (kind == "task" and ref not in tasks_):
            continue
        if (kind, ref) not in chat_ids:
            project = None if kind == "agent" else tasks_[ref] if kind == "task" else ref
            chat_ids[(kind, ref)] = _ensure(conn, kind, ref, project)
        cur = conn.execute(
            "INSERT INTO chat_messages (chat_id, author, kind, body, meta, legacy, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (chat_ids[(kind, ref)], author, mkind, body, json.dumps(meta, separators=(",", ":")), legacy, ts, ts))
        added += cur.rowcount
        if cur.rowcount and _is_operator(author):
            for agent_id in deliver_to:
                if agents.get(agent_id) == "active":
                    conn.execute("INSERT INTO chat_deliveries (message_id, agent_id, status, created_at) "
                                 "VALUES (?, ?, 'pending', ?) ON CONFLICT DO NOTHING", (cur.lastrowid, agent_id, now))
    return added
