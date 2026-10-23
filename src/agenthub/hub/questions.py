"""Questions an agent asks the operator (the ``ask_operator`` tool).

A pending question waits in the approval queue and shows as a card in a chat (its task's, else the asking agent's
own; :func:`chats.question_card`). The operator's answer (on the card or the Approvals page, one endpoint) goes
back to the asking agent as the operator's message in the agent's own chat (``Answer to your question #N: …``), so
its runner delivers it like any other: into the running session, or waking the agent's chat session. Unlike chat
text, questions are not secret, but ``question.*`` events carry ids only (the page reads the table).

Before 0.3.8 an answer went as a prompt or a private message; :func:`answer_delivery` still finds those.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from agenthub.hub import chats, events, tasks
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound, get_agent

MAX_CHARS = 4000
MAX_PENDING_PER_AGENT = 5

_SELECT = "SELECT q.*, a.name AS agent_name, a.status AS agent_status FROM questions q LEFT JOIN agents a ON a.id = q.agent_id "


def _payload(q: dict[str, Any]) -> dict[str, Any]:
    return {"question_id": q["id"], "agent_id": q["agent_id"], "project": q["project"], "task_id": q["task_id"]}


def get(conn: sqlite3.Connection, qid: int) -> dict[str, Any]:
    row = conn.execute(_SELECT + "WHERE q.id = ?", (qid,)).fetchone()
    if row is None:
        raise NotFound(f"no question {qid}")
    return dict(row)


def get_own(conn: sqlite3.Connection, qid: int, agent: Agent) -> dict[str, Any]:
    """The question, or NotFound when another agent asked it (its existence isn't revealed)."""
    q = get(conn, qid)
    if q["agent_id"] != agent.id:
        raise NotFound(f"no question {qid}")
    return q


def pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Unanswered questions, newest first."""
    return [dict(r) for r in conn.execute(_SELECT + "WHERE q.status = 'pending' ORDER BY q.id DESC")]


def recently_answered(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(_SELECT + "WHERE q.status = 'answered' ORDER BY q.answered_at DESC, q.id DESC LIMIT ?",
                        (limit,))
    return [dict(r) for r in rows]


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM questions WHERE status = 'pending'").fetchone()[0]


def ask(
    conn: sqlite3.Connection, agent: Agent, *, project: str, question: str, task_id: int | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    if project not in agent.allowed_projects:
        raise Forbidden(f"no access to project {project!r}")
    if not question.strip() or len(question) > MAX_CHARS:
        raise Invalid(f"a question must be 1 to {MAX_CHARS} characters")
    if task_id is not None and tasks.get_task(conn, task_id)["project"] != project:
        raise Invalid(f"task {task_id} is not in project {project!r}")
    open_ones = conn.execute("SELECT COUNT(*) FROM questions WHERE agent_id = ? AND status = 'pending'",
                             (agent.id,)).fetchone()[0]
    if open_ones >= MAX_PENDING_PER_AGENT:
        raise Conflict(f"you already have {MAX_PENDING_PER_AGENT} unanswered questions; wait or withdraw one")
    cur = conn.execute(
        "INSERT INTO questions (project, agent_id, task_id, question, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)", (project, agent.id, task_id, question, now_iso()))
    q = get(conn, cur.lastrowid)
    events.emit(conn, type="question.asked", actor=agent.id, project=project, device=device, payload=_payload(q))
    chats.question_card(conn, q)
    return q


def answer(conn: sqlite3.Connection, qid: int, *, answer: str, actor: str) -> dict[str, Any]:
    if not actor.startswith("human:"):
        raise Forbidden("only a human operator can answer questions")
    if not answer.strip() or len(answer) > MAX_CHARS:
        raise Invalid(f"an answer must be 1 to {MAX_CHARS} characters")
    q = get(conn, qid)
    cur = conn.execute("UPDATE questions SET status = 'answered', answer = ?, answered_by = ?, answered_at = ? "
                       "WHERE id = ? AND status = 'pending'", (answer, actor, now_iso(), qid))
    if cur.rowcount == 0:
        raise Conflict(f"question {qid} is already {q['status']}")
    events.emit(conn, type="question.answered", actor=actor, project=q["project"], payload=_payload(q))
    agent = get_agent(conn, q["agent_id"])
    if agent is None or agent.status == "revoked":  # a revoked agent's chat is read-only
        return get(conn, qid)
    # A chat message holds the whole answer (MAX_CHARS is well under chats.MAX_BODY_CHARS).
    chats.send(conn, "agent", agent.id, _answer_prefix(qid) + answer + "\n\n" + _about(q), actor,
               meta={"answer_to": qid})
    return get(conn, qid)


def _about(q: dict[str, Any]) -> str:
    """The hub's line after an answer (outside the operator's text): which question it answers, so the session
    that gets it (often a new one) can look the question up."""
    lookup = f"call get_question({q['id']}) for your question"
    return f"(about task #{q['task_id']} in {q['project']}; {lookup})" if q["task_id"] else f"({lookup})"


def _answer_prefix(qid: int) -> str:
    return f"Answer to your question #{qid}: "


def answer_prompt_id(conn: sqlite3.Connection, q: dict[str, Any]) -> int | None:
    """The prompt :func:`answer` made to carry ``q``'s answer: the first prompt to the asking agent, by the
    answering operator, at or after the answer, whose text starts with the answer's prefix. None if there is
    none (unanswered, or the answer went as a private message)."""
    if q["status"] != "answered" or not q["answered_at"]:
        return None
    prefix = _answer_prefix(q["id"])
    row = conn.execute(
        "SELECT id FROM prompts WHERE target = ? AND created_by = ? AND created_at >= ? AND substr(body, 1, ?) = ? "
        "ORDER BY id LIMIT 1", (f"agent:{q['agent_id']}", q["answered_by"], q["answered_at"], len(prefix), prefix),
    ).fetchone()
    return row["id"] if row else None


def answer_delivery(conn: sqlite3.Connection, q: dict[str, Any]) -> dict[str, Any]:
    """How ``q``'s answer reached its agent: ``{chat_message_id, prompt_id, private}``: the operator's message in the
    agent's chat that carries it (0.3.8), or before 0.3.8 the prompt :func:`answer` made (or None)
    and whether it sent the answer as a plain private message (the answer's prefix, from the answering operator,
    at or after the answer)."""
    private_sent = False
    if q["status"] == "answered" and q["answered_at"]:
        prefix = _answer_prefix(q["id"])
        private_sent = conn.execute(
            "SELECT 1 FROM private_messages WHERE agent_id = ? AND author = ? AND ts >= ? AND substr(body, 1, ?) = ?",
            (q["agent_id"], q["answered_by"], q["answered_at"], len(prefix), prefix)).fetchone() is not None
    row = conn.execute("SELECT id FROM chat_messages WHERE json_extract(meta, '$.answer_to') = ? ORDER BY id LIMIT 1",
                       (q["id"],)).fetchone()
    return {"chat_message_id": row["id"] if row else None, "prompt_id": answer_prompt_id(conn, q),
            "private": private_sent}


def withdraw(conn: sqlite3.Connection, qid: int, agent: Agent) -> dict[str, Any]:
    q = get_own(conn, qid, agent)
    cur = conn.execute("UPDATE questions SET status = 'withdrawn' WHERE id = ? AND status = 'pending'", (qid,))
    if cur.rowcount == 0:
        raise Conflict(f"question {qid} is already {q['status']}")
    return get(conn, qid)
