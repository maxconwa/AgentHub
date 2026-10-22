"""Operator dashboard: mobile-first Jinja2 + HTMX pages under /ui.

Every route requires an authenticated operator (see :mod:`agenthub.hub.operator`)
and every POST also requires the CSRF token. Forms work without JavaScript
(POST, then redirect); HTMX only adds polling and in-place updates.

Agent-written text is rendered with Jinja2 autoescaping and shown inside
blocks labeled with its author; it is never rendered as HTML or Markdown.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agenthub.hub import approvals, avatar_art, avatars, db, events, store, task_agents, tasks, views
from agenthub.hub.auth import get_conn
from agenthub.hub.bus import CLOSE_FORBIDDEN
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
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM agents")}


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


def render(request: Request, operator: Operator, name: str, conn: sqlite3.Connection, **ctx: Any) -> HTMLResponse:
    pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
    html = templates.get_template(name).render(
        operator=operator, csrf=csrf_token(request.app.state.hub.csrf_secret, operator.login),
        pending_count=pending, names=agent_names(conn), avatars=avatars.board(conn), path=request.url.path,
        **ctx,
    )
    return HTMLResponse(html, headers=SECURITY_HEADERS)


def done(request: Request, url: str) -> Response:
    """PRG for plain forms; HTMX follows HX-Redirect instead."""
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url, **SECURITY_HEADERS})
    return RedirectResponse(url, status_code=303, headers=SECURITY_HEADERS)


def render_event_row(event: dict[str, Any], names: dict[str, str]) -> str:
    return templates.get_template("_event_row.html").render(e=event, names=names)


def error_page(status: int, detail: str) -> HTMLResponse:
    html = templates.get_template("error.html").render(status=status, detail=detail)
    return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)


def operator_moves(state: str) -> list[str]:
    return sorted(tasks.TRANSITIONS.get(state, ()))


# --- routes ------------------------------------------------------------------------

def build_router() -> APIRouter:
    router = APIRouter(prefix="/ui")

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
                      tally=tally_chips)

    @router.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "projects.html", conn, nav="projects", projects=views.project_tree(conn),
                      all_projects=store.list_projects(conn))

    @router.get("/projects/{name}", response_class=HTMLResponse)
    def project_page(name: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        project = next((p for p in store.list_projects(conn) if p["name"] == name), None)
        if project is None:
            raise store.NotFound(f"no project {name!r}")
        live = next((p["tasks"] for p in views.project_tree(conn) if p["name"] == name), [])
        messages = store.recent_messages(conn, project=name, channel="general", limit=50)
        return render(request, operator, "project.html", conn, nav="projects", project=project, tasks=live,
                      messages=messages)

    @router.post("/projects/{name}/description")
    def project_description(name: str, request: Request, operator: PostOp, conn: Conn,
                            description: Annotated[str, Form(max_length=4000)] = "") -> Response:
        with db.transaction(conn):
            store.set_project_description(conn, name, description, actor=operator.actor)
        return done(request, f"/ui/projects/{name}")

    @router.post("/projects/{name}/notes")
    def project_note(name: str, request: Request, operator: PostOp, conn: Conn,
                     body: Annotated[str, Form(min_length=1, max_length=4000)]) -> Response:
        with db.transaction(conn):
            if not conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
                raise store.NotFound(f"no project {name!r}")
            store.post_message(conn, author=operator.actor, project=name, channel="general", body=body)
        return done(request, f"/ui/projects/{name}")

    @router.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "reports.html", conn, nav="reports")

    @router.get("/badge", response_class=HTMLResponse)
    def badge(operator: Op, conn: Conn) -> HTMLResponse:
        n = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
        return HTMLResponse(f'<span class="badge">{n}</span>' if n else "", headers=SECURITY_HEADERS)

    # approvals ----------------------------------------------------------------------

    @router.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        pending = list(reversed(approvals.list_approvals(conn, status="pending")))
        recent = [a for a in approvals.list_approvals(conn, status=None, limit=40) if a["status"] != "pending"][:20]
        return render(request, operator, "approvals.html", conn, nav="approvals", pending=pending, recent=recent)

    @router.get("/approvals/{approval_id}", response_class=HTMLResponse)
    def approval_page(approval_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        a = approvals.get(conn, approval_id)
        task = tasks.get_task(conn, a["details"]["task_id"]) if a["details"].get("task_id") else None
        return render(request, operator, "approval.html", conn, nav="approvals", a=a, task=task)

    @router.get("/approvals/{approval_id}/thread", response_class=HTMLResponse)
    def approval_thread(approval_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render(request, operator, "_thread.html", conn, a=approvals.get(conn, approval_id))

    @router.post("/approvals/{approval_id}/discuss")
    def approval_discuss(approval_id: int, request: Request, operator: PostOp, conn: Conn,
                         body: Annotated[str, Form(min_length=1, max_length=4000)]) -> Response:
        with db.transaction(conn):
            a = approvals.post_discussion(conn, approval_id, author=operator.actor, body=body)
        if request.headers.get("hx-request"):
            return render(request, operator, "_thread.html", conn, a=a)
        return done(request, f"/ui/approvals/{approval_id}")

    @router.post("/approvals/{approval_id}/decide")
    def approval_decide(approval_id: int, request: Request, operator: PostOp, conn: Conn,
                        decision: Annotated[str, Form(pattern="^(approve|reject)$")],
                        note: Annotated[str, Form(max_length=2000)] = "") -> Response:
        with db.transaction(conn):
            approvals.decide(conn, approval_id, approve=decision == "approve", actor=operator.actor, note=note)
        return done(request, f"/ui/approvals/{approval_id}")

    # tasks ------------------------------------------------------------------------------

    @router.get("/tasks", response_class=HTMLResponse)
    def tasks_page(request: Request, operator: Op, conn: Conn, project: str = "", state: str = "") -> HTMLResponse:
        state = state if state in tasks.STATES else ""
        rows = tasks.list_tasks(conn, projects=[project] if project else None, state=state or None, limit=500,
                                newest_first=True)
        agents = [a for a in store.list_agents(conn) if a["status"] == "active"]
        return render(request, operator, "tasks.html", conn, nav="tasks", rows=rows, project=project, state=state,
                      states=tasks.STATES, projects=store.list_projects(conn), agents=agents,
                      default_budget=tasks.DEFAULT_BUDGET)

    @router.post("/tasks")
    def task_create(request: Request, operator: PostOp, conn: Conn,
                    project: Annotated[str, Form(max_length=64)],
                    title: Annotated[str, Form(min_length=1, max_length=200)],
                    body: Annotated[str, Form(max_length=16000)] = "",
                    assignee: Annotated[str, Form(max_length=64)] = "",
                    role: Annotated[str, Form(max_length=task_agents.MAX_ROLE_CHARS)] = "",
                    max_turns: Annotated[int | None, Form()] = None,
                    wall_clock_minutes: Annotated[int | None, Form()] = None,
                    max_subtasks: Annotated[int | None, Form()] = None) -> Response:
        budget = {k: v for k, v in {"max_turns": max_turns, "wall_clock_minutes": wall_clock_minutes,
                                     "max_subtasks": max_subtasks}.items() if v is not None}
        with db.transaction(conn):
            t = tasks.create_task(conn, actor=operator.actor, project=project, title=title, body=body,
                                  assignee=assignee or None, role=role, budget=budget,
                                  state="open")
        return done(request, f"/ui/tasks/{t['id']}")

    @router.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_page(task_id: int, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        t = tasks.get_task(conn, task_id)
        rows = conn.execute(
            "SELECT * FROM events WHERE type LIKE 'task.%' AND json_extract(payload, '$.task_id') = ? "
            "ORDER BY id DESC LIMIT 100", (task_id,)).fetchall()
        history = [events.row_to_event(r) for r in rows]
        agents = [a for a in store.list_agents(conn) if a["status"] == "active" and t["project"] in a["allowed_projects"]]
        member_usage = {m["agent_id"]: tasks.usage(conn, task_id, m["agent_id"]) for m in t["agents"]}
        return render(request, operator, "task.html", conn, nav="tasks", t=t, history=history, runs=tasks.list_runs(conn, task_id),
                      usage=tasks.usage(conn, task_id), member_usage=member_usage,
                      messages=store.recent_messages(conn, project=t["project"], channel=f"task-{task_id}", limit=50),
                      moves=operator_moves(t["state"]), agents=agents,
                      terminal=t["state"] in tasks.TERMINAL)

    @router.post("/tasks/{task_id}/description")
    def task_description(task_id: int, request: Request, operator: PostOp, conn: Conn,
                         title: Annotated[str, Form(min_length=1, max_length=tasks.MAX_TITLE_CHARS)],
                         body: Annotated[str, Form(max_length=tasks.MAX_BODY_CHARS)] = "") -> Response:
        with db.transaction(conn):
            tasks.set_description(conn, task_id, title, body, actor=operator.actor)
        return done(request, f"/ui/tasks/{task_id}")

    @router.post("/tasks/{task_id}/notes")
    def task_note(task_id: int, request: Request, operator: PostOp, conn: Conn,
                  body: Annotated[str, Form(min_length=1, max_length=4000)]) -> Response:
        with db.transaction(conn):
            t = tasks.get_task(conn, task_id)
            store.post_message(conn, author=operator.actor, project=t["project"], channel=f"task-{task_id}", body=body)
        return done(request, f"/ui/tasks/{task_id}")

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

    @router.get("/feed", response_class=HTMLResponse)
    def feed_page(request: Request, operator: Op, conn: Conn, project: str = "", agent: str = "",
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
        return render(request, operator, "feed.html", conn, events=[events.row_to_event(r) for r in rows],
                      projects=store.list_projects(conn), agents=store.list_agents(conn), types=types,
                      filters={"project": project, "agent": agent, "type": type})

    @router.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, operator: Op, conn: Conn, q: str = "") -> HTMLResponse:
        results = events.search(conn, q[:500], projects=None, limit=50) if q.strip() else []
        return render(request, operator, "search.html", conn, q=q, results=results)

    # agents -------------------------------------------------------------------------

    def render_agents(request: Request, operator: Operator, conn: sqlite3.Connection, new_code: dict | None) -> HTMLResponse:
        return render(request, operator, "agents.html", conn, nav="agents", agents=views.agent_rows(conn),
                      projects=store.list_projects(conn), new_code=new_code)

    @router.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        return render_agents(request, operator, conn, None)

    @router.post("/agents/enroll", response_class=HTMLResponse)
    def agents_enroll(request: Request, operator: PostOp, conn: Conn,
                      projects: Annotated[str, Form(max_length=1000)] = "") -> HTMLResponse:
        names = [p.strip() for p in projects.split(",") if p.strip()]
        with db.transaction(conn):
            code = store.create_enrollment(conn, created_by=operator.actor, projects=names)
        # Shown once, never stored in plain text.
        return render_agents(request, operator, conn, {"code": code, "projects": names})

    @router.get("/agents/{agent_id}", response_class=HTMLResponse)
    def agent_page(agent_id: str, request: Request, operator: Op, conn: Conn) -> HTMLResponse:
        rows = views.agent_rows(conn, agent_id=agent_id)
        agent = store.get_agent(conn, agent_id)
        if not rows or agent is None:
            raise HTTPException(404, "no such agent")
        return render(request, operator, "agent.html", conn, nav="agents", ag=rows[0], allowed=agent.allowed_projects,
                      device_ip=agent.device_ip, pending=approvals.list_for_agent(conn, agent_id))

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
