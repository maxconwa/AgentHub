"""Operator dashboard: mobile-first Jinja2 + HTMX pages under /ui.

Every route requires an authenticated operator (see :mod:`agenthub.hub.operator`)
and every POST also requires the CSRF token. Forms work without JavaScript
(POST, then redirect); HTMX only adds polling and in-place updates.

Agent-written text is shown inside blocks labeled with its author. Message bodies, descriptions,
notes, questions and answers go through the ``md`` filter (:func:`agenthub.hub.markdown.render`:
Markdown with raw HTML off and only http(s)/mailto links); everything else is autoescaped.
"""

from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
from functools import lru_cache
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agenthub.hub import (
    approvals, avatar_art, avatars, db, device_board, events, markdown, private, prompts, questions, reports, store,
    task_agents, tasks, usage, views,
)
from agenthub.hub.auth import get_conn
from agenthub.hub.bus import CLOSE_FORBIDDEN
from agenthub.hub.limits import LargeFormRoute
from agenthub.hub.operator import Operator, authenticate_operator, check_csrf, check_same_origin, csrf_token

HERE = Path(__file__).parent
templates = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
    ),
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}

# An avatar is a standalone SVG with no scripts and no agent-written text; its URL names the
# look and pose exactly, so it can be cached for good.
AVATAR_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "private, max-age=31536000, immutable",
}


@lru_cache(maxsize=4096)
def avatar_svg(version: int, seed: int, pose: str, scene: bool) -> str:
    return avatar_art.render_svg(version, seed, pose, scene=scene)

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


async def current_operator(request: Request, conn: Conn) -> Operator:
    return await authenticate_operator(request, conn)


Op = Annotated[Operator, Depends(current_operator)]


async def csrf_operator(request: Request, conn: Conn) -> Operator:
    operator = await authenticate_operator(request, conn)
    submitted = request.headers.get("x-csrf-token")
    if submitted is None:
        submitted = (await request.form()).get("csrf")
    check_csrf(request, operator, submitted if isinstance(submitted, str) else None)
    return operator


PostOp = Annotated[Operator, Depends(csrf_operator)]
# A form inside the slide-in panel names its item (so the response can re-render the panel) and the page under it.
PanelField = Annotated[str, Form(max_length=300)]
NextField = Annotated[str, Form(max_length=500)]
# A form inside the chat window names its conversation, so the response can re-render it.
MessengerField = Annotated[str, Form(max_length=300)]


# --- template helpers ------------------------------------------------------------

def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    secs = int((datetime.now(UTC) - then).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return f"{max(secs, 0)}s ago"


def agent_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Agent id -> name, plus ``device:<node_id>`` -> machine name for device board events."""
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")} | device_board.device_labels(conn)


def short_host(name: str | None) -> str:
    """A device name's first label (``white5070`` for ``white5070.tail1234.ts.net``); an IP address stays whole."""
    if not name:
        return ""
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        return name.split(".", 1)[0] or name


def who(actor: str | None, names: dict[str, str]) -> str:
    if not actor:
        return "?"
    if actor.startswith("human:"):
        return actor.removeprefix("human:")
    return names.get(actor, actor)


def event_summary(event: dict[str, Any], names: dict[str, str]) -> str:
    """One-line plain-text summary (escaped by the template)."""
    p, t = event["payload"], event["type"]
    snippet = lambda s: (s or "")[:160] + ("…" if s and len(s) > 160 else "")  # noqa: E731
    if t == "message.posted":
        to = f" → {who(p.get('to'), names)}" if p.get("to") else ""
        return f"#{p.get('channel')}{to}: {snippet(p.get('body'))}"
    if t == "private.sent":  # the event never carries the text
        direction = "to" if p.get("from") == "operator" else "from"
        return f"private message {direction} {who(p.get('agent_id'), names)}"
    if t == "device.message_posted":  # the event never carries the text
        return f"device board note on {names.get('device:' + str(p.get('node_id')), p.get('node_id'))}"
    if t == "board.updated":
        return f"board {p.get('key')} v{p.get('version')}"
    if t.startswith("task."):
        if t == "task.progress":
            return f"task #{p.get('task_id')}: " + " · ".join(snippet(i.get("text")) for i in p.get("items", [])[:3])
        if t == "task.transitioned":
            return f"task #{p.get('task_id')} {p.get('from')} → {p.get('to')} {snippet(p.get('note'))}"
        if t == "task.assigned":
            return f"task #{p.get('task_id')} assigned to {who(p.get('agent_id'), names) if p.get('agent_id') else 'nobody'}"
        if t.startswith("task.agent_"):
            return f"task #{p.get('task_id')} {t.removeprefix('task.').replace('_', ' ')}: {who(p.get('agent_id'), names)}"
        if t == "task.verdict":
            return f"task #{p.get('task_id')} verdict {p.get('verdict')} by {who(p.get('agent_id'), names)}"
        return f"task #{p.get('task_id')} {snippet(p.get('title') or p.get('outcome') or '')}".strip()
    if t.startswith("approval."):
        return f"approval #{p.get('approval_id')} {p.get('kind') or ''} {p.get('status') or ''} {snippet(p.get('body'))}"
    if t == "prompt.created":  # prompt events never carry the text
        to = ", ".join(who(a, names) for a in p.get("agents") or []) or "nobody online"
        skipped = ", ".join(who(a, names) for a in p.get("skipped") or [])
        return f"prompt #{p.get('prompt_id')} to {to} ({p.get('mode')})" + (f"; skipped {skipped}" if skipped else "")
    if t == "prompt.updated":
        if p.get("agent_id"):
            return f"prompt #{p.get('prompt_id')}: {who(p.get('agent_id'), names)} {p.get('recipient_status')}"
        late = [f"{', '.join(who(a, names) for a in p.get(k) or [])} {k.replace('_', ' ')}"
                for k in ("timed_out", "failed", "withdrawn") if p.get(k)]
        return f"prompt #{p.get('prompt_id')} {p.get('status')}" + (f": {'; '.join(late)}" if late else "")
    if t.startswith("report."):
        on = f" on task #{p.get('task_id')}" if p.get("task_id") else ""
        return f"report #{p.get('report_id')} {t.split('.')[1]}{on}: {snippet(p.get('title'))}"
    if t.startswith("question."):
        return f"question #{p.get('question_id')} {t.split('.')[1]} ({who(p.get('agent_id'), names)})"
    if t.startswith("ask."):
        return f"ask #{p.get('ask_id')} {snippet(p.get('question') or p.get('answer'))}"
    if t == "intent.announced":
        return f"intends to change {', '.join(p.get('patterns') or [])}"
    if t == "intent.conflict":
        pairs = ", ".join(f"{a} ~ {b}" for a, b in (p.get("overlap") or [])[:5])
        return f"overlap between {' and '.join(p.get('agents') or [])}: {pairs} → {p.get('channel')}"
    if t == "agent.enrolled":
        return f"{p.get('name')} enrolled from {(p.get('device') or {}).get('name')} (pending approval)"
    if t in ("agent.approved", "agent.revoked"):
        return f"{p.get('name')} {t.split('.')[1]}"
    if t == "agent.projects_changed":
        return f"{who(p.get('agent_id'), names)} projects: {', '.join(p.get('projects') or []) or 'none'}"
    if t in ("agent.connected", "agent.disconnected"):
        return t.split(".")[1]
    if t == "enrollment.created":
        return f"enrollment code for {', '.join(p.get('projects') or []) or 'no projects'}"
    if t == "channel.created":
        return f"new channel #{p.get('channel')}"
    if t == "project.created":
        return f"new project {event['project']}"
    return snippet(json.dumps(p))


templates.globals.update(ago=ago, who=who, event_summary=event_summary)
templates.filters["md"] = markdown.render
templates.filters["short_host"] = short_host


def pending_total(conn: sqlite3.Connection) -> int:
    """What the red badge counts: pending approvals plus pending questions to the operator."""
    return conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0] \
        + questions.pending_count(conn)


def live_poll(request: Request) -> bool:
    """Whether this is a page's live region polling itself (base.html: HTMX, aimed at ``#live``)."""
    return bool(request.headers.get("hx-request")) and request.headers.get("hx-target") == "live"


def same_origin(request: Request) -> bool:
    """Whether a GET came from our own pages (or was typed): only then may it mark messages read."""
    try:
        check_same_origin(request)
    except HTTPException:
        return False
    return True


def shows_page_chat(request: Request) -> bool:
    """Whether a chat page or agent page response puts its private chat in front of the operator: a page load or
    its live region's poll (which holds the chat) from our own pages. Other HTMX requests and cross-site
    requests never mark anything read."""
    return (not request.headers.get("hx-request") or live_poll(request)) and same_origin(request)


def render(request: Request, operator: Operator, name: str, conn: sqlite3.Connection, *, live_url: str | None = None,
           **ctx: Any) -> HTMLResponse:
    """A page (or fragment) with the shell's context. A page with a live region polls ``live_url`` (default: this
    URL, query included); its poll gets only that region back."""
    template = templates.get_template(name)
    pending, chat_unread = pending_total(conn), sum(private.unread_counts(conn).values())
    query = request.url.query
    html = template.render(
        operator=operator, csrf=csrf_token(request.app.state.hub.csrf_secret, operator.login),
        pending_count=pending, chat_unread=chat_unread, messenger_unread=chat_unread + questions.pending_count(conn),
        names=agent_names(conn), avatars=avatars.board(conn), path=request.url.path,
        live_url=live_url or request.url.path + (f"?{query}" if query else ""),
        live_only=live_poll(request) and "live" in template.blocks,
        active_ids={r[0] for r in conn.execute("SELECT id FROM agents WHERE status = 'active'")},
        **ctx,
    )
    return HTMLResponse(html, headers=SECURITY_HEADERS)


# A form's ``next`` may only lead back to one of our own pages (no other host, no header tricks).
SAFE_NEXT = re.compile(r"/ui/[A-Za-z0-9_\-./]*")


def safe_next(next: str) -> str:
    return next if SAFE_NEXT.fullmatch(next) and ".." not in next else ""


def target_page(target: str) -> str:
    """The page a message target lives on (the target is already known to exist)."""
    kind, _, key = target.partition(":")
    return f"/ui/{kind}s/{quote(key)}"


def done(request: Request, url: str) -> Response:
    """PRG for plain forms; HTMX follows HX-Redirect instead."""
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url, **SECURITY_HEADERS})
    return RedirectResponse(url, status_code=303, headers=SECURITY_HEADERS)


# The + button's forms, shown in the slide-in panel as items ``new:<kind>``.
NEW_FORMS = ("new:task", "new:project", "new:agent")


def render_new(request: Request, operator: Operator, conn: sqlite3.Connection, item: str, next: str,
               new_code: dict | None = None) -> HTMLResponse:
    """A New task / New project / New agent form in the slide-in panel (New agent shows a code once made)."""
    return render(request, operator, "_new.html", conn, item=item, kind=item.removeprefix("new:"), next=safe_next(next),
                  projects=store.list_projects(conn), new_code=new_code,
                  agents=[a for a in store.list_agents(conn) if a["status"] == "active"])


def render_panel(request: Request, operator: Operator, conn: sqlite3.Connection, item: str, next: str) -> HTMLResponse:
    """The slide-in panel fragment for ``item``; its forms post back with ``panel`` and ``next``."""
    if item in NEW_FORMS:
        return render_new(request, operator, conn, item, next)
    data = views.panel(conn, item)
    if data["kind"] == "agent" and data["chat"] and same_origin(request):  # the operator is looking at these now
        with db.transaction(conn):
            private.mark_read_by_operator(conn, data["ag"]["id"], since_id=data["chat"][0]["id"])
    return render(request, operator, "_panel.html", conn, item=item, next=safe_next(next), **data)


def render_conversation(request: Request, operator: Operator, conn: sqlite3.Connection, key: str) -> HTMLResponse:
    """One conversation in the chat window; an agent's private chat is marked read, as on its chat page."""
    data = views.conversation(conn, key)
    if data["kind"] == "agent" and data["messages"] and same_origin(request):  # the operator is looking at these now
        with db.transaction(conn):
            private.mark_read_by_operator(conn, data["ag"]["id"], since_id=data["messages"][0]["id"])
    return render(request, operator, "_messenger.html", conn, view="conversation", **data)


def finish(request: Request, operator: Operator, conn: sqlite3.Connection, panel: str, next: str,
           default: str, messenger: str = "") -> Response:
    """After a form post: re-render the panel or chat-window conversation it came from (HTMX), else redirect (PRG).

    The post has already committed, so a panel or conversation that can't be rendered (its item is gone)
    falls back to the redirect rather than an error that would invite posting again.
    """
    if (panel or messenger) and request.headers.get("hx-request"):
        try:
            if messenger:
                return render_conversation(request, operator, conn, messenger)
            return render_panel(request, operator, conn, panel, next)
        except store.NotFound:
            pass
    return done(request, safe_next(next) or default)


def render_event_row(event: dict[str, Any], names: dict[str, str]) -> str:
    return templates.get_template("_event_row.html").render(e=event, names=names)


def error_page(status: int, detail: str) -> HTMLResponse:
    html = templates.get_template("error.html").render(status=status, detail=detail)
    return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)


def written(text: str) -> str:
    """An operator's note, message or answer: refused (422) if it is only whitespace."""
    if not text.strip():
        raise HTTPException(422, "write something first: the text is empty")
    return text


def operator_moves(state: str) -> list[str]:
    return sorted(tasks.TRANSITIONS.get(state, ()))


# --- routes ------------------------------------------------------------------------

def build_router() -> APIRouter:
    router = APIRouter(prefix="/ui", route_class=LargeFormRoute)  # the operator's text has no length limit

    @router.get("")
    @router.get("/")
    def home(operator: Op) -> Response:
        return RedirectResponse("/ui/dashboard", status_code=303)

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        pending = list(reversed(approvals.list_approvals(conn, status="pending")))
        agents = views.agent_rows(conn)
        tally: dict[str, int] = {}
        for a in agents:
            tally[a["avatar"]["pose"]] = tally.get(a["avatar"]["pose"], 0) + 1
        tally_chips = [{"pose": p, "count": n, "label": avatar_art.POSE_LABELS[p]}
                       for p, n in sorted(tally.items(), key=lambda kv: avatar_art.POSES.index(kv[0]))]
        return render(request, operator, "dashboard.html", conn, nav="dashboard", pending=pending, agents=agents,
                      tally=tally_chips, questions=questions.pending(conn), prompts=views.prompt_overview(conn))

    @router.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "projects.html", conn, nav="projects", projects=views.project_tree(conn),
                      all_projects=store.list_projects(conn))

    @router.post("/projects")
    def project_create(request: Request, operator: PostOp, conn: Conn,
                       name: Annotated[str, Form(max_length=64)],
                       description: Annotated[str, Form()] = "",
                       panel: PanelField = "", next: NextField = "") -> Response:
        with db.transaction(conn):
            if not store.ensure_project(conn, name, actor=operator.actor, description=description):
                raise store.Conflict(f"project {name!r} already exists")
        if panel and request.headers.get("hx-request"):
            return render_panel(request, operator, conn, f"project:{name}", next)
        return done(request, f"/ui/projects/{name}")

    @router.get("/projects/{name}", response_class=HTMLResponse)
    def project_page(name: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        project = next((p for p in store.list_projects(conn) if p["name"] == name), None)
        if project is None:
            raise store.NotFound(f"no project {name!r}")
        live = next((p["tasks"] for p in views.project_tree(conn) if p["name"] == name), [])
        messages = store.recent_messages(conn, project=name, channel="general", limit=50)
        return render(request, operator, "project.html", conn, nav="projects", project=project, tasks=live,
                      messages=messages, prompt=views.prompt_state(conn, f"project:{name}"),
                      reports=reports.list_reports(conn, projects=[name]))

    @router.post("/projects/{name}/description")
    def project_description(name: str, request: Request, operator: PostOp, conn: Conn,
                            description: Annotated[str, Form(max_length=4000)] = "") -> Response:
        with db.transaction(conn):
            store.set_project_description(conn, name, description, actor=operator.actor)
        return done(request, f"/ui/projects/{name}")

    @router.post("/projects/{name}/notes")
    def project_note(name: str, request: Request, operator: PostOp, conn: Conn,
                     body: Annotated[str, Form(min_length=1, max_length=4000)],
                     panel: PanelField = "", next: NextField = "") -> Response:
        written(body)
        with db.transaction(conn):
            if not conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
                raise store.NotFound(f"no project {name!r}")
            store.post_message(conn, author=operator.actor, project=name, channel="general", body=body)
        return finish(request, operator, conn, panel, next, f"/ui/projects/{name}")

    @router.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "reports.html", conn, nav="reports", groups=reports.by_project(conn))

    @router.get("/reports/{report_id}", response_class=HTMLResponse)
    def report_page(report_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "report.html", conn, nav="reports", r=reports.get(conn, report_id),
                      sandbox=reports.SANDBOX)

    @router.get("/reports/{report_id}/page", response_class=HTMLResponse)
    def report_frame(report_id: int, operator: Op, conn: Conn) -> HTMLResponse:
        # Agent-written HTML, served only with the sandboxing headers (see reports.FRAME_HEADERS).
        return HTMLResponse(reports.html(conn, report_id), headers=reports.FRAME_HEADERS)

    @router.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request, operator: Op, conn: Conn, window: str = usage.DEFAULT_WINDOW) -> HTMLResponse:
        return render(request, operator, "usage.html", conn, nav="usage", u=usage.summary(conn, window))

    @router.get("/badge", response_class=HTMLResponse)
    def badge(operator: Op, conn: Conn) -> HTMLResponse:
        n = pending_total(conn)
        return HTMLResponse(f'<span class="badge">{n}</span>' if n else "", headers=SECURITY_HEADERS)

    @router.get("/badge/chats", response_class=HTMLResponse)
    def chats_badge(operator: Op, conn: Conn) -> HTMLResponse:
        n = sum(private.unread_counts(conn).values())
        return HTMLResponse(f'<span class="badge">{n}</span>' if n else "", headers=SECURITY_HEADERS)

    @router.get("/badge/messenger", response_class=HTMLResponse)
    def messenger_badge(operator: Op, conn: Conn) -> HTMLResponse:
        """The Chat button's count: unread private messages plus pending questions."""
        n = sum(private.unread_counts(conn).values()) + questions.pending_count(conn)
        html = f'<span class="fab-badge">{n}<span class="sr-only"> unread</span></span>' if n else ""
        return HTMLResponse(html, headers=SECURITY_HEADERS)

    # private chats --------------------------------------------------------------------

    @router.get("/chats", response_class=HTMLResponse)
    def chats_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "chats.html", conn, nav="chats", between_agents=views.agent_pairs(conn),
                      **views.chat_list(conn))

    # Registered before /chats/{agent_id}; "pair" can't be an agent id (those are "ag" + hex).
    @router.get("/chats/pair/{key}", response_class=HTMLResponse)
    def pair_page(key: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "pair_thread.html", conn, nav="chats", key=key, **views.pair_thread(conn, key))

    @router.post("/chats/pair/{key}")
    def pair_post(key: str, operator: PostOp) -> Response:
        raise HTTPException(405, "agent-to-agent chats are read-only")

    @router.get("/chats/{agent_id}", response_class=HTMLResponse)
    def chat_page(agent_id: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        rows = views.agent_rows(conn, agent_id=agent_id)
        if not rows:
            raise HTTPException(404, "no such agent")
        with db.transaction(conn):
            messages = private.thread(conn, agent_id)
            if messages and shows_page_chat(request):  # the operator is looking at these now
                private.mark_read_by_operator(conn, agent_id, since_id=messages[0]["id"])
        return render(request, operator, "chat.html", conn, nav="chats", ag=rows[0], messages=messages,
                      prompt=views.prompt_state(conn, f"agent:{agent_id}"))

    @router.post("/chats/{agent_id}")
    def chat_send(agent_id: str, operator: PostOp) -> Response:
        """Retired: agents are only prompted (POST /ui/prompts); their messages to the operator still arrive here."""
        raise HTTPException(405, "Send is gone: prompt the agent instead")

    # slide-in panel ------------------------------------------------------------------------

    @router.get("/panel", response_class=HTMLResponse)
    def panel_fragment(request: Request, operator: Op, conn: Conn, item: str = "", next: str = "") -> HTMLResponse:
        return render_panel(request, operator, conn, item[:300], next)

    # chat window -------------------------------------------------------------------------------

    @router.get("/messenger", response_class=HTMLResponse)
    def messenger_list(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "_messenger.html", conn, view="list", **views.messenger(conn))

    @router.get("/messenger/{key}", response_class=HTMLResponse)
    def messenger_conversation(key: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render_conversation(request, operator, conn, key[:300])

    # the Prompt sheet -------------------------------------------------------------------------

    @router.get("/compose", response_class=HTMLResponse)
    def compose_page(request: Request, operator: Op, conn: Conn, to: str = "", next: str = "") -> HTMLResponse:
        reach = views.recipients(conn, to)
        next = safe_next(next)
        # HTMX loads just the panel into #compose; a plain link gets the whole page.
        name = "_compose.html" if request.headers.get("hx-request") else "compose.html"
        return render(request, operator, name, conn, nav="", to=to, next=next, back=next or target_page(to),
                      prompt=views.prompt_state(conn, to, reach), **reach)

    @router.get("/compose/close", response_class=HTMLResponse)
    def compose_close(operator: Op) -> HTMLResponse:
        return HTMLResponse("", headers=SECURITY_HEADERS)

    @router.post("/compose")
    def compose_send(operator: PostOp) -> Response:
        """Retired: the Prompt sheet posts to /ui/prompts, and boards take posts on the Board."""
        raise HTTPException(405, "Send is gone: use Prompt, or post on the board")

    # prompts: wake the agents now ----------------------------------------------------------

    @router.post("/prompts")
    def prompt_create(request: Request, operator: PostOp, conn: Conn,
                      target: Annotated[str, Form(max_length=200)],
                      body: Annotated[str, Form(min_length=1, max_length=prompts.MAX_BODY_CHARS)],
                      panel: PanelField = "", next: NextField = "", messenger: MessengerField = "") -> Response:
        """Every composer's Prompt button, with the composer's ``target`` (a board's Post shares its form)."""
        written(body)
        with db.transaction(conn):
            p = prompts.create(conn, target=target, body=body, actor=operator.actor)
        return finish(request, operator, conn, panel, next, f"/ui/prompts/{p['id']}", messenger)

    @router.post("/prompts/{prompt_id}/withdraw")
    def prompt_withdraw(prompt_id: int, request: Request, operator: PostOp, conn: Conn,
                        panel: PanelField = "", next: NextField = "", messenger: MessengerField = "") -> Response:
        """A prompt card's Withdraw: the recipients that haven't started don't get it (running ones finish)."""
        with db.transaction(conn):
            prompts.withdraw(conn, prompt_id, actor=operator.actor)
        return finish(request, operator, conn, panel, next, f"/ui/prompts/{prompt_id}", messenger)

    @router.get("/prompts/{prompt_id}", response_class=HTMLResponse)
    def prompt_page(prompt_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "prompt.html", conn, nav="dashboard", p=views.prompt(conn, prompt_id))

    # board (public channels) ----------------------------------

    @router.get("/board", response_class=HTMLResponse)
    def board_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "board.html", conn, nav="board", activity_link=views.ACTIVITY_LINK,
                      **views.board_index(conn))

    @router.get("/board/{key}", response_class=HTMLResponse)
    def board_thread_page(key: str, request: Request, operator: Op, conn: Conn) -> Response:
        if key.startswith("pair-"):  # agent-to-agent chats moved to Chats
            return RedirectResponse(f"/ui/chats/pair/{key}", status_code=308, headers=SECURITY_HEADERS)
        thread = views.board_thread(conn, key)
        target = views.board_prompt_target(conn, thread["project"], thread["channel"])
        return render(request, operator, "board_thread.html", conn, nav="board", key=key,
                      prompt=views.prompt_state(conn, target) if target else None, **thread)

    @router.post("/board/{key}")
    def board_post(key: str, request: Request, operator: PostOp, conn: Conn,
                   body: Annotated[str, Form(min_length=1, max_length=4000)],
                   messenger: MessengerField = "") -> Response:
        written(body)
        thread = views.board_thread(conn, key)
        if not thread["can_post"]:
            raise HTTPException(405, "this thread is read-only")
        with db.transaction(conn):
            store.post_message(conn, author=operator.actor, project=thread["project"], channel=thread["channel"],
                               body=body)
        return finish(request, operator, conn, "", "", f"/ui/board/{key}", messenger)

    # approvals ----------------------------------------------------------------------

    @router.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        pending = list(reversed(approvals.list_approvals(conn, status="pending")))
        recent = [a for a in approvals.list_approvals(conn, status=None, limit=40) if a["status"] != "pending"][:20]
        return render(request, operator, "approvals.html", conn, nav="approvals", pending=pending, recent=recent,
                      questions=questions.pending(conn), answered=questions.recently_answered(conn))

    @router.get("/questions/{qid}", response_class=HTMLResponse)
    def question_page(qid: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        q = questions.get(conn, qid)
        task = tasks.get_task(conn, q["task_id"]) if q["task_id"] else None
        return render(request, operator, "question.html", conn, nav="approvals", q=q, task=task,
                      delivery=views.answer_delivery(conn, q))

    @router.post("/questions/{qid}/answer")
    def question_answer(qid: int, request: Request, operator: PostOp, conn: Conn,
                        answer: Annotated[str, Form(min_length=1, max_length=questions.MAX_CHARS)],
                        panel: PanelField = "", next: NextField = "") -> Response:
        written(answer)
        with db.transaction(conn):
            questions.answer(conn, qid, answer=answer, actor=operator.actor)
        return finish(request, operator, conn, panel, next, f"/ui/questions/{qid}")

    @router.get("/approvals/{approval_id}", response_class=HTMLResponse)
    def approval_page(approval_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        a = approvals.get(conn, approval_id)
        task = tasks.get_task(conn, a["details"]["task_id"]) if a["details"].get("task_id") else None
        return render(request, operator, "approval.html", conn, nav="approvals", a=a, task=task)

    @router.get("/approvals/{approval_id}/thread", response_class=HTMLResponse)
    def approval_thread(approval_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "_thread.html", conn, a=approvals.get(conn, approval_id))

    @router.get("/approvals/{approval_id}/discussion", response_class=HTMLResponse)
    def approval_discussion(approval_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        """The slide-in panel's discussion thread (it polls this); 286 tells htmx to stop once decided."""
        a = approvals.get(conn, approval_id)
        resp = render(request, operator, "_discussion.html", conn, a=a)
        if a["status"] != "pending":
            resp.status_code = 286
        return resp

    @router.post("/approvals/{approval_id}/discuss")
    def approval_discuss(approval_id: int, request: Request, operator: PostOp, conn: Conn,
                         body: Annotated[str, Form(min_length=1, max_length=4000)],
                         panel: PanelField = "", next: NextField = "") -> Response:
        written(body)
        with db.transaction(conn):
            a = approvals.post_discussion(conn, approval_id, author=operator.actor, body=body)
        if request.headers.get("hx-request") and not panel:  # the approval page's own thread
            return render(request, operator, "_thread.html", conn, a=a)
        return finish(request, operator, conn, panel, next, f"/ui/approvals/{approval_id}")

    @router.post("/approvals/{approval_id}/decide")
    def approval_decide(approval_id: int, request: Request, operator: PostOp, conn: Conn,
                        decision: Annotated[str, Form(pattern="^(approve|reject)$")],
                        note: Annotated[str, Form(max_length=2000)] = "",
                        panel: PanelField = "", next: NextField = "") -> Response:
        with db.transaction(conn):
            approvals.decide(conn, approval_id, approve=decision == "approve", actor=operator.actor, note=note)
        return finish(request, operator, conn, panel, next, f"/ui/approvals/{approval_id}")

    # tasks ------------------------------------------------------------------------------

    @router.get("/tasks", response_class=HTMLResponse)
    def tasks_page(request: Request, operator: Op, conn: Conn, project: str = "", state: str = "") -> HTMLResponse:
        state = state if state in tasks.STATES else ""
        rows = tasks.list_tasks(conn, projects=[project] if project else None, state=state or None, limit=500,
                                newest_first=True)
        agents = [a for a in store.list_agents(conn) if a["status"] == "active"]
        return render(request, operator, "tasks.html", conn, nav="tasks", rows=rows, project=project, state=state,
                      states=tasks.STATES, projects=store.list_projects(conn), agents=agents)

    @router.post("/tasks")
    def task_create(request: Request, operator: PostOp, conn: Conn,
                    project: Annotated[str, Form(max_length=64)],
                    title: Annotated[str, Form(min_length=1, max_length=200)],
                    body: Annotated[str, Form(max_length=16000)] = "",
                    assignee: Annotated[str, Form(max_length=64)] = "",
                    role: Annotated[str, Form(max_length=task_agents.MAX_ROLE_CHARS)] = "",
                    panel: PanelField = "", next: NextField = "") -> Response:
        with db.transaction(conn):  # no budget: runners since 0.3.5 run a task until it's done
            t = tasks.create_task(conn, actor=operator.actor, project=project, title=title, body=body,
                                  assignee=assignee or None, role=role, state="open")
        if panel and request.headers.get("hx-request"):  # from the + button: show the new task's panel
            return render_panel(request, operator, conn, f"task:{t['id']}", next)
        return done(request, f"/ui/tasks/{t['id']}")

    @router.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_page(task_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        t = tasks.get_task(conn, task_id)
        rows = conn.execute(
            # A range on type (not LIKE, which can't use an index) walks events_type_id over task events only.
            "SELECT * FROM events WHERE type >= 'task.' AND type < 'task/' AND json_extract(payload, '$.task_id') = ? "
            "ORDER BY id DESC LIMIT 100", (task_id,)).fetchall()
        history = [events.row_to_event(r) for r in rows]
        agents = [a for a in store.list_agents(conn) if a["status"] == "active" and t["project"] in a["allowed_projects"]]
        member_usage = {m["agent_id"]: tasks.usage(conn, task_id, m["agent_id"]) for m in t["agents"]}
        return render(request, operator, "task.html", conn, nav="tasks", t=t, history=history, runs=tasks.list_runs(conn, task_id),
                      usage=tasks.usage(conn, task_id), member_usage=member_usage,
                      messages=store.recent_messages(conn, project=t["project"], channel=f"task-{task_id}", limit=50),
                      moves=operator_moves(t["state"]), agents=agents,
                      terminal=t["state"] in tasks.TERMINAL, prompt=views.prompt_state(conn, f"task:{task_id}"),
                      reports=reports.list_reports(conn, task_id=task_id))

    @router.post("/tasks/{task_id}/description")
    def task_description(task_id: int, request: Request, operator: PostOp, conn: Conn,
                         title: Annotated[str, Form(min_length=1, max_length=tasks.MAX_TITLE_CHARS)],
                         body: Annotated[str, Form(max_length=tasks.MAX_BODY_CHARS)] = "") -> Response:
        with db.transaction(conn):
            tasks.set_description(conn, task_id, title, body, actor=operator.actor)
        return done(request, f"/ui/tasks/{task_id}")

    @router.post("/tasks/{task_id}/notes")
    def task_note(task_id: int, request: Request, operator: PostOp, conn: Conn,
                  body: Annotated[str, Form(min_length=1, max_length=4000)],
                  panel: PanelField = "", next: NextField = "") -> Response:
        written(body)
        with db.transaction(conn):
            t = tasks.get_task(conn, task_id)
            store.post_message(conn, author=operator.actor, project=t["project"], channel=f"task-{task_id}", body=body)
        return finish(request, operator, conn, panel, next, f"/ui/tasks/{task_id}")

    @router.post("/tasks/{task_id}/assign")
    def task_assign(task_id: int, request: Request, operator: PostOp, conn: Conn,
                    assignee: Annotated[str, Form(max_length=64)] = "") -> Response:
        with db.transaction(conn):
            tasks.assign(conn, task_id, assignee or None, actor=operator.actor)
        return done(request, f"/ui/tasks/{task_id}")

    @router.post("/tasks/{task_id}/agents")
    def task_agent_add(task_id: int, request: Request, operator: PostOp, conn: Conn,
                       agent: Annotated[str, Form(min_length=1, max_length=64)],
                       stage: Annotated[str, Form(max_length=16)],
                       role: Annotated[str, Form(max_length=task_agents.MAX_ROLE_CHARS)] = "") -> Response:
        with db.transaction(conn):
            task_agents.add_agent(conn, task_id, agent, role=role, stage=stage, actor=operator.actor)
        return done(request, f"/ui/tasks/{task_id}")

    @router.post("/tasks/{task_id}/agents/{agent_id}/remove")
    def task_agent_remove(task_id: int, agent_id: str, request: Request, operator: PostOp, conn: Conn) -> Response:
        with db.transaction(conn):
            task_agents.remove_agent(conn, task_id, agent_id, actor=operator.actor)
        return done(request, f"/ui/tasks/{task_id}")

    @router.post("/tasks/{task_id}/agents/{agent_id}/update")
    def task_agent_update(task_id: int, agent_id: str, request: Request, operator: PostOp, conn: Conn,
                          role: Annotated[str, Form(max_length=task_agents.MAX_ROLE_CHARS)],
                          stage: Annotated[str, Form(max_length=16)]) -> Response:
        with db.transaction(conn):
            task_agents.update_agent(conn, task_id, agent_id, actor=operator.actor, role=role, stage=stage)
        return done(request, f"/ui/tasks/{task_id}")

    @router.post("/tasks/{task_id}/move")
    def task_move(task_id: int, request: Request, operator: PostOp, conn: Conn,
                  state: Annotated[str, Form(max_length=32)],
                  note: Annotated[str, Form(max_length=4000)] = "") -> Response:
        with db.transaction(conn):
            tasks.transition(conn, task_id, state, actor=operator.actor, role="operator", note=note)
        return done(request, f"/ui/tasks/{task_id}")

    # feed and search ---------------------------------------------------------------

    @router.get("/feed")
    def feed_moved(request: Request, operator: Op) -> RedirectResponse:
        # Old ntfy links and bookmarks point here.
        q = request.url.query
        return RedirectResponse("/ui/activity" + (f"?{q}" if q else ""), status_code=308, headers=SECURITY_HEADERS)

    @router.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, operator: Op, conn: Conn, project: str = "", agent: str = "",
                  type: str = "") -> HTMLResponse:
        sql, params = "SELECT * FROM events WHERE 1 = 1", []
        if project:
            sql += " AND project = ?"
            params.append(project)
        if agent:
            sql += " AND actor = ?"
            params.append(agent)
        if type:
            sql += " AND type LIKE ?"
            params.append(type.replace("%", "") + "%")
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT 200", params).fetchall()
        types = [r[0] for r in conn.execute("SELECT DISTINCT type FROM events ORDER BY type")]
        return render(request, operator, "activity.html", conn, nav="board", events=[events.row_to_event(r) for r in rows],
                      projects=store.list_projects(conn), agents=store.list_agents(conn), types=types,
                      filters={"project": project, "agent": agent, "type": type})

    @router.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, operator: Op, conn: Conn, q: str = "") -> HTMLResponse:
        results = events.search(conn, q[:500], projects=None, limit=50) if q.strip() else []
        return render(request, operator, "search.html", conn, q=q, results=results)

    @router.get("/devices", response_class=HTMLResponse)
    def devices_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "devices.html", conn, nav="devices", devices=views.devices(conn))

    @router.get("/devices/{node_id}", response_class=HTMLResponse)
    def device_page(node_id: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        device = views.device(conn, node_id)
        return render(request, operator, "device.html", conn, nav="devices", d=device,
                      messages=views.device_messages(conn, node_id),
                      prompt=views.prompt_state(conn, f"device:{node_id}"))

    @router.post("/devices/{node_id}/notes")
    def device_note(node_id: str, request: Request, operator: PostOp, conn: Conn,
                    body: Annotated[str, Form(min_length=1, max_length=device_board.MAX_BODY_CHARS)],
                    panel: PanelField = "", next: NextField = "", messenger: MessengerField = "") -> Response:
        written(body)
        with db.transaction(conn):
            device_board.post(conn, node_id, author=operator.actor, body=body)
        return finish(request, operator, conn, panel, next, f"/ui/devices/{quote(node_id, safe='')}", messenger)

    # agents -------------------------------------------------------------------------

    def render_agents(request: Request, operator: Operator, conn: sqlite3.Connection, new_code: dict | None) -> HTMLResponse:
        return render(request, operator, "agents.html", conn, nav="agents", agents=views.agent_rows(conn),
                      projects=store.list_projects(conn), new_code=new_code, live_url="/ui/agents")

    @router.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render_agents(request, operator, conn, None)

    @router.post("/agents/enroll", response_class=HTMLResponse)
    def agents_enroll(request: Request, operator: PostOp, conn: Conn,
                      projects: Annotated[str, Form(max_length=1000)] = "",
                      panel: PanelField = "", next: NextField = "") -> HTMLResponse:
        names = [p.strip() for p in projects.split(",") if p.strip()]
        with db.transaction(conn):
            code = store.create_enrollment(conn, created_by=operator.actor, projects=names)
        # Shown once, never stored in plain text.
        if panel and request.headers.get("hx-request"):  # from the + button's New agent form
            return render_new(request, operator, conn, "new:agent", next, {"code": code, "projects": names})
        return render_agents(request, operator, conn, {"code": code, "projects": names})

    @router.get("/agents/{agent_id}", response_class=HTMLResponse)
    def agent_page(agent_id: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        rows = views.agent_rows(conn, agent_id=agent_id)
        agent = store.get_agent(conn, agent_id)
        if not rows or agent is None:
            raise HTTPException(404, "no such agent")
        with db.transaction(conn):
            chat = private.thread(conn, agent_id, limit=20)
            if chat and shows_page_chat(request):  # the pinned chat shows these; older unread ones wait for the full chat
                private.mark_read_by_operator(conn, agent_id, since_id=chat[0]["id"])
        return render(request, operator, "agent.html", conn, nav="agents", ag=rows[0], allowed=agent.allowed_projects,
                      prompt=views.prompt_state(conn, f"agent:{agent_id}"),
                      device_ip=agent.device_ip, pending=approvals.list_for_agent(conn, agent_id),
                      chat=chat, pairs=[p for p in views.agent_pairs(conn) if agent_id in (a["id"] for a in p["agents"])])

    @router.post("/agents/{agent_id}/approve")
    def agent_approve(agent_id: str, request: Request, operator: PostOp, conn: Conn) -> Response:
        with db.transaction(conn):
            approvals.set_agent_status(conn, agent_id, "active", actor=operator.actor)
        return done(request, f"/ui/agents/{agent_id}")

    @router.post("/agents/{agent_id}/revoke")
    def agent_revoke(agent_id: str, request: Request, operator: PostOp, conn: Conn,
                     confirm: Annotated[str, Form(max_length=64)] = "") -> Response:
        agent = store.find_agent(conn, agent_id)
        if confirm != agent.name:
            raise HTTPException(422, "type the agent's name to confirm revocation")
        with db.transaction(conn):
            approvals.set_agent_status(conn, agent_id, "revoked", actor=operator.actor, note="revoked")
        return done(request, f"/ui/agents/{agent_id}")

    @router.post("/agents/{agent_id}/projects")
    def agent_projects(agent_id: str, request: Request, operator: PostOp, conn: Conn,
                       projects: Annotated[str, Form(max_length=1000)] = "") -> Response:
        with db.transaction(conn):
            store.set_agent_projects(conn, agent_id, [p.strip() for p in projects.split(",") if p.strip()],
                                     actor=operator.actor)
        return done(request, f"/ui/agents/{agent_id}")

    @router.post("/agents/{agent_id}/avatar")
    def agent_avatar_reroll(agent_id: str, request: Request, operator: PostOp, conn: Conn) -> Response:
        with db.transaction(conn):
            avatars.reroll(conn, agent_id, actor=operator.actor)
        return done(request, f"/ui/agents/{agent_id}")

    @router.get("/avatars/{version}/{seed}/{kind}/{pose}.svg")
    def avatar_image(version: str, seed: str, kind: str, pose: str, request: Request, operator: Op) -> Response:
        # Only our own pages embed avatars; another site must not probe for the operator with them.
        check_same_origin(request)
        if not (version.isdigit() and seed.isdigit() and len(seed) <= 10) or kind not in ("scene", "mini"):
            raise HTTPException(404, "no such avatar")
        try:
            svg = avatar_svg(int(version), int(seed), pose, kind == "scene")
        except ValueError:
            raise HTTPException(404, "no such avatar") from None
        return Response(svg, media_type="image/svg+xml", headers=AVATAR_HEADERS)

    # live feed WebSocket ------------------------------------------------------------

    @router.websocket("/ws")
    async def operator_socket(websocket: WebSocket) -> None:
        state = websocket.app.state.hub
        conn = state.connect()
        try:
            try:
                check_same_origin(websocket)  # blocks cross-site WebSocket hijacking
                await authenticate_operator(websocket, conn)
            except HTTPException as exc:
                await websocket.close(code=CLOSE_FORBIDDEN, reason=str(exc.detail)[:120])
                return
            await websocket.accept()
            state.bus.add_operator(websocket)
            try:
                while True:
                    await websocket.receive_text()  # keepalive pings; content ignored
            except WebSocketDisconnect:
                pass
            finally:
                state.bus.remove_operator(websocket)
        finally:
            conn.close()

    return router


def feed_query(filters: dict[str, str]) -> str:
    return urlencode({k: v for k, v in filters.items() if v})


templates.globals.update(feed_query=feed_query)
