"""Operator dashboard: human identity, CSRF, and every operator endpoint refusing agents."""

import re

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agenthub.hub import approvals, db, store, task_agents, tasks

from .conftest import OP_IP, OP_LOGIN, OPERATOR


def _walk(routes):
    for route in routes:
        # FastAPI 0.158 wraps included routers; descend into them.
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield route


def ui_routes(app):
    """Every operator route the app actually serves (so new routes are covered automatically)."""
    out = []
    for route in _walk(app.routes):
        if isinstance(route, APIRoute) and route.path.startswith("/ui"):
            for method in route.methods - {"HEAD"}:
                out.append((method, route.path))
    routes = sorted(set(out))
    assert len(routes) >= 15, f"route inventory looks wrong: {routes}"
    return routes


def fill(path, ids):
    return re.sub(r"\{(\w+)\}", lambda m: str(ids[m.group(1)]), path)


@pytest.fixture
def world(hub):
    a = hub.enroll("worker")
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "git push -f origin x", "cwd": "/w"},
                         headers=a.headers).json()
    q = hub.client.post("/api/v1/projects/proj/questions", json={"question": "which?"}, headers=a.headers).json()
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="t", assignee="worker")
    node_id = store.get_agent(conn, a.id).device_node_id
    return hub, a, {"approval_id": ap["id"], "qid": q["id"], "task_id": t["id"], "agent_id": a.id, "node_id": node_id,
                    "key": "ch-proj~general", "name": "proj", "version": 1, "seed": 12345, "kind": "mini", "pose": "idle"}


def test_route_inventory_is_not_empty(hub):
    routes = ui_routes(hub.client.app)
    assert ("POST", "/ui/approvals/{approval_id}/decide") in routes
    assert ("GET", "/ui/activity") in routes and ("GET", "/ui/feed") in routes
    assert len(routes) == 58, "a route was added or removed: the walk tests cover it, update this count"


def test_agent_token_cannot_reach_any_operator_endpoint(world):
    hub, agent, ids = world
    attempts = [
        agent.headers,  # plain agent request from the agent's device
        {**agent.headers, "tailscale-user-login": OP_LOGIN},  # plus a forged identity header
        {**agent.headers, "x-forwarded-for": OP_IP, "tailscale-user-login": OP_LOGIN,
         "x-csrf-token": hub.csrf()},  # even from the operator's own IP with a valid CSRF token
    ]
    for method, path in ui_routes(hub.client.app):
        for headers in attempts:
            r = hub.client.request(method, fill(path, ids), headers=headers,
                                   data={"csrf": hub.csrf(), "decision": "approve", "body": "x", "state": "done"},
                                   follow_redirects=False)
            assert r.status_code == 403, (method, path, headers.keys(), r.status_code)
    assert approvals.get(hub.conn(), ids["approval_id"])["status"] == "pending"


def test_tagged_agent_device_cannot_pose_as_operator(world):
    hub, agent, ids = world
    # No Authorization header, forged login header, from the (tagged) agent machine.
    r = hub.client.get("/ui/approvals", headers={"tailscale-user-login": OP_LOGIN, "x-forwarded-for": agent.ip})
    assert r.status_code == 403 and "tagged" in r.text


@pytest.mark.parametrize("case", ["no_header", "wrong_login", "not_operator", "unknown_ip", "direct",
                                  "claims_operator_login"])
def test_human_identity_checks(hub, case):
    headers = hub.op_headers()
    client = hub.client
    if case == "no_header":
        headers.pop("tailscale-user-login")
    elif case == "wrong_login":
        headers["tailscale-user-login"] = "someone-else@example.com"
    elif case == "not_operator":
        hub.whois.add_user("100.100.0.9", "nother", "visitor@example.com")
        headers.update({"x-forwarded-for": "100.100.0.9", "tailscale-user-login": "visitor@example.com"})
    elif case == "claims_operator_login":
        # A non-operator's device presenting the operator's login (only possible by forging the
        # header locally): the hub's own whois says otherwise.
        hub.whois.add_user("100.100.0.9", "nother", "visitor@example.com")
        headers["x-forwarded-for"] = "100.100.0.9"
    elif case == "unknown_ip":
        headers["x-forwarded-for"] = "100.100.9.9"
    elif case == "direct":
        client = TestClient(hub.client.app, client=("100.100.0.5", 5555))
    assert client.get("/ui/approvals", headers=headers).status_code == 403


def test_operator_device_running_an_agent_is_refused(hub):
    """An agent enrolled from the operator's own (untagged) laptop disqualifies that laptop."""
    hub.whois.add_user("100.100.0.7", "nlaptop", OP_LOGIN)
    code = hub.code()
    r = hub.client.post("/api/v1/enroll", json={"code": code, "name": "laptop-agent"},
                        headers={"x-forwarded-for": "100.100.0.7"})
    assert r.status_code == 201
    laptop = {**hub.op_headers(), "x-forwarded-for": "100.100.0.7"}
    resp = hub.client.get("/ui/approvals", headers=laptop)
    assert resp.status_code == 403 and "enrolled agent" in resp.text
    assert hub.client.get("/ui/approvals", headers=hub.op_headers()).status_code == 200  # the phone still works


def test_insecure_dev_disables_operator_access(dev_hub):
    assert dev_hub.client.get("/ui/approvals", headers=dev_hub.op_headers()).status_code == 403


def test_pages_render_with_security_headers(world):
    hub, agent, ids = world
    for path in ["/ui/dashboard", "/ui/reports", "/ui/approvals", f"/ui/approvals/{ids['approval_id']}", "/ui/tasks", f"/ui/tasks/{ids['task_id']}",
                 "/ui/activity", "/ui/activity?type=task", "/ui/agents", f"/ui/agents/{ids['agent_id']}", "/ui/search?q=push", "/ui/badge",
                 f"/ui/approvals/{ids['approval_id']}/thread", f"/ui/questions/{ids['qid']}", "/ui/chats", f"/ui/chats/{ids['agent_id']}",
                 "/ui/badge/chats", "/ui/devices", f"/ui/devices/{ids['node_id']}", "/ui/board"]:
        r = hub.client.get(path, headers=hub.op_headers())
        assert r.status_code == 200, path
        assert "default-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("problem", ["missing", "wrong", "cross_site", "cross_origin"])
def test_csrf_protection(world, problem):
    hub, agent, ids = world
    headers, data = hub.op_headers(), {"csrf": hub.csrf(), "decision": "approve"}
    if problem == "missing":
        data.pop("csrf")
    elif problem == "wrong":
        data["csrf"] = "0" * 64
    elif problem == "cross_site":
        headers["sec-fetch-site"] = "cross-site"
    elif problem == "cross_origin":
        headers["origin"] = "https://evil.example"
    r = hub.client.post(f"/ui/approvals/{ids['approval_id']}/decide", data=data, headers=headers,
                        follow_redirects=False)
    assert r.status_code == 403
    assert approvals.get(hub.conn(), ids["approval_id"])["status"] == "pending"


def test_approve_from_phone(world):
    hub, agent, ids = world
    listing = hub.client.get("/ui/approvals", headers=hub.op_headers()).text
    assert f"/ui/approvals/{ids['approval_id']}" in listing and "git push -f origin x" in listing
    r = hub.client.post(f"/ui/approvals/{ids['approval_id']}/decide", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "decision": "approve", "note": "go"}, follow_redirects=False)
    assert r.status_code == 303
    a = hub.client.get(f"/api/v1/approvals/{ids['approval_id']}", headers=agent.headers).json()
    assert a["status"] == "approved" and a["decided_by"] == f"human:{OP_LOGIN}" and a["decision_note"] == "go"
    consumed = hub.client.post("/api/v1/approvals/command/consume", headers=agent.headers,
                               json={"command": "git push -f origin x", "cwd": "/w"}).json()
    assert consumed["allowed"] is True


def test_htmx_csrf_header_works(world):
    hub, agent, ids = world
    r = hub.client.post(f"/ui/approvals/{ids['approval_id']}/discuss", data={"body": "Which branch is this?"},
                        headers={**hub.op_headers(), "x-csrf-token": hub.csrf(), "hx-request": "true"})
    assert r.status_code == 200 and "Which branch is this?" in r.text and 'id="thread"' in r.text
    a = hub.client.get(f"/api/v1/approvals/{ids['approval_id']}", headers=agent.headers).json()
    assert a["awaiting_reply"] and a["discussion"][0]["author"] == f"human:{OP_LOGIN}"


def test_operator_task_lifecycle(world):
    hub, agent, ids = world
    h, c = hub.op_headers(), hub.csrf()
    r = hub.client.post("/ui/tasks", headers=h, follow_redirects=False,
                        data={"csrf": c, "project": "proj", "title": "From phone", "body": "do x", "max_turns": "7"})
    task_id = int(r.headers["location"].rsplit("/", 1)[1])
    t = tasks.get_task(hub.conn(), task_id)
    assert t["state"] == "open" and t["created_by"] == f"human:{OP_LOGIN}" and t["budget"]["max_turns"] == 7
    hub.client.post(f"/ui/tasks/{task_id}/assign", headers=h, data={"csrf": c, "assignee": "worker"})
    assert [m["agent_id"] for m in tasks.get_task(hub.conn(), task_id)["agents"]] == [agent.id]
    hub.client.post(f"/ui/tasks/{task_id}/move", headers=h, data={"csrf": c, "state": "cancelled", "note": "nvm"})
    assert tasks.get_task(hub.conn(), task_id)["state"] == "cancelled"
    bad = hub.client.post(f"/ui/tasks/{task_id}/move", headers=h, data={"csrf": c, "state": "open"})
    assert bad.status_code == 409 and "illegal task transition" in bad.text


def test_agents_page_enroll_approve_revoke(hub):
    h, c = hub.op_headers(), hub.csrf()
    page = hub.client.post("/ui/agents/enroll", headers=h, data={"csrf": c, "projects": "proj"}).text
    code = re.search(r'<p class="code">([A-Z0-9-]+)</p>', page).group(1)
    ip = hub.new_ip()
    hub.whois.add(ip, "nnew", "new.example.ts.net")
    agent = hub.client.post("/api/v1/enroll", json={"code": code, "name": "fresh"},
                            headers={"x-forwarded-for": ip}).json()["agent"]
    assert agent["status"] == "pending"
    hub.client.post(f"/ui/agents/{agent['id']}/approve", headers=h, data={"csrf": c})
    assert store.get_agent(hub.conn(), agent["id"]).status == "active"
    assert approvals.pending_enrollment(hub.conn(), agent["id"]) is None
    wrong = hub.client.post(f"/ui/agents/{agent['id']}/revoke", headers=h, data={"csrf": c, "confirm": "nope"})
    assert wrong.status_code == 422 and store.get_agent(hub.conn(), agent["id"]).status == "active"
    hub.client.post(f"/ui/agents/{agent['id']}/revoke", headers=h, data={"csrf": c, "confirm": "fresh"})
    assert store.get_agent(hub.conn(), agent["id"]).status == "revoked"


def test_agent_content_is_escaped(world):
    hub, agent, ids = world
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
    hub.client.post("/api/v1/projects/proj/messages", json={"body": evil}, headers=agent.headers)
    hub.client.post(f"/api/v1/approvals/{ids['approval_id']}/discussion", json={"body": evil}, headers=agent.headers)
    for path in ["/ui/activity", f"/ui/approvals/{ids['approval_id']}", "/ui/search?q=script"]:
        html = hub.client.get(path, headers=hub.op_headers()).text
        assert "<script>alert" not in html and "<img src=x" not in html
        assert "&lt;script&gt;" in html or path.startswith("/ui/search")


def test_operator_websocket_streams_feed(tmp_path):
    from .conftest import make_hub

    hub = make_hub(tmp_path)
    agent = hub.enroll("worker")
    hub.state.bus.poll_seconds = 0.02
    with TestClient(hub.client.app, client=("127.0.0.1", 40000)) as live:
        with live.websocket_connect("/ui/ws", headers=hub.op_headers(origin="http://testserver")) as ws:
            live.post("/api/v1/projects/proj/messages", json={"body": "hello operator"}, headers=agent.headers)
            msgs = [ws.receive_json() for _ in range(2)]
            row = next(m for m in msgs if m["event_type"] == "message.posted")
            assert "hello operator" in row["html"] and 'class="event"' in row["html"]


@pytest.mark.parametrize("variant", ["cross_origin", "agent_token", "no_identity"])
def test_operator_websocket_rejects(hub, variant):
    agent = hub.enroll("worker")
    headers = hub.op_headers(origin="http://testserver")
    if variant == "cross_origin":
        headers["origin"] = "https://evil.example"
    elif variant == "agent_token":
        headers = {**agent.headers, "tailscale-user-login": OP_LOGIN}
    else:
        headers.pop("tailscale-user-login")
    with pytest.raises(WebSocketDisconnect):
        with hub.client.websocket_connect("/ui/ws", headers=headers):
            pytest.fail("the hub accepted a socket it must refuse")


def test_static_assets_served(hub):
    r = hub.client.get("/ui/static/htmx-2.0.11.min.js")
    assert r.status_code == 200 and "htmx" in r.text[:200]
    assert hub.client.get("/ui/static/app.js").status_code == 200


def test_route_inventory_has_member_routes(hub):
    routes = ui_routes(hub.client.app)
    assert ("POST", "/ui/tasks/{task_id}/agents") in routes
    assert ("POST", "/ui/tasks/{task_id}/agents/{agent_id}/remove") in routes
    assert ("POST", "/ui/tasks/{task_id}/agents/{agent_id}/update") in routes


def test_dashboard_member_routes_require_csrf_and_operator(world):
    hub, agent, ids = world
    rev = hub.enroll("rev")
    base = f"/ui/tasks/{ids['task_id']}/agents"
    form = {"agent": "rev", "role": "Reviewer", "stage": "reviews"}
    calls = [(base, form), (f"{base}/{agent.id}/update", {"role": "x", "stage": "works"}),
             (f"{base}/{agent.id}/remove", {})]
    for path, data in calls:
        no_csrf = hub.client.post(path, headers=hub.op_headers(), data=data, follow_redirects=False)
        assert no_csrf.status_code == 403, path
        bad = hub.client.post(path, headers=hub.op_headers(), data={**data, "csrf": "nope"}, follow_redirects=False)
        assert bad.status_code == 403, path
        no_op = hub.client.post(path, headers=agent.headers, data={**data, "csrf": hub.csrf()}, follow_redirects=False)
        assert no_op.status_code == 403, path
    got = tasks.get_task(hub.conn(), ids["task_id"])
    assert [m["agent_id"] for m in got["agents"]] == [agent.id] and got["agents"][0]["role"] == ""
    assert rev.id not in [m["agent_id"] for m in got["agents"]]


def test_dashboard_adds_updates_removes_members(world):
    hub, agent, ids = world
    rev = hub.enroll("rev")
    h, c = hub.op_headers(), hub.csrf()
    base = f"/ui/tasks/{ids['task_id']}/agents"
    r = hub.client.post(base, headers=h, follow_redirects=False,
                        data={"csrf": c, "agent": "rev", "role": "Reviewer: check the rate limiter",
                              "stage": "reviews"})
    assert r.status_code == 303
    members = {m["agent_id"]: m for m in tasks.get_task(hub.conn(), ids["task_id"])["agents"]}
    assert members[rev.id]["role"] == "Reviewer: check the rate limiter" and members[rev.id]["stage"] == "reviews"
    bad = hub.client.post(base, headers=h, data={"csrf": c, "agent": "rev", "role": "x", "stage": "bogus"})
    assert bad.status_code in (409, 422)
    long = hub.client.post(base, headers=h, data={"csrf": c, "agent": "rev", "role": "x" * 201, "stage": "works"})
    assert long.status_code == 422
    hub.client.post(f"{base}/{rev.id}/update", headers=h, data={"csrf": c, "role": "Tester", "stage": "works"})
    m = [m for m in tasks.get_task(hub.conn(), ids["task_id"])["agents"] if m["agent_id"] == rev.id][0]
    assert (m["role"], m["stage"]) == ("Tester", "works")
    hub.client.post(f"{base}/{rev.id}/remove", headers=h, data={"csrf": c})
    assert [m["agent_id"] for m in tasks.get_task(hub.conn(), ids["task_id"])["agents"]] == [agent.id]
    page = hub.client.get(f"/ui/tasks/{ids['task_id']}", headers=h)
    assert page.status_code == 200


def test_dashboard_adds_reviewer_with_role(world):
    hub, agent, ids = world
    rev = hub.enroll("rev")
    hub.client.post(f"/ui/tasks/{ids['task_id']}/agents", headers=hub.op_headers(),
                    data={"csrf": hub.csrf(), "agent": "rev", "role": "Reviewer: check the rate limiter",
                          "stage": "reviews"})
    agents = tasks.get_task(hub.conn(), ids["task_id"])["agents"]
    assert any(m["agent_id"] == rev.id and m["role"] == "Reviewer: check the rate limiter" for m in agents)


def test_verdict_notes_are_escaped(world):
    hub, agent, ids = world
    rev = hub.enroll("rev")
    conn = hub.conn()
    with db.transaction(conn):
        task_agents.add_agent(conn, ids["task_id"], rev.id, role="R", stage="reviews", actor=OPERATOR)
        conn.execute("UPDATE tasks SET state = 'review' WHERE id = ?", (ids["task_id"],))
        task_agents.record_verdict(conn, ids["task_id"], store.get_agent(conn, rev.id), verdict="approve",
                                   notes="<script>alert(1)</script>")
    page = hub.client.get(f"/ui/tasks/{ids['task_id']}", headers=hub.op_headers()).text
    assert "&lt;script&gt;" in page and "<script>alert(1)" not in page
    assert "agent content" in page


def test_role_shown_on_terminal_task(world):
    hub, agent, ids = world
    rev = hub.enroll("rev")
    h, c = hub.op_headers(), hub.csrf()
    hub.client.post(f"/ui/tasks/{ids['task_id']}/agents", headers=h,
                    data={"csrf": c, "agent": "rev", "role": "Reviewer: check the rate limiter", "stage": "reviews"})
    hub.client.post(f"/ui/tasks/{ids['task_id']}/move", headers=h, data={"csrf": c, "state": "cancelled"})
    assert tasks.get_task(hub.conn(), ids["task_id"])["state"] == "cancelled"
    page = hub.client.get(f"/ui/tasks/{ids['task_id']}", headers=h).text
    assert "Reviewer: check the rate limiter" in page and rev.name in page


# --- avatars ---------------------------------------------------------------------


def test_avatar_image_headers(world):
    hub, agent, ids = world
    r = hub.client.get("/ui/avatars/1/12345/scene/working.svg", headers=hub.op_headers())
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert r.text.startswith("<svg") and "<script" not in r.text


@pytest.mark.parametrize("path", ["/ui/avatars/2/1/scene/idle.svg", "/ui/avatars/1/1/scene/dancing.svg",
                                  "/ui/avatars/1/1/huge/idle.svg", "/ui/avatars/1/-1/mini/idle.svg",
                                  "/ui/avatars/1/abc/mini/idle.svg", "/ui/avatars/x/1/mini/idle.svg",
                                  "/ui/avatars/1/99999999999/mini/idle.svg"])
def test_bad_avatar_urls_are_404(world, path):
    hub, _, _ = world
    assert hub.client.get(path, headers=hub.op_headers()).status_code == 404


def test_other_sites_cannot_embed_avatars(world):
    hub, _, _ = world
    r = hub.client.get("/ui/avatars/1/1/mini/idle.svg", headers=hub.op_headers(**{"sec-fetch-site": "cross-site"}))
    assert r.status_code == 403


def test_avatar_needs_an_operator(world):
    hub, agent, _ = world
    assert hub.client.get("/ui/avatars/1/1/mini/idle.svg").status_code == 403
    assert hub.client.get("/ui/avatars/1/1/mini/idle.svg", headers=agent.headers).status_code == 403


def test_reroll_needs_csrf_and_changes_the_look(world):
    hub, agent, ids = world
    seed = lambda: hub.conn().execute("SELECT avatar_seed FROM agents WHERE id = ?", (agent.id,)).fetchone()[0]  # noqa: E731
    before = seed()
    r = hub.client.post(f"/ui/agents/{agent.id}/avatar", headers=hub.op_headers(), data={}, follow_redirects=False)
    assert r.status_code == 403 and seed() == before
    r = hub.client.post(f"/ui/agents/{agent.id}/avatar", headers=hub.op_headers(), data={"csrf": hub.csrf()},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/ui/agents/{agent.id}" and seed() != before


def test_pages_show_each_agents_avatar_in_its_pose(world):
    hub, agent, ids = world
    other = hub.enroll("helper")
    conn = hub.conn()
    conn.execute("UPDATE agents SET connected = 1 WHERE id = ?", (other.id,))
    looks = {r["id"]: (r["avatar_version"], r["avatar_seed"])
             for r in conn.execute("SELECT id, avatar_version, avatar_seed FROM agents")}
    html = hub.client.get("/ui/agents", headers=hub.op_headers()).text
    v, s = looks[other.id]
    assert (f'src="/ui/avatars/{v}/{s}/scene/idle.svg" width="96" height="96" alt="helper" loading="lazy">'
            '<span class="avatar-status status-idle">idle</span>') in html
    v, s = looks[agent.id]  # not connected
    assert f'src="/ui/avatars/{v}/{s}/scene/offline.svg"' in html and '<span class="avatar-status status-offline">offline</span>' in html
    for page in (f"/ui/approvals/{ids['approval_id']}", f"/ui/tasks/{ids['task_id']}", "/ui/approvals"):
        text = hub.client.get(page, headers=hub.op_headers()).text
        assert f'/ui/avatars/{v}/{s}/mini/offline.svg' in text and 'status-offline">offline</span>' in text, page
    hub.set_status(agent, "revoked")
    assert f"/ui/avatars/{v}/{s}/" not in hub.client.get("/ui/agents", headers=hub.op_headers()).text
