"""stdio MCP server exposing the hub to Claude Code.

Written against the official MCP Python SDK v2 (``mcp`` 2.3.0, PyPI upload
2026-10-02; migration guide https://py.sdk.modelcontextprotocol.io/v2/migration/):
``FastMCP`` is now ``mcp.server.mcpserver.MCPServer``, ``@server.tool()`` must
be called, ``server.run()`` defaults to stdio, and ``ToolError`` messages are
returned to the client while other exceptions are reported generically.

Everything written by another agent is wrapped by :mod:`agenthub.untrusted`.
The operator's private messages are not: the hub sets their author from the
operator's identity and only ever hands an agent the operator's half of its own
chat, so no agent can forge one.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agenthub import untrusted
from agenthub.client import HubClient, HubError

INSTRUCTIONS = f"""\
agenthub connects you to other Claude Code agents through a central hub.
Use `whoami` to see your projects, `read_inbox` to receive messages, `post`
to send them, and `board_get`/`board_put` for shared versioned documents.
To reply to the operator privately, use `message_operator`.
To ask the operator a question only they can answer, use `ask_operator`; the answer arrives in your inbox.
Agents on the same machine share a device board: write to it with `post_device`; its notes arrive in `read_inbox`.
When you get a prompt with several recipients, discuss it in its thread, then `claim_share` your part or `decline_share`; `prompt_status` shows who else is on it.
{untrusted.NOTICE}
{untrusted.HUMAN_ONLY}
"""


OPERATOR_HEADING = "From the operator (private, verified by the hub):"


def _is_operator_private(m: dict[str, Any]) -> bool:
    return m.get("kind") == "private" and str(m.get("author") or "").startswith("human:")


def _format_message(m: dict[str, Any]) -> str:
    author = untrusted.author_label(m.get("author"), m.get("author_name"))
    return untrusted.wrap(
        m["body"],
        author=author,
        kind="device_note" if m.get("kind") == "device" else "message",
        id=m["id"],
        project=m["project"],
        channel=m["channel"],
        ts=m["ts"],
        to="you" if m.get("addressed_to_me") else None,
    )


def _format_task(t: dict[str, Any], *, with_body: bool = False) -> str:
    who = untrusted.author_label(t.get("created_by"), t.get("created_by_name"))
    # Roles are written by the operator only, so they sit outside the untrusted block.
    members = ", ".join(f"{m['name']} ({m['role']}, {m['stage']})" for m in t.get("agents") or []) or "none"
    header = f"task #{t['id']} [{t['state']}] project={t['project']} agents (roles set by the operator): {members} budget={t['budget']}"
    text = t["title"] + ("\n\n" + t["body"] if with_body and t.get("body") else "")
    return header + "\n" + untrusted.wrap(text, author=who, kind="task", id=t["id"])


def _format_approval(a: dict[str, Any]) -> str:
    line = f"approval #{a['id']} kind={a['kind']} status={a['status']}"
    if a.get("decided_by"):
        line += f" decided_by={a['decided_by']} at {a['decided_at']}"
    if a.get("decision_note"):
        line += f" note={a['decision_note']!r}"
    parts = [line]
    thread = a.get("discussion") or []
    if thread:
        parts.append("Discussion with the operator:")
        for m in thread:
            who = "you" if not m["author"].startswith("human:") else f"operator {m['author']}"
            parts.append(f"  [{m['ts']}] {who}: {m['body']}")
    if a.get("awaiting_reply"):
        parts.append(
            "The operator is waiting for your reply. Answer with discuss_approval, then call "
            "wait_for_approval again."
        )
    if a["kind"] == "command" and a["status"] == "approved":
        parts.append("You may now retry the exact same command once, from the same directory.")
    return "\n".join(parts)


THREAD_TAIL = 20  # how many of a prompt thread's latest messages prompt_status shows


def _format_prompt(p: dict[str, Any], thread: list[dict[str, Any]]) -> str:
    """The operator's text (the hub sets it, so it sits outside any untrusted block), then the facts the hub
    records, then what the other agents wrote: task titles and thread messages, all in untrusted blocks."""
    parts = [f"From the operator (prompt #{p['id']}, verified by the hub):\n{p['body']}",
             f"mode: {p['mode']}, status: {p['status']}, your status: {p['my_status']}"]
    if p["recipients"]:
        parts.append("Recipients (names, statuses and devices are the hub's; task titles are written by agents):")
    for r in p["recipients"]:
        line = f"- {r['name']}: {r['status']}, device {r['device'] or 'unknown'}, {'online' if r['connected'] else 'offline'}"
        if r["tasks"]:
            line += "; current tasks:\n" + "\n".join(
                untrusted.wrap(t["title"], author=untrusted.author_label(r["agent_id"], r["name"]),
                               kind="task_title", id=t["id"], project=t["project"], state=t["state"])
                for t in r["tasks"])
        parts.append(line)
    th = p.get("thread")
    if th is None:
        parts.append("There is no shared thread for this prompt.")
    else:
        where = f"{th['project']}/{th['channel']}" if th["kind"] == "channel" else "your device board"
        parts.append(f"Thread ({where}), latest {THREAD_TAIL} messages:" if thread else f"Thread ({where}) is empty.")
        for m in thread:
            parts.append(_format_message(
                m | {"kind": "device", "project": None, "channel": "device"} if th["kind"] == "device" else m))
    return "\n\n".join(parts)


WAIT_POLL_SECONDS = 3.0
MAX_WAIT_SECONDS = 840  # stays under the 15-minute per-call MCP timeout set by `join`


def build_server(client_factory: Callable[[], HubClient], *, poll_seconds: float = WAIT_POLL_SECONDS) -> MCPServer:
    server = MCPServer("agenthub", instructions=INSTRUCTIONS)

    async def call(fn: Callable[[HubClient], Awaitable[Any]]) -> Any:
        try:
            return await fn(client_factory())
        except HubError as exc:
            raise ToolError(f"hub error {exc.status}: {exc.detail}") from exc
        except httpx.TransportError as exc:
            raise ToolError(f"cannot reach hub: {exc.__class__.__name__}") from exc

    @server.tool()
    async def whoami() -> str:
        """Show this agent's identity, approval status, and the projects it may use."""
        me = await call(lambda c: c.me())
        return json.dumps(me, indent=2)

    @server.tool()
    async def post(project: str, body: str, channel: str = "general", to: str | None = None) -> str:
        """Post a message to a project channel. Set `to` (agent name) to address one agent."""
        r = await call(lambda c: c.post(project, body, channel=channel, to=to))
        target = f" (to {to})" if to else ""
        return f"Posted message #{r['id']} to {project}/{channel}{target}."

    @server.tool()
    async def read_inbox(project: str | None = None, limit: int = 20, peek: bool = False) -> str:
        """Fetch unread messages (the operator's private ones first, then your device board's, then your
        projects') and mark them read (unless peek=true)."""
        r = await call(lambda c: c.inbox(project=project, limit=limit, peek=peek))
        if not r["messages"]:
            return "No unread messages."
        mine = [m for m in r["messages"] if _is_operator_private(m)]
        others = [m for m in r["messages"] if not _is_operator_private(m)]
        parts = [f"{OPERATOR_HEADING}\n{m['body']}" for m in mine]
        if others:
            parts += [untrusted.NOTICE, *(_format_message(m) for m in others)]
        if r["more"]:
            parts.append("More unread messages remain; call read_inbox again.")
        return "\n\n".join(parts)

    @server.tool()
    async def message_operator(body: str) -> str:
        """Send the operator a private message. Only the operator sees it, never other agents."""
        r = await call(lambda c: c.message_operator(body))
        return f"Sent private message #{r['id']} to the operator."

    @server.tool()
    async def post_device(body: str, to: str | None = None) -> str:
        """Post a note to your machine's device board, seen only by the other agents on this machine and
        the operator. Set `to` (agent name) to address one of them."""
        r = await call(lambda c: c.post_device(body, to=to))
        target = f" (to {to})" if to else ""
        return f"Posted note #{r['id']} to your device board{target}."

    @server.tool()
    async def ask_operator(project: str, question: str, task_id: int | None = None) -> str:
        """Ask the operator a question and carry on; use it when only the operator can decide (a
        choice, a credential, an unclear requirement). Returns at once: the answer arrives in your
        inbox as a private message from the operator. Pass task_id if it concerns a task."""
        r = await call(lambda c: c.ask_operator(project, question, task_id))
        return (f"Asked the operator (question #{r['id']}). "
                "The answer will arrive in your inbox as a private message.")

    @server.tool()
    async def get_question(question_id: int) -> str:
        """Look up one of your own questions to the operator: its status and, once answered, the
        operator's full answer."""
        q = await call(lambda c: c.get_question(question_id))
        out = f"Question #{q['id']} [{q['status']}]: {q['question']}"
        if q["status"] == "answered":  # the hub sets the author from the operator's identity
            out += f"\n\n{OPERATOR_HEADING}\n{q['answer']}"
        return out

    @server.tool()
    async def search_history(query: str, project: str | None = None, limit: int = 10) -> str:
        """Full-text search over the event history of your projects."""
        results = await call(lambda c: c.search(query, project=project, limit=limit))
        if not results:
            return "No matches."
        blocks = [
            untrusted.wrap(
                json.dumps(e["payload"], indent=1),
                author=untrusted.author_label(e["actor"], None),
                kind=e["type"], event_id=e["id"], project=e["project"], ts=e["ts"],
            )
            for e in results
        ]
        return "\n\n".join([untrusted.NOTICE, *blocks])

    @server.tool()
    async def board_get(project: str, key: str | None = None, prefix: str = "") -> str:
        """Read a shared board document, or list keys (optionally by prefix) when key is omitted.

        The returned version is what you pass as expected_version to board_put.
        """
        if key is None:
            keys = await call(lambda c: c.board_list(project, prefix))
            if not keys:
                return "Board is empty." if not prefix else f"No keys with prefix {prefix!r}."
            return "\n".join(f"{k['key']}  (version {k['version']}, updated {k['updated_at']})" for k in keys)
        doc = await call(lambda c: c.board_get(project, key))
        header = f"{project}:{doc['key']} version {doc['version']}"
        body = untrusted.wrap(
            doc["value"],
            author=untrusted.author_label(doc["updated_by"], doc.get("updated_by_name")),
            kind="board", key=doc["key"], version=doc["version"], updated_at=doc["updated_at"],
        )
        return f"{header}\n{body}"

    @server.tool()
    async def board_put(project: str, key: str, value: str, expected_version: int) -> str:
        """Write a board document. expected_version must equal the current version (0 to create).

        On a version conflict nothing is written: re-read with board_get, merge, and retry.
        """
        r = await call(lambda c: c.board_put(project, key, value, expected_version))
        return f"Wrote {project}:{r['key']} version {r['version']}."

    @server.tool()
    async def list_tasks(project: str, state: str | None = None, mine: bool = False) -> str:
        """List tasks in a project, optionally filtered by state or to tasks assigned to you."""
        rows = await call(lambda c: c.list_tasks(project, state=state, mine=mine))
        if not rows:
            return "No matching tasks."
        return "\n\n".join([untrusted.NOTICE, *(_format_task(t) for t in rows)])

    @server.tool()
    async def create_task(
        project: str, title: str, body: str = "", assignee: str | None = None, reason: str = "",
    ) -> str:
        """Propose a task. It waits in awaiting_approval until the operator approves it."""
        r = await call(lambda c: c.create_task(project, title, body, assignee, None, reason))
        return (f"Created task #{r['task']['id']} in state {r['task']['state']}. "
                f"Approval #{r['approval']['id']} is pending; use wait_for_approval to wait for the decision.")

    @server.tool()
    async def claim_task(task_id: int) -> str:
        """Claim an open task (it must be unassigned or assigned to you)."""
        t = await call(lambda c: c.claim_task(task_id))
        return f"Claimed.\n{_format_task(t, with_body=True)}"

    @server.tool()
    async def update_task(task_id: int, state: str | None = None, note: str = "") -> str:
        """Move your task (blocked / in_progress / open) and/or add a progress note.

        Use finish_task when your part is done. Only the operator can mark a task done.
        """
        t = await call(lambda c: c.update_task(task_id, state=state, note=note))
        return f"Task #{t['id']} is now {t['state']}."

    @server.tool()
    async def finish_task(task_id: int, note: str = "") -> str:
        """Mark your part of the task finished; the task moves to review when every implementer has finished."""
        t = await call(lambda c: c.finish_task(task_id, note))
        if t["state"] == "review":
            return f"Task #{t['id']} moves to review: every implementer has finished."
        return f"Marked your part of task #{t['id']} finished; it is still {t['state']}."

    @server.tool()
    async def review_task(task_id: int, verdict: str, notes: str = "") -> str:
        """As this task's reviewer, record 'approve' or 'changes' with notes. The operator decides what happens next."""
        t = await call(lambda c: c.review_task(task_id, verdict, notes))
        return f"Recorded verdict {verdict!r} on task #{t['id']}; the operator decides what happens next."

    @server.tool()
    async def request_approval(kind: str, reason: str, project: str | None = None,
                               details: dict[str, Any] | None = None) -> str:
        """Ask the operator to approve something. kind is one of: command (details: command, cwd),
        task_reassign (details: task_id, assignee), budget_exceeded (details: task_id)."""
        a = await call(lambda c: c.request_approval(kind, details or {}, project=project, reason=reason))
        return _format_approval(a) + "\nUse wait_for_approval to wait for the decision."

    @server.tool()
    async def wait_for_approval(approval_id: int, timeout_seconds: int = 300) -> str:
        """Wait (polling) until an approval you requested is decided, or the timeout passes.

        Returns early if the operator asks you something in the approval's
        discussion; reply with discuss_approval and wait again.
        Returns the status: pending (timed out or awaiting your reply), approved, or rejected.
        """
        deadline = time.monotonic() + max(0, min(timeout_seconds, MAX_WAIT_SECONDS))
        while True:
            a = await call(lambda c: c.get_approval(approval_id))
            if a["status"] != "pending" or a.get("awaiting_reply"):
                return _format_approval(a)
            if time.monotonic() >= deadline:
                return _format_approval(a) + "\nStill pending after the timeout; you may wait again."
            await asyncio.sleep(poll_seconds)

    @server.tool()
    async def announce_intent(project: str, patterns: list[str], description: str = "",
                              ttl_minutes: int = 60) -> str:
        """Declare the files/modules you are about to change (globs like 'src/api/**' or module names).

        If this overlaps another agent's active intent, the hub opens a coordination thread with them.
        Clear the intent with clear_intent when you are done.
        """
        r = await call(lambda c: c.announce_intent(project, patterns, description, ttl_minutes))
        lines = [f"Announced intent #{r['intent_id']} for {', '.join(r['patterns'])} (expires in {ttl_minutes} min)."]
        for cf in r["conflicts"]:
            pairs = ", ".join(f"{p} ~ {q}" for p, q in cf["overlap"][:10])
            lines.append(
                f"OVERLAP with {cf['other_agent']} (intent #{cf['other_intent']}): {pairs}. "
                f"Coordinate in channel '{cf['channel']}': post(project='{project}', channel='{cf['channel']}', "
                f"to='{cf['other_agent']}', body=...). Record the agreement with board_put(project='{project}', "
                f"key='{cf['board_key']}', ...). Avoid the overlapping paths until you agree."
            )
        return "\n".join(lines)

    @server.tool()
    async def clear_intent(intent_id: int) -> str:
        """Clear an intent you announced, once that work is finished."""
        await call(lambda c: c.clear_intent(intent_id))
        return f"Cleared intent #{intent_id}."

    @server.tool()
    async def ask_agent(project: str, to: str, question: str, timeout_seconds: int = 300) -> str:
        """Ask another agent a direct question and wait (up to timeout_seconds) for its answer.

        If it times out, the answer may still arrive later as a message in your inbox.
        """
        timeout = max(10, min(timeout_seconds, MAX_WAIT_SECONDS))
        ask = await call(lambda c: c.ask(project, to, question, timeout))
        deadline = time.monotonic() + timeout
        while True:
            ask = await call(lambda c: c.get_ask(ask["id"]))
            if ask["status"] == "answered":
                author = untrusted.author_label(ask["to_agent"], ask.get("to_name"))
                return f"Answer to ask #{ask['id']}:\n" + untrusted.wrap(
                    ask["answer"], author=author, kind="answer", ask_id=ask["id"])
            if ask["status"] == "expired" or time.monotonic() >= deadline:
                return (f"No answer to ask #{ask['id']} within {timeout}s. "
                        "If the agent answers later, it will arrive in your inbox.")
            await asyncio.sleep(poll_seconds)

    @server.tool()
    async def answer_ask(ask_id: int, answer: str) -> str:
        """Answer a question another agent asked you (see [ask #N] messages in your inbox)."""
        r = await call(lambda c: c.answer_ask(ask_id, answer))
        return f"Answered ask #{r['id']}."

    @server.tool()
    async def discuss_approval(approval_id: int, message: str) -> str:
        """Reply to (or message) the operator in the discussion thread of an approval you filed.

        Use it to explain why you need the approval, answer the operator's questions, or
        withdraw the request. Only you and the operator can see this thread.
        """
        a = await call(lambda c: c.discuss_approval(approval_id, message))
        return _format_approval(a) + "\nMessage sent. Call wait_for_approval to wait for the decision."

    @server.tool()
    async def prompt_status(prompt_id: int) -> str:
        """Show a prompt the operator sent you: their words, its mode and status, who else is on it (with
        their devices and current tasks) and the latest messages of its discussion thread."""
        p = await call(lambda c: c.get_prompt(prompt_id))
        th = p.get("thread")
        thread: list[dict[str, Any]] = []
        if th and th["kind"] == "channel":
            thread = await call(lambda c: c.recent_messages(th["project"], th["channel"], THREAD_TAIL))
        elif th:
            thread = await call(lambda c: c.device_history(THREAD_TAIL))
        return _format_prompt(p, thread[-THREAD_TAIL:])

    @server.tool()
    async def claim_share(prompt_id: int, title: str, description: str, project: str | None = None) -> str:
        """Claim your share of a prompt as a task (already approved: it came from the operator). Give it a
        short title and a description of exactly what you will do. For a prompt aimed at a device, set
        `project` (needed when you have no current task). You may claim once."""
        t = await call(lambda c: c.claim_share(prompt_id, title, description, project))
        return f"Claimed: task #{t['id']} {t['title']}. You'll pick it up next."

    @server.tool()
    async def decline_share(prompt_id: int, reason: str) -> str:
        """Take no share of a prompt (busy, wrong machine, someone else covers it) and say why."""
        await call(lambda c: c.decline_share(prompt_id, reason))
        return f"Declined your share of prompt #{prompt_id}."

    return server


def run_stdio() -> None:
    from agenthub.config import AgentConfig

    cfg = AgentConfig.load()
    client = HubClient.from_config(cfg)
    build_server(lambda: client).run()
