"""Page shell, Dashboard and Reports placeholder."""

import re

import pytest

from agenthub.hub import db, task_agents, tasks, views

from .conftest import OPERATOR

LABELS = ["Dashboard", "Projects", "Tasks", "Agents", "Devices", "Approvals", "Chats", "Reports"]


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
                             "role": "tester", "stage": "works"}]
    assert row["avatar"]["pose"] and row["device"] and "connected" in row
