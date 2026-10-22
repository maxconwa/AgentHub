"""Page shell, Dashboard and Reports placeholder."""

import json
import re

import pytest

from agenthub.hub import db, store, task_agents, tasks, views

from .conftest import OPERATOR

LABELS = ["Dashboard", "Projects", "Tasks", "Agents", "Devices", "Approvals", "Chats", "Board", "Reports"]


def get(hub, path):
    return hub.client.get(path, headers=hub.op_headers())


@pytest.fixture
def busy(hub):
    a = hub.enroll("worker")
    b = hub.enroll("gone")
    hub.set_status(b, "revoked")
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "git push -f origin x", "cwd": "/w"},
                         headers=a.headers).json()
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Fix login", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
    return hub, a, b, ap, t


def test_ui_redirects_to_dashboard(hub):
    for p in ("/ui", "/ui/"):
        r = hub.client.get(p, headers=hub.op_headers(), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/ui/dashboard"


@pytest.mark.parametrize("path,label", [("/ui/dashboard", "Dashboard"), ("/ui/reports", "Reports"),
                                        ("/ui/approvals", "Approvals"), ("/ui/tasks", "Tasks"),
                                        ("/ui/agents", "Agents")])
def test_top_bar_order_and_active(hub, path, label):
    html = get(hub, path).text
    nav = re.search(r"<nav.*?</nav>", html, re.S).group(0)
    found = re.findall(r"<a href=\"(/ui/\w+)\" class=\"(active)?\"[^>]*>(\w+)", nav)
    assert [f[2] for f in found] == LABELS
    assert [f[2] for f in found if f[1] == "active"] == [label]
    assert 'action="/ui/search"' in html


def test_feed_and_search_have_no_active_link(hub):
    for p in ("/ui/feed", "/ui/search"):
        assert 'class="active"' not in get(hub, p).text


def test_dashboard_lists_approval_and_agents_not_revoked(busy):
    hub, a, b, ap, t = busy
    r = get(hub, "/ui/dashboard")
    assert r.status_code == 200
    assert f"/ui/approvals/{ap['id']}" in r.text and "git push -f origin x" in r.text
    assert "worker" in r.text and "gone" not in r.text
    assert "1 offline" in r.text and "Fix login" in r.text and "tester" in r.text


def test_empty_hub_renders(hub):
    for p in ("/ui/dashboard", "/ui/reports"):
        r = get(hub, p)
        assert r.status_code == 200
    assert "Nothing needs you" in get(hub, "/ui/dashboard").text
    assert "Reports and pages your agents make will appear here." in get(hub, "/ui/reports").text


def test_no_inline_style_or_script(busy):
    hub = busy[0]
    for p in ("/ui/dashboard", "/ui/reports"):
        html = get(hub, p).text
        assert "style=" not in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)


def test_dashboard_has_live_region(busy):
    html = get(busy[0], "/ui/dashboard").text
    assert 'id="live"' in html and 'hx-get="/ui/dashboard"' in html and 'hx-trigger="every 10s"' in html


def test_agent_rows_show_role(busy):
    hub, a, b, ap, t = busy
    rows = views.agent_rows(hub.conn())
    assert [r["name"] for r in rows] == ["worker"]
    row = rows[0]
    assert row["tasks"] == [{"id": t["id"], "title": "Fix login", "project": "proj", "state": t["state"],
                             "role": "tester", "stage": "works", "created_by": OPERATOR}]
    assert row["avatar"]["pose"] and row["device"] and "connected" in row


# --- Projects ----------------------------------------------------------------------------


@pytest.fixture
def tree(hub):
    a = hub.enroll("worker")
    hub.enroll("idle-bot")
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "empty", actor=OPERATOR, description="nothing yet")
        e = tasks.create_task(conn, actor=OPERATOR, project="empty", title="Finished", assignee=None)
        conn.execute("UPDATE tasks SET state = 'cancelled' WHERE id = ?", (e["id"],))
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Fix login", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
        d = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Old chore", assignee=None)
        conn.execute("UPDATE tasks SET state = 'done' WHERE id = ?", (d["id"],))
        task_agents.add_agent(conn, d["id"], a.id, role="gone-role", stage="works", actor=OPERATOR)
        by_agent = tasks.create_task(conn, actor=a.id, project="proj", title="Agent made", assignee=None)
    return hub, a, t, d, by_agent


def test_project_tree_nests_and_hides_terminal(tree):
    hub, a, t, d, _ = tree
    out = views.project_tree(hub.conn())
    proj = next(p for p in out if p["name"] == "proj")
    titles = [x["title"] for x in proj["tasks"]]
    assert "Fix login" in titles and "Old chore" not in titles
    ag = next(x for x in proj["tasks"] if x["id"] == t["id"])["agents"][0]
    assert ag["name"] == "worker" and ag["role"] == "tester" and ag["stage"] == "works" and ag["device"]
    assert ag["avatar"] and ag["agent_id"] == a.id
    assert [p["name"] for p in out][-1] == "empty" or "empty" not in [p["name"] for p in out]


def test_projects_page(tree):
    hub, a, t, d, _ = tree
    r = get(hub, "/ui/projects")
    assert r.status_code == 200
    assert "Fix login" in r.text and "tester" in r.text and "Old chore" not in r.text and "gone-role" not in r.text
    assert f"/ui/tasks/{t['id']}" in r.text and "/ui/projects/proj" in r.text
    assert "No live tasks" in r.text
    assert r.text.index("/ui/projects/proj") < r.text.index("/ui/projects/empty")
    assert re.search(r'<details class="panel project" open>(?:(?!</details>).)*proj', r.text, re.S)
    assert "style=" not in r.text


def test_agent_created_task_shows_by_label(hub):
    a = hub.enroll("worker")
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=a.id, project="proj", title="Agent made", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="", stage="works", actor=OPERATOR)
        h = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Human made", assignee=None)
        task_agents.add_agent(conn, h["id"], a.id, role="", stage="works", actor=OPERATOR)
    for path in ("/ui/dashboard", "/ui/projects"):
        html = get(hub, path).text
        assert "Agent made</span> <span class=\"by-label\">by worker" in html, path
    assert "Human made</span> <span" not in get(hub, "/ui/projects").text


def test_project_page_and_404(tree):
    hub = tree[0]
    r = get(hub, "/ui/projects/proj")
    assert r.status_code == 200 and "Fix login" in r.text and "Old chore" not in r.text
    assert 'id="live"' in r.text and 'hx-get="/ui/projects/proj"' in r.text
    assert get(hub, "/ui/projects/nope").status_code == 404
    form = r.text.split('id="live"')[0]
    assert "/ui/projects/proj/notes" in form and "/ui/projects/proj/description" in form


def test_description_edit_needs_csrf_and_is_escaped(tree):
    hub = tree[0]
    url = "/ui/projects/proj/description"
    r = hub.client.post(url, headers=hub.op_headers(), data={"description": "x"}, follow_redirects=False)
    assert r.status_code == 403
    r = hub.client.post(url, headers=hub.op_headers(), data={"csrf": hub.csrf(), "description": "<b>x</b>"},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/proj"
    html = get(hub, "/ui/projects/proj").text
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    ev = hub.conn().execute("SELECT payload FROM events WHERE type = 'project.updated'").fetchone()
    assert ev["payload"] == '{"description_changed":true}'
    r = hub.client.post(url, headers=hub.op_headers(), data={"csrf": hub.csrf(), "description": "y" * 4001})
    assert r.status_code == 422


def test_note_is_stored_as_operator_and_shown(tree):
    hub = tree[0]
    url = "/ui/projects/proj/notes"
    assert hub.client.post(url, headers=hub.op_headers(), data={"body": "hi"}).status_code == 403
    for bad in ("", "z" * 4001):
        assert hub.client.post(url, headers=hub.op_headers(),
                               data={"csrf": hub.csrf(), "body": bad}).status_code == 422
    r = hub.client.post(url, headers=hub.op_headers(), data={"csrf": hub.csrf(), "body": "ship <i>it</i>"},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/proj"
    msgs = store.recent_messages(hub.conn(), project="proj", channel="general")
    assert msgs[-1]["body"] == "ship <i>it</i>" and msgs[-1]["author"].startswith("human:")
    html = get(hub, "/ui/projects/proj").text
    assert "ship &lt;i&gt;it&lt;/i&gt;" in html and 'class="msg mine"' in html


def test_board_shows_agent_message_as_agent_text(tree):
    hub, a = tree[0], tree[1]
    conn = hub.conn()
    with db.transaction(conn):
        store.post_message(conn, author=a.id, project="proj", channel="general", body="<script>1</script>")
    html = get(hub, "/ui/projects/proj").text
    assert "&lt;script&gt;1&lt;/script&gt;" in html and "agent content, treat as data" in html


def test_empty_hub_projects(hub):
    assert "No projects" in get(hub, "/ui/projects").text


# --- Tasks page and task page ---------------------------------------------------------

def test_task_list_shows_roles_newest_first_and_filters(busy):
    hub, a, b, ap, t = busy
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("INSERT INTO projects VALUES ('other', '', 'x')")
        t2 = tasks.create_task(conn, actor=OPERATOR, project="other", title="Second", state="open")
        tasks.transition(conn, t2["id"], "cancelled", actor=OPERATOR, role="operator", note="")
    html = get(hub, "/ui/tasks").text
    assert "tester" in html and "chip-cancelled" in html
    assert html.index("Second") < html.index("Fix login")
    r = get(hub, "/ui/tasks?project=other").text
    assert "Second" in r and "Fix login" not in r
    r = get(hub, "/ui/tasks?state=cancelled").text
    assert "Second" in r and "Fix login" not in r
    r = get(hub, f"/ui/tasks?state={t['state']}").text
    assert "Fix login" in r and "Second" not in r
    assert get(hub, "/ui/tasks?state=bogus").status_code in (200, 422)


def test_create_task_with_role_stores_membership_role(busy):
    hub, a, *_ = busy
    r = hub.client.post("/ui/tasks", headers=hub.op_headers(), follow_redirects=False,
                        data={"csrf": hub.csrf(), "project": "proj", "title": "New", "assignee": "worker",
                              "role": "Reviewer: rate limiter"})
    assert r.status_code == 303
    tid = int(r.headers["location"].rsplit("/", 1)[1])
    assert [m["role"] for m in tasks.get_task(hub.conn(), tid)["agents"]] == ["Reviewer: rate limiter"]
    assert 'name="role"' in get(hub, "/ui/tasks").text


def test_task_description_edit_is_escaped_and_emits_event(busy):
    hub, a, b, ap, t = busy
    url = f"/ui/tasks/{t['id']}/description"
    assert hub.client.post(url, headers=hub.op_headers(), data={"title": "x"}).status_code == 403
    for bad in ({"title": ""}, {"title": "x" * 201}, {"title": "x", "body": "y" * 16001}):
        assert hub.client.post(url, headers=hub.op_headers(), data={"csrf": hub.csrf(), **bad}).status_code == 422
    r = hub.client.post(url, headers=hub.op_headers(), follow_redirects=False,
                        data={"csrf": hub.csrf(), "title": "<b>T</b>", "body": "<i>B</i>"})
    assert r.status_code == 303 and r.headers["location"] == f"/ui/tasks/{t['id']}"
    html = get(hub, f"/ui/tasks/{t['id']}").text
    assert "&lt;b&gt;T&lt;/b&gt;" in html and "&lt;i&gt;B&lt;/i&gt;" in html and "<b>T</b>" not in html
    ev = hub.conn().execute("SELECT payload FROM events WHERE type = 'task.updated'").fetchone()
    assert ev["payload"] == f'{{"task_id":{t["id"]},"fields":["title","body"]}}'
    assert get(hub, "/ui/tasks/9999").status_code == 404
    assert hub.client.post("/ui/tasks/9999/description", headers=hub.op_headers(),
                           data={"csrf": hub.csrf(), "title": "x"}).status_code == 404


def test_task_note_lands_in_task_channel_as_operator(busy):
    hub, a, b, ap, t = busy
    url = f"/ui/tasks/{t['id']}/notes"
    assert hub.client.post(url, headers=hub.op_headers(), data={"body": "hi"}).status_code == 403
    assert hub.client.post(url, headers=hub.op_headers(), data={"csrf": hub.csrf(), "body": ""}).status_code == 422
    r = hub.client.post(url, headers=hub.op_headers(), follow_redirects=False,
                        data={"csrf": hub.csrf(), "body": "try <u>this</u>"})
    assert r.status_code == 303 and r.headers["location"] == f"/ui/tasks/{t['id']}"
    msgs = store.recent_messages(hub.conn(), project="proj", channel=f"task-{t['id']}")
    assert msgs[-1]["body"] == "try <u>this</u>" and msgs[-1]["author"].startswith("human:")
    page = get(hub, f"/ui/tasks/{t['id']}").text
    assert "try &lt;u&gt;this&lt;/u&gt;" in page.split('id="live"')[1]
    form = page.split('id="live"')[0]
    assert f"/ui/tasks/{t['id']}/notes" in form and f"/ui/tasks/{t['id']}/description" in form
    assert hub.client.post("/ui/tasks/9999/notes", headers=hub.op_headers(),
                           data={"csrf": hub.csrf(), "body": "x"}).status_code == 404


def test_task_page_shows_each_agents_own_budget_and_use(busy):
    hub, a, b, ap, t = busy
    c = hub.enroll("second")
    conn = hub.conn()
    with db.transaction(conn):
        task_agents.add_agent(conn, t["id"], c.id, role="", stage="works", actor=OPERATOR,
                              budget={"max_turns": 7, "wall_clock_minutes": 9, "max_subtasks": 2})
        conn.execute("INSERT INTO task_runs (task_id, agent_id, started_at, ended_at, num_turns, outcome) "
                     "VALUES (?, ?, '2026-01-01T00:00:00.000Z', '2026-01-01T00:03:00.000Z', 4, 'done')",
                     (t["id"], c.id))
    html = get(hub, f"/ui/tasks/{t['id']}").text
    assert "4 / 7" in html and "3 / 9" in html and "1 / 2" in html
    assert "0 / 50" in html  # worker: default budget, no runs


def test_task_page_keeps_activity_outside_live(busy):
    hub, a, b, ap, t = busy
    html = get(hub, f"/ui/tasks/{t['id']}").text
    before, live = html.split('id="live"')
    assert "<h2>Activity</h2>" in before and "Activity" not in live
    assert f"task #{t['id']}" in before.split("<h2>Activity</h2>")[1]


# --- Agents page and agent page ---------------------------------------------------

EVIL = "<script>alert(1)</script>"


def report(hub, agent, checkouts, current_dir):
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET checkouts = ?, current_dir = ? WHERE id = ?",
                     (json.dumps(checkouts), current_dir, agent.id))


def test_agents_grid_cards_link_to_pages_and_show_directory(busy):
    hub, a, b, ap, t = busy
    report(hub, a, {"proj": "/srv/proj", "other": "/srv/other"}, None)
    html = get(hub, "/ui/agents").text
    assert f'href="/ui/agents/{a.id}"' in html and "worker" in html
    assert "/srv/proj, /srv/other" in html and "Fix login" in html and "tester" in html
    assert "gone" not in html and "Enroll a new agent" in html
    assert 'width="96"' in html
    report(hub, a, {"proj": "/srv/proj"}, "/srv/proj/.worktrees/x")
    html = get(hub, "/ui/agents").text
    assert "/srv/proj/.worktrees/x" in html and "/srv/proj, " not in html
    report(hub, a, {}, None)
    assert "No checkouts reported" in get(hub, "/ui/agents").text


def test_agents_grid_shows_pending_agents(hub):
    p = hub.enroll("newbie", approve=False)
    html = get(hub, "/ui/agents").text
    assert "newbie" in html and "chip-pending" in html and f'href="/ui/agents/{p.id}"' in html


def test_agent_page_shows_device_directory_checkouts_escaped(busy):
    hub, a, b, ap, t = busy
    report(hub, a, {"proj": f"/srv/{EVIL}"}, f"/w/{EVIL}")
    r = get(hub, f"/ui/agents/{a.id}")
    assert r.status_code == 200
    html = r.text
    assert "worker-host.example.ts.net" in html and "<script>alert" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "reported by the agent" in html
    assert "Fix login" in html and "tester" in html and f"/ui/tasks/{t['id']}" in html
    assert f"/ui/approvals/{ap['id']}" in html and "git push -f origin x" in html
    assert f'action="/ui/agents/{a.id}/projects"' in html and f'action="/ui/agents/{a.id}/avatar"' in html
    assert f'action="/ui/agents/{a.id}/revoke"' in html
    assert f'href="/ui/chats/{a.id}"' in html and 'href="/ui/board"' in html


def test_agent_page_unknown_is_404(hub):
    assert get(hub, "/ui/agents/nope").status_code == 404


def test_agent_page_forms_redirect_back_to_it(busy):
    hub, a, b, ap, t = busy
    h, c = hub.op_headers(), hub.csrf()
    page = f"/ui/agents/{a.id}"
    r = hub.client.post(f"{page}/projects", headers=h, data={"csrf": c, "projects": "proj, more"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == page
    assert set(store.get_agent(hub.conn(), a.id).allowed_projects) == {"proj", "more"}
    r = hub.client.post(f"{page}/revoke", headers=h, data={"csrf": c, "confirm": "worker"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == page
    assert store.get_agent(hub.conn(), a.id).status == "revoked"
    p = hub.enroll("newbie", approve=False)
    r = hub.client.post(f"/ui/agents/{p.id}/approve", headers=h, data={"csrf": c}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/ui/agents/{p.id}"
    assert f'action="/ui/agents/{p.id}/approve"' not in get(hub, f"/ui/agents/{p.id}").text


def test_pending_agent_page_has_approve_form(hub):
    p = hub.enroll("newbie", approve=False)
    assert f'action="/ui/agents/{p.id}/approve"' in get(hub, f"/ui/agents/{p.id}").text


def test_revoked_agent_page_renders_read_only(busy):
    hub, a, b, ap, t = busy
    html = get(hub, f"/ui/agents/{b.id}").text
    assert "chip-revoked" in html and "gone" in html
    assert "/ui/avatars/" not in html and "<form" not in html.split("<h1>", 1)[1]  # read-only
    assert "gone" not in get(hub, "/ui/agents").text


# --- Devices page -----------------------------------------------------------------

def place(hub, agent, node, device, connected, heartbeat):
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET device_node_id = ?, device_name = ?, connected = ?, last_heartbeat = ? "
                     "WHERE id = ?", (node, device, int(connected), heartbeat, agent.id))


def test_two_agents_on_one_device_form_one_card_online_if_any_connected(hub):
    a, b = hub.enroll("alpha"), hub.enroll("beta")
    place(hub, a, "n1", "laptop", False, "2026-01-01T00:00:00.000Z")
    place(hub, b, "n1", "laptop", True, "2026-01-01T00:05:00.000Z")
    devs = views.devices(hub.conn())
    assert len(devs) == 1 and devs[0]["online"] and devs[0]["last_seen"] == "2026-01-01T00:05:00.000Z"
    assert [x["name"] for x in devs[0]["agents"]] == ["alpha", "beta"]
    html = get(hub, "/ui/devices").text
    assert html.count('class="card device-card"') == 1 and "laptop" in html
    assert "alpha" in html and "beta" in html and "last seen" not in html


def test_offline_device_shows_last_seen_and_sorts_after_online(hub):
    a, b = hub.enroll("alpha"), hub.enroll("beta")
    place(hub, a, "n1", "aaa-box", False, "2026-01-01T00:00:00.000Z")
    place(hub, b, "n2", "zzz-box", True, "2026-01-01T00:00:00.000Z")
    assert [d["name"] for d in views.devices(hub.conn())] == ["zzz-box", "aaa-box"]
    html = get(hub, "/ui/devices").text
    assert "last seen" in html and "ago" in html and "arrive in the devices-and-usage plan" in html


def test_revoked_only_device_hidden_and_empty_state(hub):
    assert "No devices yet" in get(hub, "/ui/devices").text
    a = hub.enroll("alpha")
    hub.set_status(a, "revoked")
    assert views.devices(hub.conn()) == [] and "No devices yet" in get(hub, "/ui/devices").text
