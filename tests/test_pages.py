"""Page shell, Dashboard and Reports placeholder."""

import json
import re

import pytest

from agenthub.hub import approvals, chats, db, private, questions, store, task_agents, tasks, views

from .conftest import OP_LOGIN, OPERATOR
from .test_chat_ui import add_row

LABELS = ["Dashboard", "Projects", "Agents", "Tasks", "Devices", "Approvals", "Chats", "Reports", "Usage"]


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


def test_search_has_no_active_link(hub):
    assert 'class="active"' not in get(hub, "/ui/search").text


def test_activity_page_has_filters_and_is_linked_from_chats(hub):
    html = get(hub, "/ui/activity").text
    nav = re.search(r"<nav.*?</nav>", html, re.S).group(0)
    assert 'class="active"' not in nav  # 0.3.7's Board tab is gone; the Chats page links here
    assert 'href="/ui/activity"' in get(hub, "/ui/chats").text
    assert 'action="/ui/activity"' in html and 'name="project"' in html and 'id="feed"' in html
    assert get(hub, "/ui/activity?type=task").status_code == 200


def test_old_feed_url_redirects_to_activity(hub):
    r = hub.client.get("/ui/feed?project=proj&type=task", headers=hub.op_headers(), follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/ui/activity?project=proj&type=task"
    r = hub.client.get("/ui/feed", headers=hub.op_headers(), follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/ui/activity"
    assert hub.client.get("/ui/feed", follow_redirects=False).status_code in (401, 403)


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
    assert "No reports yet." in get(hub, "/ui/reports").text


def test_no_inline_style_or_script(busy):
    hub = busy[0]
    for p in ("/ui/dashboard", "/ui/reports"):
        html = get(hub, p).text
        assert "style=" not in html
        assert not re.search(r"<script(?![^>]*\bsrc=)", html)


def test_dashboard_has_live_region(busy):
    html = get(busy[0], "/ui/dashboard").text
    assert 'id="live"' in html and 'hx-get="/ui/dashboard"' in html and 'hx-trigger="every 10s"' in html


def test_dashboard_sections_in_order(busy):
    live = get(busy[0], "/ui/dashboard").text.split('id="live"')[1]
    heads = [live.index(h) for h in ("<h2>Waiting on you", "<h2>Agents")]
    assert heads == sorted(heads)
    assert heads[1] < live.index('class="tally"') < live.index('class="agent-grid"')
    assert "<h2>Prompts" not in live


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
    assert re.search(r'<details class="panel project" open data-remember="project:proj">', r.text)
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
    assert 'id="live"' not in r.text and 'data-chat="project:proj"' in r.text  # the chat is live by itself
    assert get(hub, "/ui/projects/nope").status_code == 404
    assert "/ui/projects/proj/notes" not in r.text and "/ui/projects/proj/description" in r.text


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
    # the operator's description has no length limit (0.3.9.0)
    r = hub.client.post(url, headers=hub.op_headers(), data={"csrf": hub.csrf(), "description": "y" * 40_001},
                        follow_redirects=False)
    assert r.status_code == 303
    assert len(next(p for p in store.list_projects(hub.conn()) if p["name"] == "proj")["description"]) == 40_001


def test_empty_hub_projects(hub):
    html = get(hub, "/ui/projects").text
    assert "No projects yet. Make one with the + button › New project." in html


def test_project_without_tasks_is_listed_collapsed_last(tree):
    hub = tree[0]
    r = hub.client.post("/ui/projects", headers=hub.op_headers(), data={"csrf": hub.csrf(), "name": "aaa-new"},
                        follow_redirects=False)
    assert r.status_code == 303
    out = views.project_tree(hub.conn())
    assert [p["name"] for p in out][0] == "proj" and {"aaa-new", "empty"} <= {p["name"] for p in out[1:]}
    assert next(p for p in out if p["name"] == "aaa-new")["tasks"] == []
    html = get(hub, "/ui/projects").text
    assert '<details class="panel project" data-remember="project:aaa-new">' in html  # collapsed
    assert html.index("project:proj") < html.index("project:aaa-new")


# --- Tasks page and task page ---------------------------------------------------------

def test_task_list_shows_roles_newest_first_and_filters(busy):
    hub, a, b, ap, t = busy
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("INSERT INTO projects (name, description, created_at) VALUES ('other', '', 'x')")
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
    for bad in ({"title": ""}, {"title": "x" * 201}):  # the operator's description has no length limit
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


def test_task_page_shows_each_agents_use_without_limits(busy):
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
    # Usage only: since 0.3.5 runners enforce no budget, so the page shows no "used / limit".
    assert "4 / 7" not in html and "/ 50" not in html and "<h3>Usage</h3>" in html


def test_task_page_has_its_chat_then_activity(busy):
    hub, a, b, ap, t = busy
    html = get(hub, f"/ui/tasks/{t['id']}").text
    assert 'id="live"' not in html
    assert html.index(f'data-chat="task:{t["id"]}"') < html.index("<h2>Activity</h2>")
    assert f"task #{t['id']}" in html.split("<h2>Activity</h2>")[1]


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
    assert f'data-chat="agent:{a.id}"' in html and "/ui/board" not in html and "/ui/chats/" not in html


def agent_facts(html):
    """Each agent-facts list in ``html`` as its (label, value text, value attributes) rows."""
    return [[(dt, " ".join(re.sub(r"<[^>]+>", "", dd).split()), attrs)
             for dt, attrs, dd in re.findall(r"<dt>(.*?)</dt>\s*<dd([^>]*)>(.*?)</dd>", dl, re.S)]
            for dl in re.findall(r'<dl class="agent-facts">(.*?)</dl>', html, re.S)]


def agent_card(html, agent):
    return re.search(rf'<li class="card agent-card[^"]*"[^>]*data-panel="agent:{agent.id}">.*?</li>', html, re.S)[0]


@pytest.fixture
def two_tasks(busy):
    hub, a, b, ap, t = busy
    conn = hub.conn()
    with db.transaction(conn):
        t2 = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Review login", assignee=None)
        task_agents.add_agent(conn, t2["id"], a.id, role="", stage="reviews", actor=OPERATOR)
    report(hub, a, {"proj": "/srv/proj"}, "/srv/proj/.worktrees/x")
    return hub, a, t, t2


@pytest.mark.parametrize("path", ["/ui/agents", "/ui/dashboard"])
def test_agent_card_puts_each_fact_on_its_own_line(two_tasks, path):
    hub, a, t, t2 = two_tasks
    card = agent_card(get(hub, path).text, a)
    lists = agent_facts(card)
    # Task, Project and Role (Stage without a role) per task, oldest task first, then Device and Folder once
    assert [[(dt, text) for dt, text, _ in dl] for dl in lists] == [
        [("Task", f"#{t['id']} Fix login"), ("Project", "proj"), ("Role", "tester")],
        [("Task", f"#{t2['id']} Review login"), ("Project", "proj"), ("Stage", "reviews")],
        [("Device", "worker-host"), ("Folder", "/srv/proj/.worktrees/x")],
    ]
    device, folder = lists[-1]
    assert device[2] == ' title="worker-host.example.ts.net"'
    assert folder[2] == ' class="folder" title="/srv/proj/.worktrees/x"'
    assert "msg-btn" not in card and " · " not in card and "Role: " not in card  # no buttons: links open panels


def test_agent_card_without_task_has_only_device_and_folder(hub):
    a = hub.enroll("idle")
    html = get(hub, "/ui/agents").text
    card = agent_card(html, a)
    assert [[(dt, text) for dt, text, _ in dl] for dl in agent_facts(card)] == [
        [("Device", "idle-host"), ("Folder", "No checkouts reported")]]
    assert "No current task" not in card
    assert agent_facts(agent_card(get(hub, "/ui/dashboard").text, a))[0][0][0] == "Device"


def test_agent_card_folder_and_device_are_escaped(busy):
    hub, a, b, ap, t = busy
    report(hub, a, {"proj": f"/srv/{EVIL}"}, None)
    card = agent_card(get(hub, "/ui/agents").text, a)
    assert "<script>alert" not in card and 'title="/srv/&lt;script&gt;alert(1)&lt;/script&gt;"' in card


@pytest.mark.parametrize("name, short", [("white5070.tail1234.ts.net", "white5070"), ("box", "box"),
                                         ("100.64.0.7", "100.64.0.7"), ("fd7a:115c::1", "fd7a:115c::1"),
                                         ("", ""), (None, "")])
def test_short_host(name, short):
    from agenthub.hub import dashboard
    assert dashboard.short_host(name) == short


def test_agent_page_and_panel_task_rows_put_each_fact_on_its_own_line(two_tasks):
    hub, a, t, t2 = two_tasks
    expected = [[("Project", "proj"), ("Role", "tester"), ("Stage", "works")],
                [("Project", "proj"), ("Stage", "reviews")]]
    for html in (get(hub, f"/ui/agents/{a.id}").text, panel(hub, f"agent:{a.id}").text):
        assert [[(dt, text) for dt, text, _ in dl] for dl in agent_facts(html)] == expected
        assert "Role: " not in html and "Fix login" in html and "Review login" in html


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
    assert "last seen" in html and "ago" in html and "CPU, RAM and GPU memory" in html


def test_revoked_only_device_hidden_and_empty_state(hub):
    assert "No devices yet" in get(hub, "/ui/devices").text
    a = hub.enroll("alpha")
    hub.set_status(a, "revoked")
    assert views.devices(hub.conn()) == [] and "No devices yet" in get(hub, "/ui/devices").text


# --- Tap to message ------------------------------------------------------------------------


@pytest.fixture
def reach(hub):
    """worker + helper on task t (helper offline), a pending and a revoked agent that must never be offered."""
    a, b = hub.enroll("worker"), hub.enroll("helper")
    p, r = hub.enroll("newbie"), hub.enroll("gone")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET connected = 1, prompt_support = 1 WHERE id = ?", (a.id,))
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Fix login", assignee=None)
        for ag in (a, b, p, r):
            task_agents.add_agent(conn, t["id"], ag.id, role="r", stage="works", actor=OPERATOR)
        conn.execute("UPDATE agents SET status = 'pending' WHERE id = ?", (p.id,))  # members may lose standing later
        conn.execute("UPDATE agents SET status = 'revoked' WHERE id = ?", (r.id,))
    return hub, a, b, p, r, t


def names_of(hub, target):
    return [x["name"] for x in views.recipients(hub.conn(), target)["agents"]]


def test_recipients_per_target_kind(reach):
    hub, a, b, p, r, t = reach
    conn = hub.conn()
    assert names_of(hub, f"agent:{a.id}") == ["worker"]
    assert names_of(hub, f"agent:{p.id}") == [] and names_of(hub, f"agent:{r.id}") == []
    task = views.recipients(conn, f"task:{t['id']}")
    assert sorted(x["name"] for x in task["agents"]) == ["helper", "worker"]
    assert task["board"] == ("proj", f"task-{t['id']}")
    proj = views.recipients(conn, "project:proj")
    assert sorted(x["name"] for x in proj["agents"]) == ["helper", "worker"] and proj["board"] == ("proj", "general")
    assert names_of(hub, "device:nworker") == ["worker"] and names_of(hub, "device:nnewbie") == []
    assert views.recipients(conn, "device:nworker")["board"] is None
    assert views.recipients(conn, f"agent:{a.id}")["label"]


@pytest.mark.parametrize("target", ["", "agent", "agent:nope", "task:abc", "task:9999", "project:nope", "device:nope",
                                    "thing:1", f"agent:"])
def test_recipients_unknown_target_is_404(hub, target):
    with pytest.raises(store.NotFound):
        views.recipients(hub.conn(), target)
    assert get(hub, "/ui/compose?to=" + target).status_code == 404


def test_project_recipients_are_its_allowed_agents_with_or_without_tasks(reach):
    """A project reaches every active agent allowed into it, so a new project with no tasks can be prompted."""
    hub, a, b, p, r, t = reach
    hub.enroll("elsewhere", projects=("other",))
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET state = 'done' WHERE id = ?", (t["id"],))
    assert names_of(hub, "project:proj") == ["helper", "worker"]  # no live task; pending and revoked still left out
    assert names_of(hub, "project:other") == ["elsewhere"]
    with db.transaction(conn):  # access withdrawn: no longer reached, though still on the project's task
        conn.execute("UPDATE agents SET allowed_projects = '[]' WHERE id = ?", (b.id,))
        conn.execute("UPDATE tasks SET state = 'in_progress' WHERE id = ?", (t["id"],))
    assert names_of(hub, "project:proj") == ["worker"]


def links(html):
    """The items a page opens in the slide-in panel (``data-panel``): each entity is clickable, not a button."""
    return set(re.findall(r'data-panel="([^"]+)"', html))


def test_every_page_opens_each_item_in_the_panel(reach):
    hub, a, b, p, r, t = reach
    tid = t["id"]
    ag = {f"agent:{x.id}" for x in (a, b)}
    assert ag | {f"task:{tid}"} <= links(get(hub, "/ui/dashboard").text)
    assert ag | {f"task:{tid}", "project:proj"} <= links(get(hub, "/ui/projects").text)
    assert ag | {f"task:{tid}"} <= links(get(hub, "/ui/projects/proj").text)
    assert {f"task:{tid}"} <= links(get(hub, "/ui/tasks").text)
    assert ag <= links(get(hub, f"/ui/tasks/{tid}").text)
    assert ag <= links(get(hub, "/ui/agents").text)
    assert {f"task:{tid}", "project:proj"} <= links(get(hub, f"/ui/agents/{a.id}").text)
    assert ag | {"device:nworker", "device:nhelper"} <= links(get(hub, "/ui/devices").text)


def test_no_page_has_message_buttons(reach):
    hub, a, b, p, r, t = reach
    for path in ("/ui/dashboard", "/ui/agents", "/ui/projects", "/ui/projects/proj", "/ui/tasks", f"/ui/tasks/{t['id']}",
                 "/ui/devices", "/ui/devices/nworker", "/ui/board", "/ui/chats", f"/ui/agents/{a.id}"):
        html = get(hub, path).text
        assert "msg-btn" not in html and 'hx-target="#compose"' not in html and ">Message</a>" not in html, path


def test_agent_card_directory_caption_is_visible(reach):
    hub, *_ = reach
    html = get(hub, "/ui/agents").text
    assert "reported by the agent" in html and 'title="reported by the agent"' not in html


# --- Slide-in panel ---------------------------------------------------------------------------


@pytest.fixture
def side(hub):
    """worker (connected) on task t in proj with a command approval, a question, and its agent, task and project chats."""
    a, c = hub.enroll("worker"), hub.enroll("helper")
    r = hub.enroll("gone")
    hub.set_status(r, "revoked")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET connected = 1, prompt_support = 1 WHERE id = ?", (a.id,))
        t = tasks.create_task(conn, actor=a.id, project="proj", title="Fix <b>login</b>", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
        task_agents.add_agent(conn, t["id"], c.id, role="", stage="reviews", actor=OPERATOR)
    ap = hub.client.post("/api/v1/approvals/command", headers=a.headers,
                         json={"command": "git push -f origin x", "cwd": f"/w/{EVIL}", "reason": f"why {EVIL}"}).json()
    hub.client.post(f"/api/v1/approvals/{ap['id']}/discussion", headers=a.headers, json={"body": f"reply {EVIL}"})
    q = hub.client.post("/api/v1/projects/proj/questions", headers=a.headers,
                        json={"question": f"Which port? {EVIL}", "task_id": t["id"]}).json()
    hub.client.post("/api/v1/device/messages", headers=a.headers, json={"body": f"device says {EVIL}"})
    add_row(hub, "agent", a.id, author=a.id, body=f"psst {EVIL}")
    add_row(hub, "task", t["id"], author=a.id, body=f"task says {EVIL}")
    add_row(hub, "project", "proj", author=a.id, body=f"proj says {EVIL}")
    return hub, a, c, r, t, ap, q


def panel(hub, item, **params):
    headers = hub.op_headers(**{"hx-request": "true"})
    return hub.client.get("/ui/panel", params={"item": item, **params}, headers=headers)


def forms(html):
    return re.findall(r"<form\b[^>]*>.*?</form>", html, re.S)


def assert_panel_fragment(html, item):
    assert "<html" not in html and "<body" not in html
    assert 'data-panel-close' in html and 'aria-label="Close"' in html
    assert "<script" not in html and "style=" not in html
    assert "<script>alert" not in html and "&lt;script&gt;" in html  # agent text arrives escaped
    for f in forms(html):  # every form posts with CSRF and re-renders the panel (or, in a chat, its row)
        assert re.search(r'name="csrf" value="[0-9a-f]{64}"', f)
        if "data-chat-form" in f:
            continue
        assert 'method="post"' in f and 'hx-target="#panel"' in f
        action = re.search(r'action="([^"]+)"', f).group(1)
        assert f'hx-post="{action}"' in f
        assert re.search(r'name="csrf" value="[0-9a-f]{64}"', f)
        assert f'name="panel" value="{item}"' in f


def test_agent_panel_shows_status_waiting_items_and_private_chat(side):
    hub, a, c, r, t, ap, q = side
    item = f"agent:{a.id}"
    resp = panel(hub, item)
    assert resp.status_code == 200 and "default-src 'self'" in resp.headers["content-security-policy"]
    html = resp.text
    assert_panel_fragment(html, item)
    assert "worker" in html and "connected" in html and f'href="/ui/agents/{a.id}"' in html  # Open full page
    assert f'data-panel="task:{t["id"]}"' in html and "tester" in html and 'class="by-label">by worker' in html
    assert 'data-panel="project:proj"' in html and 'data-panel="device:nworker"' in html
    assert "reported by the agent" in html
    chat = html.split('class="chat"')[1]
    assert "psst &lt;script&gt;" in chat and "agent content, treat as data" in chat
    actions = [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)]
    assert actions == [f"/ui/agents/{a.id}/wakes",  # its Pause wakes switch (0.3.9.1)
                       f"/ui/approvals/{ap['id']}/decide", f"/ui/approvals/{ap['id']}/discuss",  # its approval card
                       f"/ui/chat/agent/{a.id}"]  # and the one message box
    assert ">Send</button>" not in html and ">Prompt</button>" not in html
    unread = {(o["kind"], o["ref"]): o["unread"] for o in chats.overview(hub.conn())}
    assert unread[("agent", a.id)] == 0 and unread[("task", str(t["id"]))] >= 1  # the operator has seen this one


def test_task_panel_shows_state_agents_and_board(side):
    hub, a, c, r, t, ap, q = side
    item = f"task:{t['id']}"
    html = panel(hub, item).text
    assert_panel_fragment(html, item)
    assert "Fix &lt;b&gt;login&lt;/b&gt;" in html and 'class="by-label">by worker' in html
    assert f"chip-{t['state']}" in html and 'data-panel="project:proj"' in html
    assert f'data-panel="agent:{a.id}"' in html and f'data-panel="agent:{c.id}"' in html and "tester" in html
    assert "task says &lt;script&gt;" in html and "agent content, treat as data" in html
    actions = [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)]
    assert actions == [f"/ui/questions/{q['id']}/answer", f"/ui/chat/task/{t['id']}",  # the question card (about t)
                       f"/ui/chat/task/{t['id']}"]  # and the one message box
    assert ">Post</button>" not in html and "/ui/board" not in html
    assert f'href="/ui/tasks/{t["id"]}"' in html


def test_project_panel_shows_tasks_and_board(side):
    hub, a, c, r, t, ap, q = side
    html = panel(hub, "project:proj").text
    assert_panel_fragment(html, "project:proj")
    assert f'data-panel="task:{t["id"]}"' in html and "proj says &lt;script&gt;" in html
    assert "agent content, treat as data" in html
    assert [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)] == ["/ui/chat/project/proj"]
    assert "/ui/board" not in html and 'href="/ui/projects/proj"' in html


def test_device_panel_shows_agents_and_no_chat(side):
    hub, a, c, r, t, ap, q = side
    html = panel(hub, "device:nworker").text
    assert "<html" not in html and 'data-panel-close' in html
    assert "worker-host.example.ts.net" in html and "online" in html and f'data-panel="agent:{a.id}"' in html
    assert "device says" not in html and not forms(html)  # devices have no chat (their board is the agents')
    assert 'href="/ui/devices/nworker"' in html


def test_approval_panel_has_request_thread_discuss_and_decide(side):
    hub, a, c, r, t, ap, q = side
    item = f"approval:{ap['id']}"
    html = panel(hub, item, next="/ui/dashboard").text
    assert_panel_fragment(html, item)
    request = html.split("Discuss with")[0]
    assert "git push -f origin x" in request and "agent content, treat as data" in request
    assert "/w/&lt;script&gt;" in request and "why &lt;script&gt;" in request
    assert f'data-panel="agent:{a.id}"' in request  # a command approval names no project
    thread = html.split("Discuss with")[1]
    assert "reply &lt;script&gt;" in thread
    actions = [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)]
    assert actions == [f"/ui/approvals/{ap['id']}/discuss", f"/ui/approvals/{ap['id']}/decide"]
    assert ">Reply</button>" in html
    decide = forms(html)[1]
    assert 'name="decision" value="approve"' in decide and 'name="decision" value="reject"' in decide
    assert all('name="next" value="/ui/dashboard"' in f for f in forms(html))


def test_question_panel_has_answer_form_then_the_answer(side):
    hub, a, c, r, t, ap, q = side
    item = f"question:{q['id']}"
    html = panel(hub, item).text
    assert_panel_fragment(html, item)
    assert "Which port? &lt;script&gt;" in html and f'data-panel="task:{t["id"]}"' in html
    assert "agent content, treat as data" in html
    assert f'data-panel="agent:{a.id}"' in html
    [f] = forms(html)
    assert f'action="/ui/questions/{q["id"]}/answer"' in f and 'name="answer"' in f and ">Answer</button>" in f
    resp = hub.client.post(f"/ui/questions/{q['id']}/answer", headers=hub.op_headers(**{"hx-request": "true"}),
                           data={"csrf": hub.csrf(), "answer": "8080", "panel": item})
    assert resp.status_code == 200 and "<html" not in resp.text
    assert "8080" in resp.text and not forms(resp.text)
    assert "your chat with worker" in resp.text  # the answer is in the asker's chat too


def test_revoked_agent_panel_is_read_only(side):
    hub, a, c, r, t, ap, q = side
    html = panel(hub, f"agent:{r.id}").text
    assert "gone" in html and "chip-revoked" in html and not forms(html) and "read-only" in html


def test_pending_agent_panel_renders(hub):
    p = hub.enroll("newbie", approve=False)
    html = panel(hub, f"agent:{p.id}").text
    assert "newbie" in html and "chip-pending" in html and 'data-chat="agent:' in html
    assert "data-chat-send" not in html and "waiting for your approval" in html  # a pending agent can't be woken


@pytest.mark.parametrize("item", ["", "agent", "agent:", "agent:nope", "task:abc", "task:9999", "task:-1",
                                  "task:99999999999999999999999", "project:nope", "project:", "device:nope",
                                  "approval:x", "approval:9999", "question:9999", "question:1.5", "thing:1",
                                  "pair:x", ":1", "task:1:2"])
def test_unknown_or_malformed_panel_item_is_404(side, item):
    hub = side[0]
    assert panel(hub, item).status_code == 404


def test_panel_needs_the_operator(side):
    hub, a, *_ = side
    assert hub.client.get(f"/ui/panel?item=agent:{a.id}", headers=a.headers).status_code == 403


def test_panel_next_must_be_one_of_our_pages(side):
    hub, a, *_ = side
    html = panel(hub, f"agent:{a.id}", next="https://evil.example").text
    assert "evil.example" not in html and 'name="next"' not in html
    assert 'name="next" value="/ui/tasks"' in panel(hub, f"agent:{a.id}", next="/ui/tasks").text


def post_panel(hub, url, item, htmx=True, **data):
    headers = hub.op_headers(**({"hx-request": "true"} if htmx else {}))
    return hub.client.post(url, headers=headers, follow_redirects=False,
                           data={"csrf": hub.csrf(), "panel": item, "next": "/ui/dashboard", **data})


def test_panel_forms_post_and_rerender_the_panel(side):
    hub, a, c, r, t, ap, q = side
    aid = ap["id"]
    resp = post_panel(hub, f"/ui/approvals/{aid}/discuss", f"approval:{aid}", body="discuss <i>from panel</i>")
    assert resp.status_code == 200 and "&lt;i&gt;from panel&lt;/i&gt;" in resp.text and 'class="msg mine"' in resp.text
    resp = post_panel(hub, f"/ui/approvals/{aid}/decide", f"approval:{aid}", decision="approve")
    assert resp.status_code == 200 and "Approved" in resp.text and not forms(resp.text)
    assert hub.conn().execute("SELECT status FROM approvals WHERE id = ?", (aid,)).fetchone()[0] == "approved"


def test_panel_forms_without_htmx_redirect_to_next(side):
    hub, a, c, r, t, ap, q = side
    for url, data in [(f"/ui/chat/agent/{a.id}", {"body": "x"}),
                      (f"/ui/chat/task/{t['id']}", {"body": "x"}),
                      (f"/ui/approvals/{ap['id']}/discuss", {"body": "x"}),
                      (f"/ui/questions/{q['id']}/answer", {"answer": "x"}),
                      (f"/ui/approvals/{ap['id']}/decide", {"decision": "reject"})]:
        resp = post_panel(hub, url, "agent:" + a.id, htmx=False, **data)
        assert resp.status_code == 303 and resp.headers["location"] == "/ui/dashboard", url


def test_panel_form_posts_still_need_csrf(side):
    hub, a, c, r, t, ap, q = side
    before = len(chats.messages(hub.conn(), "agent", a.id))
    resp = hub.client.post(f"/ui/chat/agent/{a.id}", headers=hub.op_headers(**{"x-chat": "1"}), data={"body": "x"})
    assert resp.status_code == 403 and len(chats.messages(hub.conn(), "agent", a.id)) == before
    resp = hub.client.post(f"/ui/approvals/{ap['id']}/discuss", headers=hub.op_headers(**{"hx-request": "true"}),
                           data={"body": "x", "panel": f"approval:{ap['id']}"})
    assert resp.status_code == 403


def test_base_has_panel_outside_main_and_live(side):
    hub = side[0]
    html = get(hub, "/ui/dashboard").text
    main, after = html.split("</main>")
    assert 'id="panel"' not in main and '<aside id="panel"' in after and "inert" in after.split("</aside>")[0]
    assert 'id="panel-scrim"' in after


PANEL_PAGES = {"agents": "agent", "tasks": "task", "projects": "project", "devices": "device",
               "approvals": "approval", "questions": "question"}


def item_links(html):
    """(href, data-panel) of every link to an agent, task, project, device, approval or question page."""
    out = []
    for tag in re.findall(r"<a\b[^>]*>", html):
        href = re.search(r'href="([^"]*)"', tag)
        m = href and re.fullmatch(r"/ui/(\w+)/([^/?\"]+)", href.group(1))
        if m and m.group(1) in PANEL_PAGES:
            dp = re.search(r'data-panel="([^"]*)"', tag)
            out.append((href.group(1), dp.group(1) if dp else None))
    return out


def test_every_page_links_items_to_the_panel(side):
    hub, a, c, r, t, ap, q = side
    tid = t["id"]
    pages = ["/ui/dashboard", "/ui/projects", "/ui/tasks", "/ui/agents", "/ui/devices", "/ui/approvals", "/ui/chats",
             "/ui/projects/proj", f"/ui/tasks/{tid}", f"/ui/agents/{a.id}", "/ui/devices/nworker",
             f"/ui/approvals/{ap['id']}", f"/ui/questions/{q['id']}"]
    seen = set()
    for path in pages:
        html = get(hub, path).text
        assert "style=" not in html and not re.search(r"<script(?![^>]*\bsrc=)", html), path
        for href, item in item_links(html):
            kind, _, key = href.removeprefix("/ui/").partition("/")
            assert item == f"{PANEL_PAGES[kind]}:{key.replace('%3A', ':')}", (path, href, item)
            seen.add(item)
        for tag in re.findall(r"<[a-z]+\b[^>]*data-panel=[^>]*>", html):
            assert tag.startswith("<a ") and 'href="/ui/' in tag or 'class="card' in tag, (path, tag)
    assert {f"agent:{a.id}", f"agent:{c.id}", f"task:{tid}", "project:proj", "device:nworker",
            f"approval:{ap['id']}", f"question:{q['id']}"} <= seen
    listing = get(hub, "/ui/chats").text  # a chat row opens the item's panel, which is its chat
    for item, href in ((f"agent:{a.id}", f"/ui/agents/{a.id}"), (f"agent:{c.id}", f"/ui/agents/{c.id}"),
                       (f"task:{tid}", f"/ui/tasks/{tid}"), ("project:proj", "/ui/projects/proj")):
        assert f'href="{href}" data-panel="{item}"' in listing, item


def test_cards_open_the_panel_and_keep_their_links(side):
    hub, a, *_ = side
    for path in ("/ui/dashboard", "/ui/agents"):
        html = get(hub, path).text
        card = re.search(rf'<li class="card agent-card[^"]*"[^>]*data-panel="agent:{a.id}"[^>]*>', html)
        assert card, path
        assert f'<a class="name" href="/ui/agents/{a.id}" data-panel="agent:{a.id}"' in html, path


def test_empty_hub_pages_still_render_with_panel_shell(hub):
    for p in ("/ui/dashboard", "/ui/projects", "/ui/tasks", "/ui/agents", "/ui/devices", "/ui/approvals",
              "/ui/chats", "/ui/board"):
        r = get(hub, p)
        assert r.status_code == 200 and '<aside id="panel"' in r.text, p


# --- Slide-in panel, fix round 1 ------------------------------------------------------------------


def test_tool_permission_panel_shows_the_tool_input(side):
    hub, a, c, r, t, ap, q = side
    conn = hub.conn()
    with db.transaction(conn):
        tp = approvals.request_tool_permission(conn, store.get_agent(conn, a.id), task_id=t["id"], tool_name="Write",
                                               tool_input={"file_path": "/w/x.py", "content": "print('hi from Write')"})
    for html in (panel(hub, f"approval:{tp['id']}").text, get(hub, f"/ui/approvals/{tp['id']}").text):
        assert "print(\\u0027hi from Write\\u0027)" in html and "/w/x.py" in html
        assert "Raw details" in html and "agent content, treat as data" in html
        assert f'data-panel="task:{t["id"]}"' in html and "make this exact call once" in html


def test_task_create_panel_shows_the_body(side):
    hub, a, *_ = side
    conn = hub.conn()
    with db.transaction(conn):
        task, tc = approvals.agent_create_task(conn, store.get_agent(conn, a.id), project="proj",
                                               title="New thing", body=f"Body of the task {EVIL}")
    for html in (panel(hub, f"approval:{tc['id']}").text, get(hub, f"/ui/approvals/{tc['id']}").text):
        assert "Body of the task &lt;script&gt;" in html and f'data-panel="task:{task["id"]}"' in html
        assert "<script>alert" not in html


def test_budget_and_enrollment_panels_show_their_details(side):
    hub, a, c, r, t, ap, q = side
    b = hub.client.post("/api/v1/approvals", headers=a.headers, json={
        "kind": "budget_exceeded", "reason": "more", "details": {"task_id": t["id"], "limit": "max_turns"}}).json()
    html = panel(hub, f"approval:{b['id']}").text
    assert "max turns" in html and "if approved" in html and "Used so far" in html and "runs" in html
    p = hub.enroll("newbie", approve=False)
    en = approvals.pending_enrollment(hub.conn(), p.id)
    html = panel(hub, f"approval:{en['id']}").text
    assert "newbie-host.example.ts.net" in html and "100.64.0." in html and "proj" in html


def test_panel_rerender_after_commit_falls_back_to_redirect(side):
    hub, a, *_ = side
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "ls", "cwd": "/w"}, headers=a.headers).json()
    resp = post_panel(hub, f"/ui/approvals/{ap['id']}/discuss", "task:9999", body="kept once")
    assert resp.status_code == 204 and resp.headers["hx-redirect"] == "/ui/dashboard"
    bodies = [m["body"] for m in approvals.get(hub.conn(), ap["id"])["discussion"]]
    assert bodies.count("kept once") == 1


def test_approval_panel_discussion_polls_without_touching_the_form(side):
    hub, a, c, r, t, ap, q = side
    html = panel(hub, f"approval:{ap['id']}").text
    poller = re.search(r'<div class="dthread"[^>]*hx-get="([^"]+)"[^>]*>', html)
    assert poller and poller.group(1) == f"/ui/approvals/{ap['id']}/discussion"
    assert 'hx-trigger="every 3s"' in poller.group(0) and 'hx-swap="innerHTML"' in poller.group(0)
    assert 'id="thread"' not in html and "<form" not in html.split(poller.group(0))[1].split("</div>\n")[0]
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    frag = get(hub, f"/ui/approvals/{ap['id']}/discussion")
    assert frag.status_code == 200 and "reply &lt;script&gt;" in frag.text and "agent content, treat as data" in frag.text
    assert "<html" not in frag.text and "<form" not in frag.text
    hub.client.post(f"/ui/approvals/{ap['id']}/decide", headers=hub.op_headers(),
                    data={"csrf": hub.csrf(), "decision": "reject"})
    assert get(hub, f"/ui/approvals/{ap['id']}/discussion").status_code == 286  # htmx stops polling
    assert 'hx-trigger="every 3s"' not in panel(hub, f"approval:{ap['id']}").text
    assert get(hub, "/ui/approvals/9999/discussion").status_code == 404


def test_panel_is_a_labelled_modal_dialog(side):
    after = get(side[0], "/ui/dashboard").text.split("</main>")[1]
    aside = re.search(r'<aside id="panel"[^>]*>', after).group(0)
    assert 'role="dialog"' in aside and 'aria-modal="true"' in aside and 'aria-labelledby="panel-title"' in aside
    for item in ("project:proj", "device:nworker"):
        assert 'id="panel-title"' in panel(side[0], item).text


# --- Floating buttons and the chat window ------------------------------------------------------------

def messenger(hub):
    return hub.client.get("/ui/messenger", headers=hub.op_headers(**{"hx-request": "true"}))


@pytest.fixture
def window(side):
    return side


def rows(html):
    """data-panel value -> the row's <a> markup, for every chat row in the list."""
    return {m.group(1): m.group(0) for m in re.finditer(r'<a class="crow"[^>]*data-panel="([^"]+)"[^>]*>.*?</a>', html, re.S)}


def assert_fragment(html):
    assert "<html" not in html and "<body" not in html
    assert "<script" not in html and "style=" not in html
    assert "<script>alert" not in html


def test_messenger_lists_every_section_with_the_right_rows(window):
    hub, a, c, r, t, ap, q = window
    resp = messenger(hub)
    assert resp.status_code == 200 and "default-src 'self'" in resp.headers["content-security-policy"]
    html = resp.text
    assert_fragment(html)
    heads = re.findall(r'<div class="chat-group">([^<]+)</div>', html)
    assert heads == ["Projects", "Agents", "Tasks"]  # no device boards, no agent-to-agent pairs
    found = rows(html)
    tid = t["id"]
    assert set(found) == {f"agent:{a.id}", f"agent:{c.id}", f"task:{tid}", "project:proj"}  # no revoked agent
    worker = found[f"agent:{a.id}"]
    assert f'href="/ui/agents/{a.id}"' in worker and 'class="unread"' in worker  # its message and its question
    assert "psst &lt;script&gt;" in worker and "agent text" in worker and "worker" in worker
    assert "psst" not in found[f"agent:{c.id}"]
    assert "status-" in worker  # its avatar status
    task_row = found[f"task:{tid}"]
    assert "Fix &lt;b&gt;login&lt;/b&gt;" in task_row and 'class="by-label">by worker' in task_row
    assert "task says &lt;script&gt;" in task_row
    assert task_row.count('<img class="avatar"') == 2  # its members, worker and helper
    assert found["project:proj"].count('<img class="avatar"') == 2  # agents on its live tasks
    order = [html.index(f'data-panel="{k}"') for k in ("project:proj", f"agent:{a.id}", f"task:{tid}")]
    assert order == sorted(order)
    poller = re.search(r'<div class="mscroll"[^>]*>', html).group(0)
    assert 'hx-get="/ui/messenger"' in poller and 'hx-trigger="every 10s"' in poller
    assert "data-messenger-close" in html and 'aria-label="Close chats"' in html


def test_messenger_lists_projects_and_tasks_before_anyone_posts(hub):
    a = hub.enroll("worker")
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "quiet", actor=OPERATOR)
        t = tasks.create_task(conn, actor=OPERATOR, project="quiet", title="Untouched", assignee=None)
        done = tasks.create_task(conn, actor=OPERATOR, project="quiet", title="Finished", assignee=None)
        tasks.transition(conn, done["id"], "cancelled", actor=OPERATOR, role="operator")
    found = rows(messenger(hub).text)
    assert "project:quiet" in found and f"task:{t['id']}" in found
    assert f"task:{done['id']}" not in found  # finished and never discussed
    assert f"agent:{a.id}" in found


def test_empty_hub_messenger_renders(hub):
    resp = messenger(hub)
    assert resp.status_code == 200 and "No chats yet" in resp.text and not rows(resp.text)


def test_messenger_needs_the_operator(window):
    hub, a, *_ = window
    assert hub.client.get("/ui/messenger", headers=a.headers).status_code == 403
    assert hub.client.get(f"/ui/chat/agent/{a.id}?after=0", headers=a.headers).status_code == 403
    assert hub.client.get("/ui/chat/msg/1", headers=a.headers).status_code == 403
    assert hub.client.get("/ui/badge/messenger", headers=a.headers).status_code == 403


def test_new_project_is_created_with_csrf_and_duplicates_are_409(hub):
    data = {"csrf": hub.csrf(), "name": "mobile-app", "description": "The <b>phone</b> app"}
    r = hub.client.post("/ui/projects", headers=hub.op_headers(), data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/mobile-app"
    row = hub.conn().execute("SELECT description FROM projects WHERE name = 'mobile-app'").fetchone()
    assert row["description"] == "The <b>phone</b> app"
    ev = hub.conn().execute("SELECT actor FROM events WHERE type = 'project.created' AND project = 'mobile-app'").fetchone()
    assert ev["actor"] == f"human:{OP_LOGIN}"
    r = hub.client.post("/ui/projects", headers=hub.op_headers(), data={**data, "description": "again"})
    assert r.status_code == 409
    assert hub.conn().execute("SELECT description FROM projects WHERE name = 'mobile-app'").fetchone()[0] == data["description"]
    assert hub.client.post("/ui/projects", headers=hub.op_headers(),  # names are unique ignoring case
                           data={**data, "name": "Mobile-App"}).status_code == 409
    for bad in ("-x", "a b", "x" * 65, "", "caf\u00e9"):
        assert hub.client.post("/ui/projects", headers=hub.op_headers(),
                               data={**data, "name": bad}).status_code == 422, bad
    r = hub.client.post("/ui/projects", headers=hub.op_headers(), data={"name": "no-csrf"})
    assert r.status_code == 403
    # the operator's description has no length limit
    long = hub.client.post("/ui/projects", headers=hub.op_headers(), data={**data, "name": "long", "description": "x" * 70_000})
    assert long.status_code == 200
    assert {p["name"] for p in store.list_projects(hub.conn())} == {"mobile-app", "long"}


def test_new_project_from_the_panel_shows_its_panel(hub):
    resp = post_panel(hub, "/ui/projects", "new:project", name="from-panel", description="desc")
    assert resp.status_code == 200 and 'id="panel-title">from-panel<' in resp.text and "desc" in resp.text


@pytest.mark.parametrize("kind,action", [("task", "/ui/tasks"), ("project", "/ui/projects"), ("agent", "/ui/agents/enroll")])
def test_plus_menu_forms_open_in_the_panel(busy, kind, action):
    hub = busy[0]
    item = f"new:{kind}"
    html = panel(hub, item, next="/ui/dashboard").text
    assert "<html" not in html and "<script" not in html and "style=" not in html
    assert 'data-panel-close' in html and 'id="panel-title"' in html
    [f] = forms(html)
    assert f'action="{action}"' in f and f'hx-post="{action}"' in f and 'hx-target="#panel"' in f
    assert re.search(r'name="csrf" value="[0-9a-f]{64}"', f) and f'name="panel" value="{item}"' in f
    if kind == "task":
        assert 'name="role"' in f and 'name="assignee"' in f and 'name="title"' in f and 'value="proj"' in f
        assert 'value="worker"' in f and 'value="gone"' not in f
    if kind == "project":
        assert 'name="name"' in f and 'pattern="[A-Za-z0-9][A-Za-z0-9_\\-]{0,63}"' in f and "<textarea" in f
        assert "names ignore case" in f.lower() and "<textarea" in f and "maxlength" not in f.split("<textarea")[1]


def test_new_task_and_agent_from_the_panel(busy):
    hub = busy[0]
    resp = post_panel(hub, "/ui/tasks", "new:task", project="proj", title="From <i>panel</i>", assignee="worker",
                      role="Implementer")
    assert resp.status_code == 200 and "From &lt;i&gt;panel&lt;/i&gt;" in resp.text and "Implementer" in resp.text
    t = tasks.list_tasks(hub.conn(), projects=None, newest_first=True, limit=1)[0]
    assert t["title"] == "From <i>panel</i>" and t["created_by"] == f"human:{OP_LOGIN}"
    assert f'id="panel-title"' in resp.text and f"task #{t['id']}" in resp.text
    r = post_panel(hub, "/ui/tasks", "new:task", htmx=False, project="proj", title="plain")
    assert r.status_code == 303 and re.fullmatch(r"/ui/tasks/\d+", r.headers["location"])
    resp = post_panel(hub, "/ui/agents/enroll", "new:agent", projects="proj")
    assert resp.status_code == 200 and "<html" not in resp.text and "Enrollment code" in resp.text
    assert re.search(r'class="code">[^<]+<', resp.text) and "agenthub join" in resp.text


def test_unknown_new_form_is_404(hub):
    assert panel(hub, "new:thing").status_code == 404 and panel(hub, "new:").status_code == 404


FAB_PAGES = ["/ui/dashboard", "/ui/projects", "/ui/tasks", "/ui/agents", "/ui/devices", "/ui/approvals", "/ui/chats",
             "/ui/reports", "/ui/search?q=x", "/ui/activity"]


def test_floating_buttons_and_chat_window_on_every_page(window):
    hub, a, c, r, t, ap, q = window
    pages = FAB_PAGES + [f"/ui/agents/{a.id}", f"/ui/tasks/{t['id']}", "/ui/projects/proj", "/ui/devices/nworker",
                         f"/ui/approvals/{ap['id']}", f"/ui/questions/{q['id']}"]
    for path in pages:
        html = get(hub, path).text
        assert "style=" not in html and not re.search(r"<script(?![^>]*\bsrc=)", html), path
        main, after = html.split("</main>")
        assert 'class="fabs"' not in main and 'id="messenger"' not in main, path
        assert re.search(r'<button type="button" class="fab fab-plus" id="fab-plus"[^>]*aria-expanded="false"', after), path
        assert re.search(r'<a class="fab fab-chat" id="fab-chat" href="/ui/chats"', after), path
        assert re.search(r'<section id="messenger" class="messenger"[^>]*hidden', after), path
        menu = re.search(r'<div class="plus-menu" id="plus-menu"[^>]*>(.*?)</div>', after, re.S)
        assert menu and 'hidden' in menu.group(0).split(">")[0], path
        assert re.findall(r'data-new="(\w+)"', menu.group(1)) == ["task", "project", "agent"], path


def test_chat_button_counts_unread_chats_and_pending_questions(window):
    hub, a, c, r, t, ap, q = window
    add_row(hub, "agent", c.id, author=c.id, body="me too")
    after = get(hub, "/ui/dashboard").text.split("</main>")[1]
    fab = re.search(r'<a class="fab fab-chat".*?</a>', after, re.S).group(0)
    assert 'hx-get="/ui/badge/messenger"' in fab and 'hx-trigger="every 10s"' in fab
    unread = sum(o["unread"] for o in chats.overview(hub.conn()) if o["ref"] != r.id)  # not the revoked agent's
    assert unread >= 4 and re.search(rf'class="fab-badge">{unread + 1}<', fab)  # unread chat rows + one question
    assert re.search(rf'class="fab-badge">{unread + 1}<', get(hub, "/ui/badge/messenger").text)
    for item in (f"agent:{a.id}", f"agent:{c.id}", f"task:{t['id']}", "project:proj"):
        panel(hub, item)
    assert re.search(r'class="fab-badge">1<', get(hub, "/ui/badge/messenger").text)
    hub.client.post(f"/ui/questions/{q['id']}/answer", headers=hub.op_headers(), data={"csrf": hub.csrf(), "answer": "x"})
    assert get(hub, "/ui/badge/messenger").text == ""


def test_empty_hub_pages_have_the_buttons(hub):
    for p in FAB_PAGES:
        r = get(hub, p)
        assert r.status_code == 200 and 'id="fab-chat"' in r.text and "fab-badge" not in r.text, p


# --- Floating buttons and the chat window, fix round 1 -------------------------------------------------------

def test_slide_in_panel_comes_out_wide_but_fits_a_phone(hub):
    css = hub.client.get("/ui/static/app.css").text
    m = re.search(r"^\.drawer \{[^}]*width: min\((\d+)px, 94vw\);", css, re.M)
    assert m and int(m.group(1)) >= 600


# --- The failed look on agents -------------------------------------------------------------------------

def online(hub, *agents, connected=True, prompts=True):
    conn = hub.conn()
    with db.transaction(conn):
        for ag in agents:
            conn.execute("UPDATE agents SET connected = ?, prompt_support = ? WHERE id = ?",
                         (int(connected), int(prompts), ag.id))


def make_prompt(hub, target, body="Look at the flaky test"):
    from agenthub.hub import prompts
    conn = hub.conn()
    with db.transaction(conn):
        return prompts.create(conn, target=target, body=body, actor=OPERATOR)


def fail_hook(html, key):
    """The opening tags in ``html`` that carry the failed hook for ``key``."""
    return re.findall(r'<[a-z]+\b[^>]*data-fail-key="' + re.escape(key) + r'"[^>]*>', html)


def test_an_agent_whose_prompt_session_failed_gets_the_failed_card(window):
    hub, a, c, r, t, ap, q = window
    from agenthub.hub import prompts
    d = hub.enroll("dora")
    online(hub, d)
    p = make_prompt(hub, f"agent:{d.id}", body="try it")
    for path in ("/ui/dashboard", "/ui/agents"):
        [card] = fail_hook(get(hub, path).text, f"agent:{d.id}")
        assert "is-failed" not in card and "data-failed" not in card
    conn = hub.conn()
    with db.transaction(conn):
        prompts.start(conn, p["id"], store.get_agent(conn, d.id))
        prompts.finish(conn, p["id"], store.get_agent(conn, d.id), ok=False, note="crashed")
    for path in ("/ui/dashboard", "/ui/agents"):
        html = get(hub, path).text
        [card] = fail_hook(html, f"agent:{d.id}")
        assert card.startswith('<li class="card agent-card is-failed"') and " data-failed " in card
        assert "/failed.svg" in html and 'class="avatar-status status-failed">failed<' in html


def test_app_js_plays_failed_new_once_when_a_key_turns_failed(hub):
    js = hub.client.get("/ui/static/app.js").text
    assert "[data-fail-key]" in js and 'hasAttribute("data-failed")' in js
    assert 'classList.add("failed-new")' in js and 'classList.remove("failed-new")' in js  # one shot, replayable
    assert "failedSeen[" in js and "!== false" in js  # only a key this page last showed not failed


def answer(hub, qid, text):
    return hub.client.post(f"/ui/questions/{qid}/answer", headers=hub.op_headers(), follow_redirects=False,
                           data={"csrf": hub.csrf(), "answer": text})


def assert_answered_in_chat(hub, agent, qid, text):
    """0.3.8: whatever the agent is doing, the answer is the operator's message in its chat, delivered to it (its
    runner gives it to the running session or wakes its chat session); no prompt, no private message."""
    conn = hub.conn()
    assert conn.execute("SELECT COUNT(*) FROM prompts WHERE body LIKE 'Answer%'").fetchone()[0] == 0
    assert [m for m in private.thread(conn, agent.id) if m["author"].startswith("human:")] == []
    tid = conn.execute("SELECT task_id FROM questions WHERE id = ?", (qid,)).fetchone()[0]
    about = f"about task #{tid} in proj; " if tid else ""
    msg = [m for m in chats.messages(conn, "agent", agent.id) if m["kind"] == "text"][-1]
    assert msg["body"] == f"Answer to your question #{qid}: {text}\n\n({about}call get_question({qid}) for your question)"
    assert msg["author"] == f"human:{OP_LOGIN}"
    assert [(d["agent_id"], d["status"]) for d in msg["deliveries"]] == [(agent.id, "pending")]
    assert questions.answer_delivery(conn, questions.get(conn, qid))["chat_message_id"] == msg["id"]
    for html in (get(hub, f"/ui/questions/{qid}").text, panel(hub, f"question:{qid}").text):
        assert "/ui/prompts/" not in html and "not delivered" not in html


@pytest.mark.parametrize("state", ["idle", "offline", "old_runner", "task_run"])
def test_an_answer_reaches_the_asking_agent_in_its_chat(window, state):
    hub, a, c, r, t, ap, q = window  # worker (a) is connected
    conn = hub.conn()
    with db.transaction(conn):
        if state == "offline":
            conn.execute("UPDATE agents SET connected = 0 WHERE id = ?", (a.id,))
        elif state == "old_runner":
            conn.execute("UPDATE agents SET prompt_support = 0, chat_support = 0 WHERE id = ?", (a.id,))
        elif state == "task_run":
            conn.execute("INSERT INTO task_runs (task_id, agent_id, started_at) VALUES (?, ?, ?)",
                         (t["id"], a.id, db.now_iso()))
    assert answer(hub, q["id"], "Use 8080").status_code == 303
    assert_answered_in_chat(hub, a, q["id"], "Use 8080")


def test_an_answer_to_a_revoked_agent_is_recorded_only(window):
    hub, a, c, r, t, ap, q = window
    texts = lambda: [m for m in chats.messages(hub.conn(), "agent", a.id) if m["kind"] == "text"]  # noqa: E731
    before = texts()
    hub.set_status(a, "revoked")
    assert answer(hub, q["id"], "Use 8080").status_code == 303
    assert texts() == before  # no answer message for a revoked agent
    assert "not delivered because worker is revoked" in get(hub, f"/ui/questions/{q['id']}").text


def test_agent_page_and_panel_show_the_reported_model(busy):
    hub, a, b, ap, t = busy
    assert "Not reported yet" in get(hub, f"/ui/agents/{a.id}").text
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET model = 'claude-opus-5-5' WHERE id = ?", (a.id,))
    assert "claude-opus-5-5" in get(hub, f"/ui/agents/{a.id}").text
    assert "claude-opus-5-5" in panel(hub, f"agent:{a.id}").text


def test_agent_page_and_panel_show_the_reported_effort(busy):
    hub, a, b, ap, t = busy
    html = get(hub, f"/ui/agents/{a.id}").text
    assert "<dt>Effort" in html and html.count("Not reported yet") == 2  # neither model nor effort yet
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET effort = 'xhigh' WHERE id = ?", (a.id,))
    assert "<dd>xhigh</dd>" in get(hub, f"/ui/agents/{a.id}").text
    side = panel(hub, f"agent:{a.id}").text
    assert "<dt>Effort</dt><dd>xhigh " in side


# --- logo ---------------------------------------------------------------------------


def test_every_page_shows_the_fishbowl_logo_and_tab_icon(hub):
    html = hub.client.get("/ui/dashboard", headers=hub.op_headers()).text
    assert '<link rel="icon" type="image/svg+xml" href="/ui/static/fishbowl.svg">' in html
    assert '<link rel="apple-touch-icon" href="/ui/static/fishbowl-192.png">' in html
    assert '<img class="brand-icon" src="/ui/static/fishbowl.svg"' in html and 'alt="agenthub"' in html
    for path, kind in (("/ui/static/fishbowl.svg", "image/svg+xml"), ("/ui/static/fishbowl-32.png", "image/png"),
                       ("/ui/static/fishbowl-192.png", "image/png")):
        r = hub.client.get(path, headers=hub.op_headers())
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path


def test_logo_files_match_their_generator():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("fishbowl", root / "scripts" / "fishbowl.py")
    fishbowl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fishbowl)
    art = fishbowl.draw()
    static = root / "src" / "agenthub" / "hub" / "static"
    assert (static / "fishbowl.svg").read_text() == fishbowl.svg(art)
    assert (static / "fishbowl-32.png").read_bytes() == fishbowl.png(art, 1)
    assert "<script" not in fishbowl.svg(art)
