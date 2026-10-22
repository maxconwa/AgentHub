"""Page shell, Dashboard and Reports placeholder."""

import json
import re

import pytest

from agenthub.hub import approvals, db, private, store, task_agents, tasks, views

from .conftest import OP_LOGIN, OPERATOR

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


# --- Board ------------------------------------------------------------------------

def say(hub, agent, body, channel="general", to=None, project="proj"):
    r = hub.client.post(f"/api/v1/projects/{project}/messages", headers=agent.headers,
                        json={"channel": channel, "body": body, **({"to": to} if to else {})})
    assert r.status_code in (200, 201), r.text


@pytest.fixture
def talk(busy):
    hub, a, b, ap, t = busy
    c = hub.enroll("helper")
    say(hub, a, "hello general")
    say(hub, a, "task chatter", channel=f"task-{t['id']}")
    say(hub, a, "split the work", channel="coordination-1")
    say(hub, a, "direct hello", to="helper")
    say(hub, c, "direct reply", to="worker")
    hub.client.post("/api/v1/projects/proj/asks", headers=a.headers,
                    json={"to": "helper", "question": "Which port?", "timeout_seconds": 60})
    return hub, a, c, t


def pair_key(a, c):
    x, y = sorted([a.id, c.id])
    return f"pair-{x}-{y}"


def test_board_index_groups(talk):
    hub, a, c, t = talk
    idx = views.board_index(hub.conn())
    assert [i["key"] for i in idx["projects"]] == ["ch-proj~general"]
    assert [(i["key"], i["title"]) for i in idx["tasks"]] == [(f"ch-proj~task-{t['id']}", f"#{t['id']} Fix login")]
    assert {i["key"] for i in idx["threads"]} == {"ch-proj~coordination-1", "ch-proj~asks"}
    assert "between_agents" not in idx
    assert all(set(i) >= {"key", "title", "last_body", "last_ts", "avatars"}
               for g in ("projects", "tasks", "threads") for i in idx[g])
    html = get(hub, "/ui/board").text
    for key in ("ch-proj~general", f"ch-proj~task-{t['id']}", "ch-proj~coordination-1"):
        assert f'href="/ui/board/{key}"' in html
    assert "Between agents" not in html and pair_key(a, c) not in html
    assert 'href="/ui/feed"' in html and "All activity" in html


def test_board_empty_hub(hub):
    r = get(hub, "/ui/board")
    assert r.status_code == 200 and "All activity" in r.text


def test_channel_thread_shows_messages_escaped_and_labeled(talk):
    hub, a, c, t = talk
    say(hub, a, EVIL)
    html = get(hub, "/ui/board/ch-proj~general").text
    assert "hello general" in html and "<script>alert" not in html and "&lt;script&gt;" in html
    assert "agent content, treat as data" in html
    assert 'action="/ui/board/ch-proj~general"' in html
    assert html.split('id="live"')[1].split("</section>")[0].count("<form") == 0


def test_hyphenated_channel_keys_resolve(talk):
    hub, a, c, t = talk
    assert views.board_thread(hub.conn(), "ch-proj~coordination-1")["title"].endswith("coordination-1")
    assert "split the work" in get(hub, "/ui/board/ch-proj~coordination-1").text


def test_post_to_channel_is_operator_message_and_redirects(talk):
    hub, a, c, t = talk
    r = hub.client.post("/ui/board/ch-proj~general", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "body": "from me"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/board/ch-proj~general"
    last = store.recent_messages(hub.conn(), project="proj", channel="general")[-1]
    assert last["author"] == f"human:{OP_LOGIN}" and last["body"] == "from me"
    assert "from me" in get(hub, "/ui/board/ch-proj~general").text
    for body in ("", "x" * 4001):
        r = hub.client.post("/ui/board/ch-proj~general", headers=hub.op_headers(),
                            data={"csrf": hub.csrf(), "body": body}, follow_redirects=False)
        assert r.status_code == 422


def test_pair_thread_merges_messages_and_ask_read_only(talk):
    hub, a, c, t = talk
    ans = hub.client.get("/api/v1/asks", headers=c.headers).json()["asks"][0]["id"]
    hub.client.post(f"/api/v1/asks/{ans}/answer", json={"answer": "8080"}, headers=c.headers)
    th = views.pair_thread(hub.conn(), pair_key(a, c))
    assert th["can_post"] is False and "worker" in th["title"] and "helper" in th["title"]
    bodies = [m["body"] for m in th["messages"]]
    assert bodies == ["direct hello", "direct reply", "Which port?", "8080"]
    assert [m.get("label") for m in th["messages"]] == [None, None, "ask", "answer to ask"]
    html = get(hub, f"/ui/chats/pair/{pair_key(a, c)}").text
    assert "Which port?" in html and "8080" in html and "<textarea" not in html
    assert "agent content, treat as data" in html
    r = hub.client.post(f"/ui/chats/pair/{pair_key(a, c)}", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "body": "hi"}, follow_redirects=False)
    assert r.status_code == 405


def test_old_board_pair_url_redirects_308(talk):
    hub, a, c, t = talk
    r = hub.client.get(f"/ui/board/{pair_key(a, c)}", headers=hub.op_headers(), follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == f"/ui/chats/pair/{pair_key(a, c)}"
    assert get(hub, "/ui/board/ch-proj~general").status_code == 200


def test_agent_pairs_newest_first_with_labels(talk):
    hub, a, c, t = talk
    d = hub.enroll("third")
    say(hub, d, "psst", to="worker")
    pairs = views.agent_pairs(hub.conn())
    assert [p["key"] for p in pairs] == [pair_key(a, d), pair_key(a, c)]
    assert pairs[1]["title"] in ("worker ↔ helper", "helper ↔ worker")
    html = get(hub, "/ui/chats").text
    assert "Between agents" in html and f'href="/ui/chats/pair/{pair_key(a, c)}"' in html
    assert html.index(f"/ui/chats/pair/{pair_key(a, d)}") < html.index(f"/ui/chats/pair/{pair_key(a, c)}")
    assert html.index("Between agents") < html.index("Start a chat")
    assert "Message helper" in html


def test_chats_page_lists_private_chats_before_pairs(talk):
    hub, a, c, t = talk
    hub.client.post("/api/v1/private", headers=a.headers, json={"body": "hi operator"})
    html = get(hub, "/ui/chats").text
    assert html.index("With you") < html.index("Between agents") < html.index("Start a chat")


def test_agent_page_links_its_pair_chats(talk):
    hub, a, c, t = talk
    html = get(hub, f"/ui/agents/{a.id}").text
    assert f'href="/ui/chats/pair/{pair_key(a, c)}"' in html
    gone = hub.conn().execute("SELECT id FROM agents WHERE name = 'gone'").fetchone()["id"]
    assert pair_key(a, c) not in get(hub, f"/ui/agents/{gone}").text


@pytest.mark.parametrize("key", ["nope", "ch-", "ch-proj~missing", "ch-nosuch~general", "ch-proj-general", "ch-proj~", "ch-~general", "ch-proj~~general", "ch-proj~general~x", "ch-PROJ~general"])
def test_malformed_or_unknown_key_is_404(talk, key):
    hub, *_ = talk
    assert get(hub, f"/ui/board/{key}").status_code == 404
    r = hub.client.post(f"/ui/board/{key}", headers=hub.op_headers(), data={"csrf": hub.csrf(), "body": "x"})
    assert r.status_code == 404


@pytest.mark.parametrize("key", ["pair-x-y", "pair-a", "pair--", "nope", "pair-ag01-ag02"])
def test_malformed_or_unknown_pair_key_is_404(talk, key):
    hub, *_ = talk
    assert get(hub, f"/ui/chats/pair/{key}").status_code == 404
    r = hub.client.post(f"/ui/chats/pair/{key}", headers=hub.op_headers(), data={"csrf": hub.csrf(), "body": "x"})
    assert r.status_code in (404, 405)


def test_private_messages_never_on_board(talk):
    hub, a, c, t = talk
    secret = "SECRET-PRIVATE-TEXT"
    hub.client.post(f"/ui/chats/{a.id}", headers=hub.op_headers(), data={"csrf": hub.csrf(), "body": secret})
    hub.client.post("/api/v1/private", headers=a.headers, json={"body": secret + "-agent"})
    paths = ["/ui/board"] + [f"/ui/board/{k}" for k in
                             ("ch-proj~general", "ch-proj~asks", "ch-proj~coordination-1")] + [f"/ui/chats/pair/{pair_key(a, c)}"]
    for p in paths:
        assert "SECRET-PRIVATE" not in get(hub, p).text, p


def test_colliding_hyphenated_names_resolve_to_different_threads(hub):
    a = hub.enroll("worker", projects=("a", "a-b"))
    say(hub, a, "in a / b-c", channel="b-c", project="a")
    say(hub, a, "in a-b / c", channel="c", project="a-b")
    one, two = (get(hub, f"/ui/board/ch-{k}").text for k in ("a~b-c", "a-b~c"))
    assert "in a / b-c" in one and "in a-b / c" not in one
    assert "in a-b / c" in two and "in a / b-c" not in two
    assert {i["key"] for i in views.board_index(hub.conn())["threads"]} >= {"ch-a~b-c", "ch-a-b~c"}


def test_agent_chosen_channel_and_task_title_labeled(talk):
    hub, a, c, t = talk
    html = get(hub, "/ui/board").text
    assert html.count('<span class="by-label">by worker</span>') >= 2  # coordination-1 channel, asks channel
    assert "Fix login" in html and 'by-label">by worker' in get(hub, "/ui/board/ch-proj~coordination-1").text
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET created_by = ? WHERE id = ?", (c.id, t["id"]))
    html = get(hub, "/ui/board").text
    assert 'Fix login</span> <span class="by-label">by helper</span>' in html
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET created_by = ? WHERE id = ?", (OPERATOR, t["id"]))
    assert "Fix login</span> <span" not in get(hub, "/ui/board").text


# --- Tap to message ------------------------------------------------------------------------


@pytest.fixture
def reach(hub):
    """worker + helper on task t (helper offline), a pending and a revoked agent that must never be offered."""
    a, b = hub.enroll("worker"), hub.enroll("helper")
    p, r = hub.enroll("newbie"), hub.enroll("gone")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET connected = 1 WHERE id = ?", (a.id,))
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


def test_project_recipients_ignore_finished_tasks(reach):
    hub, a, b, p, r, t = reach
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET state = 'done' WHERE id = ?", (t["id"],))
    assert names_of(hub, "project:proj") == []


def test_compose_page_lists_recipients_and_marks_offline(reach):
    hub, a, b, p, r, t = reach
    html = get(hub, f"/ui/compose?to=task:{t['id']}&next=/ui/tasks/{t['id']}").text
    assert html.count('type="checkbox"') == 2 and html.count(" checked") == 2
    assert "worker" in html and "helper" in html and "newbie" not in html and "gone" not in html
    assert html.count('<span class="small-line">offline</span>') == 1 and "Post on the board instead" in html
    assert 'name="next" value="/ui/tasks/' in html
    assert "Post on the board instead" not in get(hub, f"/ui/compose?to=agent:{a.id}").text


def test_compose_partial_for_htmx_and_next_filtering(reach):
    hub, a, *_ = reach
    r = hub.client.get(f"/ui/compose?to=agent:{a.id}&next=https://evil.example", headers=hub.op_headers(**{"hx-request": "true"}))
    assert "<html" not in r.text and "evil.example" not in r.text and 'name="next"' not in r.text
    assert "<html" in get(hub, f"/ui/compose?to=agent:{a.id}").text


def test_compose_without_recipients(reach):
    hub, a, b, p, r, t = reach
    html = get(hub, f"/ui/compose?to=agent:{p.id}").text
    assert "No active agents" in html and "<textarea" not in html
    conn = hub.conn()
    with db.transaction(conn):
        tk = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Alone", assignee=None)
    html = get(hub, f"/ui/compose?to=task:{tk['id']}").text
    assert "No active agents" in html and "Post on the board instead" in html and 'name="recipients"' not in html


def send(hub, **data):
    return hub.client.post("/ui/compose", headers=hub.op_headers(), data={"csrf": hub.csrf(), "mode": "send", **data},
                           follow_redirects=False)


def chat(hub, agent):
    return [m["body"] for m in private.thread(hub.conn(), agent.id)]


def test_send_delivers_one_private_message_per_recipient(reach):
    hub, a, b, p, r, t = reach
    resp = send(hub, to=f"task:{t['id']}", recipients=[a.id, b.id], body="hello team")
    assert resp.status_code == 303 and resp.headers["location"] == f"/ui/tasks/{t['id']}"
    assert chat(hub, a) == ["hello team"] and chat(hub, b) == ["hello team"]
    sent = [m for m in private.thread(hub.conn(), a.id)]
    assert sent[0]["author"] == f"human:{OP_LOGIN}"
    resp = send(hub, to=f"task:{t['id']}", recipients=[a.id], body="just you")
    assert chat(hub, a) == ["hello team", "just you"] and chat(hub, b) == ["hello team"]


@pytest.mark.parametrize("target,loc", [("agent:{a}", "/ui/agents/{a}"), ("project:proj", "/ui/projects/proj"),
                                        ("device:nworker", "/ui/devices/nworker")])
def test_send_redirects_to_target_page(reach, target, loc):
    hub, a, *_ = reach
    resp = send(hub, to=target.format(a=a.id), recipients=[a.id], body="x")
    assert resp.headers["location"] == loc.format(a=a.id)


def test_send_redirects_to_valid_next_only(reach):
    hub, a, *_ = reach
    assert send(hub, to=f"agent:{a.id}", recipients=[a.id], body="x", next="/ui/tasks").headers["location"] == "/ui/tasks"
    for bad in ("https://evil.example", "//evil.example", "/ui/../x", "/other"):
        loc = send(hub, to=f"agent:{a.id}", recipients=[a.id], body="x", next=bad).headers["location"]
        assert loc == f"/ui/agents/{a.id}", bad


def test_send_rejects_recipient_outside_target_and_empty_selection(reach):
    hub, a, b, p, r, t = reach
    for bad in (p.id, r.id, "ag-nope"):
        assert send(hub, to=f"agent:{a.id}", recipients=[a.id, bad], body="x").status_code == 400
    assert send(hub, to=f"agent:{a.id}", recipients=[b.id], body="x").status_code == 400
    assert send(hub, to=f"agent:{a.id}", body="x").status_code == 400
    assert chat(hub, a) == [] and chat(hub, b) == []  # nothing partially delivered


def test_send_validates_body_and_target(reach):
    hub, a, *_ = reach
    assert send(hub, to=f"agent:{a.id}", recipients=[a.id], body="").status_code == 422
    assert send(hub, to=f"agent:{a.id}", recipients=[a.id], body="x" * 4001).status_code == 422
    assert send(hub, to="agent:nope", recipients=[a.id], body="x").status_code == 404
    assert send(hub, to=f"agent:{a.id}", recipients=[a.id], body="x", mode="bogus").status_code == 422


def test_board_mode_posts_to_the_right_channel(reach):
    hub, a, b, p, r, t = reach
    resp = send(hub, to=f"task:{t['id']}", body="for the board", mode="board")
    assert resp.status_code == 303 and resp.headers["location"] == f"/ui/tasks/{t['id']}"
    conn = hub.conn()
    msgs = store.recent_messages(conn, project="proj", channel=f"task-{t['id']}", limit=5)
    assert [(m["author"], m["body"]) for m in msgs] == [(f"human:{OP_LOGIN}", "for the board")]
    send(hub, to="project:proj", body="project note", mode="board")
    assert [m["body"] for m in store.recent_messages(conn, project="proj", channel="general", limit=5)] == ["project note"]
    assert chat(hub, a) == []  # the board is public, not a private message
    assert send(hub, to=f"agent:{a.id}", body="x", mode="board").status_code == 400
    assert send(hub, to="device:nworker", body="x", mode="board").status_code == 400


def test_compose_post_requires_csrf(reach):
    hub, a, *_ = reach
    r = hub.client.post("/ui/compose", headers=hub.op_headers(), follow_redirects=False,
                        data={"to": f"agent:{a.id}", "recipients": [a.id], "body": "x", "mode": "send"})
    assert r.status_code == 403 and chat(hub, a) == []


def test_compose_close_is_empty(hub):
    r = get(hub, "/ui/compose/close")
    assert r.status_code == 200 and r.text == ""


def links(html):
    return set(re.findall(r'href="/ui/compose\?to=([^&"]+)', html))


def test_every_page_has_a_compose_link_per_item(reach):
    hub, a, b, p, r, t = reach
    tid = t["id"]
    ag = {f"agent%3A{x.id}" for x in (a, b)}
    assert ag | {f"task%3A{tid}"} <= links(get(hub, "/ui/dashboard").text)
    pr = links(get(hub, "/ui/projects").text)
    assert ag | {f"task%3A{tid}", "project%3Aproj"} <= pr
    assert ag | {f"task%3A{tid}", "project%3Aproj"} <= links(get(hub, "/ui/projects/proj").text)
    assert ag | {f"task%3A{tid}"} <= links(get(hub, "/ui/tasks").text)
    assert ag | {f"task%3A{tid}"} <= links(get(hub, f"/ui/tasks/{tid}").text)
    assert ag <= links(get(hub, "/ui/agents").text)
    assert f"agent%3A{a.id}" in links(get(hub, f"/ui/agents/{a.id}").text)
    assert f"task%3A{tid}" in links(get(hub, f"/ui/agents/{a.id}").text)
    dev = links(get(hub, "/ui/devices").text)
    assert ag | {"device%3Anworker", "device%3Anhelper"} <= dev
    send(hub, to=f"task:{tid}", body="n", mode="board")
    send(hub, to="project:proj", body="n", mode="board")
    board = links(get(hub, "/ui/board").text)
    assert {f"task%3A{tid}", "project%3Aproj"} <= board


def test_compose_links_cover_every_task_project_and_pair_agent(reach):
    hub, a, b, p, r, t = reach
    conn = hub.conn()
    with db.transaction(conn):
        t2 = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Second chore", assignee=None)
        task_agents.add_agent(conn, t2["id"], a.id, role="r2", stage="works", actor=OPERATOR)
        store.post_message(conn, author=a.id, project="proj", channel="general", body="hi", to="helper")
    for path in ("/ui/dashboard", "/ui/agents"):
        found = links(get(hub, path).text)
        assert {f"task%3A{t['id']}", f"task%3A{t2['id']}", "project%3Aproj"} <= found, path
    assert {f"agent%3A{a.id}", f"agent%3A{b.id}"} <= links(get(hub, "/ui/chats").text)


def test_agent_buttons_hidden_for_pending_and_revoked_agents(reach):
    hub, a, b, p, r, t = reach
    tid = t["id"]
    for path in ("/ui/dashboard", "/ui/agents", "/ui/projects", "/ui/projects/proj", "/ui/tasks", f"/ui/tasks/{tid}",
                 "/ui/devices", "/ui/board", f"/ui/agents/{p.id}", f"/ui/agents/{r.id}"):
        found = links(get(hub, path).text)
        assert f"agent%3A{p.id}" not in found and f"agent%3A{r.id}" not in found, path
        assert f"agent%3A{a.id}" in found or path.startswith("/ui/agents/") or path == "/ui/board", path
    assert f"task%3A{tid}" in links(get(hub, "/ui/tasks").text)  # task targets keep theirs


def test_message_button_markup(reach):
    hub, a, *_ = reach
    html = get(hub, "/ui/tasks").text
    assert 'hx-target="#compose"' in html and 'id="compose"' in html
    assert re.search(r'aria-label="Message the agents on task #\d+"[^>]*>Message</a>', html)
    assert "next=%2Fui%2Ftasks" in html and "style=" not in html


def test_agent_card_directory_caption_is_visible(reach):
    hub, *_ = reach
    html = get(hub, "/ui/agents").text
    assert "reported by the agent" in html and 'title="reported by the agent"' not in html


# --- Slide-in panel ---------------------------------------------------------------------------


@pytest.fixture
def side(hub):
    """worker (connected) on task t in proj with a command approval, a question, a private chat and boards."""
    a, c = hub.enroll("worker"), hub.enroll("helper")
    r = hub.enroll("gone")
    hub.set_status(r, "revoked")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET connected = 1 WHERE id = ?", (a.id,))
        t = tasks.create_task(conn, actor=a.id, project="proj", title="Fix <b>login</b>", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
        task_agents.add_agent(conn, t["id"], c.id, role="", stage="reviews", actor=OPERATOR)
        store.post_message(conn, author=a.id, project="proj", channel=f"task-{t['id']}", body=f"task says {EVIL}")
        store.post_message(conn, author=a.id, project="proj", channel="general", body=f"proj says {EVIL}")
    ap = hub.client.post("/api/v1/approvals/command", headers=a.headers,
                         json={"command": "git push -f origin x", "cwd": f"/w/{EVIL}", "reason": f"why {EVIL}"}).json()
    hub.client.post(f"/api/v1/approvals/{ap['id']}/discussion", headers=a.headers, json={"body": f"reply {EVIL}"})
    q = hub.client.post("/api/v1/projects/proj/questions", headers=a.headers,
                        json={"question": f"Which port? {EVIL}", "task_id": t["id"]}).json()
    hub.client.post("/api/v1/private", headers=a.headers, json={"body": f"psst {EVIL}"})
    hub.client.post("/api/v1/device/messages", headers=a.headers, json={"body": f"device says {EVIL}"})
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
    for f in forms(html):  # every form posts with CSRF and re-renders the panel
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
    waiting = html.split("Waiting on you")[1].split("Private chat")[0]
    assert f'data-panel="approval:{ap["id"]}"' in waiting and f'href="/ui/approvals/{ap["id"]}"' in waiting
    assert f'data-panel="question:{q["id"]}"' in waiting and f'href="/ui/questions/{q["id"]}"' in waiting
    assert "git push -f origin x" in waiting and "agent text" in waiting
    chat = html.split("Private chat")[1]
    assert "psst &lt;script&gt;" in chat and "agent content, treat as data" in chat
    assert [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)] == [f"/ui/chats/{a.id}"]
    assert ">Send</button>" in html
    assert private.unread_counts(hub.conn()) == {}  # the operator has seen it now


def test_task_panel_shows_state_agents_and_board(side):
    hub, a, c, r, t, ap, q = side
    item = f"task:{t['id']}"
    html = panel(hub, item).text
    assert_panel_fragment(html, item)
    assert "Fix &lt;b&gt;login&lt;/b&gt;" in html and 'class="by-label">by worker' in html
    assert f"chip-{t['state']}" in html and 'data-panel="project:proj"' in html
    assert f'data-panel="agent:{a.id}"' in html and f'data-panel="agent:{c.id}"' in html and "tester" in html
    assert "task says &lt;script&gt;" in html and "agent content, treat as data" in html
    assert [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)] == [f"/ui/tasks/{t['id']}/notes"]
    assert ">Post</button>" in html and f'href="/ui/tasks/{t["id"]}"' in html


def test_project_panel_shows_tasks_and_board(side):
    hub, a, c, r, t, ap, q = side
    html = panel(hub, "project:proj").text
    assert_panel_fragment(html, "project:proj")
    assert f'data-panel="task:{t["id"]}"' in html and "proj says &lt;script&gt;" in html
    assert "agent content, treat as data" in html
    assert [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)] == ["/ui/projects/proj/notes"]
    assert 'href="/ui/projects/proj"' in html


def test_device_panel_shows_agents_and_device_board(side):
    hub, a, c, r, t, ap, q = side
    html = panel(hub, "device:nworker").text
    assert_panel_fragment(html, "device:nworker")
    assert "worker-host.example.ts.net" in html and "online" in html and f'data-panel="agent:{a.id}"' in html
    assert "device says &lt;script&gt;" in html and "agent content, treat as data" in html
    assert [re.search(r'action="([^"]+)"', f).group(1) for f in forms(html)] == ["/ui/devices/nworker/notes"]
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
    assert ">Discuss</button>" in html
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
    assert "8080" in resp.text and not forms(resp.text) and "private message" in resp.text


def test_revoked_agent_panel_is_read_only(side):
    hub, a, c, r, t, ap, q = side
    html = panel(hub, f"agent:{r.id}").text
    assert "gone" in html and "chip-revoked" in html and not forms(html) and "read-only" in html


def test_pending_agent_panel_renders(hub):
    p = hub.enroll("newbie", approve=False)
    html = panel(hub, f"agent:{p.id}").text
    assert "newbie" in html and "chip-pending" in html and f'action="/ui/chats/{p.id}"' in html


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
    tid, aid = t["id"], ap["id"]
    cases = [
        (f"/ui/chats/{a.id}", f"agent:{a.id}", {"body": "chat <i>from panel</i>"}),
        (f"/ui/tasks/{tid}/notes", f"task:{tid}", {"body": "task <i>from panel</i>"}),
        ("/ui/projects/proj/notes", "project:proj", {"body": "project <i>from panel</i>"}),
        ("/ui/devices/nworker/notes", "device:nworker", {"body": "device <i>from panel</i>"}),
        (f"/ui/approvals/{aid}/discuss", f"approval:{aid}", {"body": "discuss <i>from panel</i>"}),
    ]
    for url, item, data in cases:
        resp = post_panel(hub, url, item, **data)
        assert resp.status_code == 200, url
        assert "<html" not in resp.text and 'data-panel-close' in resp.text, url
        assert "&lt;i&gt;from panel&lt;/i&gt;" in resp.text and 'class="msg mine"' in resp.text, url
        assert 'name="next" value="/ui/dashboard"' in resp.text, url
    assert private.thread(hub.conn(), a.id)[-1]["author"] == f"human:{OP_LOGIN}"
    resp = post_panel(hub, f"/ui/approvals/{aid}/decide", f"approval:{aid}", decision="approve")
    assert resp.status_code == 200 and "Approved" in resp.text and not forms(resp.text)
    assert hub.conn().execute("SELECT status FROM approvals WHERE id = ?", (aid,)).fetchone()[0] == "approved"


def test_panel_forms_without_htmx_redirect_to_next(side):
    hub, a, c, r, t, ap, q = side
    for url, data in [(f"/ui/chats/{a.id}", {"body": "x"}), (f"/ui/tasks/{t['id']}/notes", {"body": "x"}),
                      ("/ui/projects/proj/notes", {"body": "x"}), ("/ui/devices/nworker/notes", {"body": "x"}),
                      (f"/ui/approvals/{ap['id']}/discuss", {"body": "x"}),
                      (f"/ui/questions/{q['id']}/answer", {"answer": "x"}),
                      (f"/ui/approvals/{ap['id']}/decide", {"decision": "reject"})]:
        resp = post_panel(hub, url, "agent:" + a.id, htmx=False, **data)
        assert resp.status_code == 303 and resp.headers["location"] == "/ui/dashboard", url


def test_panel_form_posts_still_need_csrf(side):
    hub, a, c, r, t, ap, q = side
    resp = hub.client.post(f"/ui/chats/{a.id}", headers=hub.op_headers(**{"hx-request": "true"}),
                           data={"body": "x", "panel": f"agent:{a.id}"})
    assert resp.status_code == 403 and private.thread(hub.conn(), a.id)[-1]["body"] != "x"


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
             "/ui/board", f"/ui/projects/proj", f"/ui/tasks/{tid}", f"/ui/agents/{a.id}", "/ui/devices/nworker",
             f"/ui/approvals/{ap['id']}", f"/ui/questions/{q['id']}", f"/ui/chats/{a.id}"]
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
    board = get(hub, "/ui/board").text
    for item in (f"task:{tid}", "project:proj", "device:nworker"):
        assert f'data-panel="{item}"' in board, item
    chats = get(hub, "/ui/chats").text  # a chat row opens the agent's panel, which holds the chat
    assert f'href="/ui/chats/{a.id}" data-panel="agent:{a.id}"' in chats
    assert f'href="/ui/chats/{c.id}" data-panel="agent:{c.id}"' in chats


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
    resp = post_panel(hub, "/ui/projects/proj/notes", "task:9999", body="kept once")
    assert resp.status_code == 204 and resp.headers["hx-redirect"] == "/ui/dashboard"
    bodies = [m["body"] for m in store.recent_messages(hub.conn(), project="proj", channel="general")]
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

def messenger(hub, key=None):
    path = "/ui/messenger" if key is None else f"/ui/messenger/{key}"
    return hub.client.get(path, headers=hub.op_headers(**{"hx-request": "true"}))


@pytest.fixture
def window(side):
    hub, a, c, r, t, ap, q = side
    say(hub, a, f"direct {EVIL}", to="helper")
    return side


def rows(html):
    """data-chat value -> the row's <a> markup, for every conversation row in the list."""
    return {m.group(1): m.group(0) for m in re.finditer(r'<a class="crow"[^>]*data-chat="([^"]+)"[^>]*>.*?</a>', html, re.S)}


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
    assert heads == ["Agents", "Between agents", "Projects and tasks", "Device boards"]
    found = rows(html)
    tid = t["id"]
    assert set(found) == {f"agent:{a.id}", f"agent:{c.id}", f"pair:{pair_key(a, c)}", "board:ch-proj~general",
                          f"board:ch-proj~task-{tid}", "device:nworker", "device:nhelper"}  # no revoked agent
    worker = found[f"agent:{a.id}"]
    assert f'href="/ui/chats/{a.id}"' in worker and 'class="unread"' in worker  # its message and its question
    assert "psst &lt;script&gt;" in worker and "agent text" in worker and "worker" in worker
    assert 'class="unread"' not in found[f"agent:{c.id}"] and "No messages yet" in found[f"agent:{c.id}"]
    assert "status-" in worker  # its avatar status
    assert f'href="/ui/chats/pair/{pair_key(a, c)}"' in found[f"pair:{pair_key(a, c)}"]
    assert "worker ↔ helper" in found[f"pair:{pair_key(a, c)}"] or "helper ↔ worker" in found[f"pair:{pair_key(a, c)}"]
    task_row = found[f"board:ch-proj~task-{tid}"]
    assert "Fix &lt;b&gt;login&lt;/b&gt;" in task_row and 'class="by-label">by worker' in task_row
    assert task_row.count('<img class="avatar"') == 2  # its members, worker and helper
    assert found["board:ch-proj~general"].count('<img class="avatar"') == 2  # agents on its live tasks
    assert "worker-host.example.ts.net" in found["device:nworker"]
    order = [html.index(f'data-chat="{k}"') for k in (f"agent:{a.id}", f"pair:{pair_key(a, c)}",
                                                     "board:ch-proj~general", "device:nworker")]
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
    assert "board:ch-quiet~general" in found and f"board:ch-quiet~task-{t['id']}" in found
    assert f"board:ch-quiet~task-{done['id']}" not in found  # finished and never discussed
    assert f"agent:{a.id}" in found


def test_empty_hub_messenger_renders(hub):
    resp = messenger(hub)
    assert resp.status_code == 200 and "No chats yet" in resp.text and not rows(resp.text)


def test_pair_row_opens_its_read_only_conversation(window):
    hub, a, c, *_ = window
    resp = messenger(hub, f"pair:{pair_key(a, c)}")
    assert resp.status_code == 200
    html = resp.text
    assert_fragment(html)
    assert "direct &lt;script&gt;" in html and "agent content, treat as data" in html
    assert not forms(html) and "read-only" in html
    assert 'data-chat-list' in html and 'aria-label="Back to chats"' in html and "data-messenger-close" in html
    assert f'href="/ui/chats/pair/{pair_key(a, c)}"' in html


def test_agent_conversation_sends_a_private_message(window):
    hub, a, *_ = window
    key = f"agent:{a.id}"
    html = messenger(hub, key).text
    assert_fragment(html)
    assert "psst &lt;script&gt;" in html and "agent content, treat as data" in html
    assert f'href="/ui/agents/{a.id}" data-panel="agent:{a.id}"' in html  # the header opens its panel
    assert private.unread_counts(hub.conn()) == {}  # the operator has read it now
    [f] = forms(html)
    assert f'action="/ui/chats/{a.id}"' in f and f'hx-post="/ui/chats/{a.id}"' in f and 'hx-target="#messenger"' in f
    assert re.search(r'name="csrf" value="[0-9a-f]{64}"', f) and f'name="messenger" value="{key}"' in f
    resp = hub.client.post(f"/ui/chats/{a.id}", headers=hub.op_headers(**{"hx-request": "true"}),
                           data={"csrf": hub.csrf(), "body": "hi <i>there</i>", "messenger": key})
    assert resp.status_code == 200 and "<html" not in resp.text and "data-chat-list" in resp.text
    assert "hi &lt;i&gt;there&lt;/i&gt;" in resp.text and 'class="msg mine"' in resp.text
    last = private.thread(hub.conn(), a.id)[-1]
    assert last["author"] == f"human:{OP_LOGIN}" and last["body"] == "hi <i>there</i>"
    r = hub.client.post(f"/ui/chats/{a.id}", headers=hub.op_headers(), follow_redirects=False,
                        data={"csrf": hub.csrf(), "body": "plain", "messenger": key})
    assert r.status_code == 303 and r.headers["location"] == f"/ui/chats/{a.id}"
    r = hub.client.post(f"/ui/chats/{a.id}", headers=hub.op_headers(**{"hx-request": "true"}),
                        data={"body": "no csrf", "messenger": key})
    assert r.status_code == 403


def test_agent_conversation_shows_its_pending_questions(window):
    hub, a, c, r, t, ap, q = window
    html = messenger(hub, f"agent:{a.id}").text
    assert f'data-panel="question:{q["id"]}"' in html and "Which port? &lt;script&gt;" in html and "agent text" in html


def test_revoked_agent_conversation_is_read_only(window):
    hub, a, c, r, *_ = window
    html = messenger(hub, f"agent:{r.id}").text
    assert not forms(html) and "read-only" in html


def test_board_conversation_posts_to_the_board(window):
    hub, a, c, r, t, ap, q = window
    tid = t["id"]
    key = f"board:ch-proj~task-{tid}"
    html = messenger(hub, key).text
    assert_fragment(html)
    assert "task says &lt;script&gt;" in html and f'href="/ui/tasks/{tid}" data-panel="task:{tid}"' in html
    [f] = forms(html)
    action = f"/ui/board/ch-proj~task-{tid}"
    assert f'action="{action}"' in f and f'hx-post="{action}"' in f and 'hx-target="#messenger"' in f
    assert f'name="messenger" value="{key}"' in f and re.search(r'name="csrf" value="[0-9a-f]{64}"', f)
    resp = hub.client.post(action, headers=hub.op_headers(**{"hx-request": "true"}),
                           data={"csrf": hub.csrf(), "body": "board <i>note</i>", "messenger": key})
    assert resp.status_code == 200 and "board &lt;i&gt;note&lt;/i&gt;" in resp.text and "data-chat-list" in resp.text
    last = store.recent_messages(hub.conn(), project="proj", channel=f"task-{tid}")[-1]
    assert last["author"] == f"human:{OP_LOGIN}" and last["body"] == "board <i>note</i>"
    general = messenger(hub, "board:ch-proj~general").text
    assert 'href="/ui/projects/proj" data-panel="project:proj"' in general and 'action="/ui/board/ch-proj~general"' in general


def test_board_conversation_starts_a_board_nobody_posted_on(hub):
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "quiet", actor=OPERATOR)
        t = tasks.create_task(conn, actor=OPERATOR, project="quiet", title="Untouched", assignee=None)
    for channel in ("general", f"task-{t['id']}"):
        key = f"ch-quiet~{channel}"
        assert "Nothing posted here yet" in messenger(hub, f"board:{key}").text
        assert get(hub, f"/ui/board/{key}").status_code == 200
        resp = hub.client.post(f"/ui/board/{key}", headers=hub.op_headers(**{"hx-request": "true"}),
                               data={"csrf": hub.csrf(), "body": "first", "messenger": f"board:{key}"})
        assert resp.status_code == 200 and "first" in resp.text
        assert [m["body"] for m in store.recent_messages(hub.conn(), project="quiet", channel=channel)] == ["first"]
    arabic_id = "".join(chr(0x660 + int(d)) for d in str(t["id"]))  # int() would accept these digits
    for key in ("ch-quiet~task-9999", f"ch-quiet~task-{arabic_id}", "ch-quiet~task-99999999999999999999",
                "ch-docs~general"):
        assert get(hub, f"/ui/board/{key}").status_code == 404, key
        assert messenger(hub, f"board:{key}").status_code == 404, key


def test_device_conversation_posts_to_the_device_board(window):
    hub, *_ = window
    html = messenger(hub, "device:nworker").text
    assert_fragment(html)
    assert "device says &lt;script&gt;" in html and 'data-panel="device:nworker"' in html
    [f] = forms(html)
    assert 'action="/ui/devices/nworker/notes"' in f and 'name="messenger" value="device:nworker"' in f
    resp = hub.client.post("/ui/devices/nworker/notes", headers=hub.op_headers(**{"hx-request": "true"}),
                           data={"csrf": hub.csrf(), "body": "gpu is mine", "messenger": "device:nworker"})
    assert resp.status_code == 200 and "gpu is mine" in resp.text and "data-chat-list" in resp.text


@pytest.mark.parametrize("key", ["", "agent:", "agent:nope", "pair:x", "pair:pair-ag1-ag0", "board:", "board:nope",
                                 "board:ch-proj~missing", "board:ch-nosuch~general", "device:nope", "task:1",
                                 "project:proj", "thing", "agent"])
def test_unknown_or_malformed_conversation_is_404(window, key):
    hub = window[0]
    if key:
        assert messenger(hub, key).status_code == 404


def test_messenger_needs_the_operator(window):
    hub, a, *_ = window
    assert hub.client.get("/ui/messenger", headers=a.headers).status_code == 403
    assert hub.client.get(f"/ui/messenger/agent:{a.id}", headers=a.headers).status_code == 403
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
    for bad in ("Mobile", "-x", "a b", "x" * 65, ""):
        assert hub.client.post("/ui/projects", headers=hub.op_headers(),
                               data={**data, "name": bad}).status_code == 422, bad
    r = hub.client.post("/ui/projects", headers=hub.op_headers(), data={"name": "no-csrf"})
    assert r.status_code == 403
    long = hub.client.post("/ui/projects", headers=hub.op_headers(), data={**data, "name": "long", "description": "x" * 4001})
    assert long.status_code == 422
    assert {p["name"] for p in store.list_projects(hub.conn())} == {"mobile-app"}


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
        assert 'name="name"' in f and 'pattern="[a-z0-9][a-z0-9_\\-]{0,63}"' in f and 'maxlength="4000"' in f


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
             "/ui/board", "/ui/reports", "/ui/search?q=x", "/ui/feed"]


def test_floating_buttons_and_chat_window_on_every_page(window):
    hub, a, c, r, t, ap, q = window
    pages = FAB_PAGES + [f"/ui/agents/{a.id}", f"/ui/tasks/{t['id']}", "/ui/projects/proj", "/ui/devices/nworker",
                         f"/ui/approvals/{ap['id']}", f"/ui/questions/{q['id']}", f"/ui/chats/{a.id}",
                         f"/ui/chats/pair/{pair_key(a, c)}", "/ui/board/ch-proj~general", f"/ui/compose?to=agent:{a.id}"]
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
    hub.client.post("/api/v1/private", headers=c.headers, json={"body": "me too"})
    after = get(hub, "/ui/dashboard").text.split("</main>")[1]
    fab = re.search(r'<a class="fab fab-chat".*?</a>', after, re.S).group(0)
    assert 'hx-get="/ui/badge/messenger"' in fab and 'hx-trigger="every 10s"' in fab
    assert re.search(r'class="fab-badge">3<', fab)  # two unread private messages + one pending question
    assert re.search(r'class="fab-badge">3<', get(hub, "/ui/badge/messenger").text)
    messenger(hub, f"agent:{a.id}")
    messenger(hub, f"agent:{c.id}")
    assert re.search(r'class="fab-badge">1<', get(hub, "/ui/badge/messenger").text)
    hub.client.post(f"/ui/questions/{q['id']}/answer", headers=hub.op_headers(), data={"csrf": hub.csrf(), "answer": "x"})
    assert get(hub, "/ui/badge/messenger").text == ""


def test_empty_hub_pages_have_the_buttons(hub):
    for p in FAB_PAGES:
        r = get(hub, p)
        assert r.status_code == 200 and 'id="fab-chat"' in r.text and "fab-badge" not in r.text, p


# --- Floating buttons and the chat window, fix round 1 -------------------------------------------------------

def test_task_board_key_with_leading_zeros_is_404(window):
    hub, a, c, r, t, ap, q = window
    key = f"ch-proj~task-0{t['id']}"
    assert get(hub, f"/ui/board/{key}").status_code == 404
    assert messenger(hub, f"board:{key}").status_code == 404
    r = hub.client.post(f"/ui/board/{key}", headers=hub.op_headers(), data={"csrf": hub.csrf(), "body": "stray"})
    assert r.status_code == 404
    assert not hub.conn().execute("SELECT 1 FROM channels WHERE name = ?", (f"task-0{t['id']}",)).fetchone()


def test_messenger_titles_use_the_shared_by_label(window):
    hub, a, c, r, t, ap, q = window
    conn = hub.conn()
    with db.transaction(conn):
        store.post_message(conn, author="system:hub", project="proj", channel="overlap-1", body="hub note")
    html = messenger(hub, "board:ch-proj~overlap-1").text
    assert "hub note" in html and "by-label" not in html  # the hub's own thread, not agent text
    html = messenger(hub, f"board:ch-proj~task-{t['id']}").text
    assert 'id="messenger-title"><span class="task-title">#' in html and 'class="by-label">by worker' in html


def test_buttons_step_aside_for_the_compose_sheet(hub):
    css = hub.client.get("/ui/static/app.css").text
    js = hub.client.get("/ui/static/app.js").text
    assert re.search(r"body\.compose-open \.fabs[^{]*\{ display: none; \}", css)
    assert 'classList.toggle("compose-open"' in js and 'getElementById("compose")' in js
    html = get(hub, "/ui/dashboard").text
    assert '<div id="compose"></div>' in html.split("</main>")[0]  # the region app.js watches
