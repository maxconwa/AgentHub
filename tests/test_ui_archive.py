"""#772 on the dashboard: chats listed projects, then agents, then tasks (the order set in #1120); archived (done) tasks left out of the
at-a-glance views and listed, folded, on their agents' pages and panels."""

import re

import pytest

from agenthub.hub import chats, db, task_agents, tasks, views

from .conftest import OPERATOR
from .test_ui_wakes import say


def get(hub, path, **headers):
    return hub.client.get(path, headers=hub.op_headers(**headers))


def move(hub, task_id, *states):
    conn = hub.conn()
    for s in states:
        with db.transaction(conn):
            tasks.transition(conn, task_id, s, actor=OPERATOR, role="operator")


def chat_rows(html):
    """data-panel values of the chat list's rows, in page order."""
    return re.findall(r'<a class="crow"[^>]*data-panel="([^"]+)"', html)


def groups(html):
    return re.findall(r'<div class="chat-group">([^<]+)</div>', html)


@pytest.fixture
def shelf(hub):
    """worker on a live task and a done (archived) one, both with chat; a cancelled task with chat; a project."""
    a = hub.enroll("worker")
    conn = hub.conn()
    made = {}
    with db.transaction(conn):
        for key, title in (("live", "Still going"), ("done", "Shipped it"), ("dropped", "Never mind")):
            t = tasks.create_task(conn, actor=OPERATOR, project="proj", title=title, assignee=None)
            task_agents.add_agent(conn, t["id"], a.id, role="builder", stage="works", actor=OPERATOR)
            chats.send(conn, "task", str(t["id"]), f"about {title}", OPERATOR)
            made[key] = t["id"]
        chats.send(conn, "project", "proj", "hello project", OPERATOR)
        chats.send(conn, "agent", a.id, "hello worker", OPERATOR)
    move(hub, made["done"], "claimed", "in_progress", "review", "done")
    move(hub, made["dropped"], "cancelled")
    return hub, a, made


def test_chat_groups_are_projects_then_agents_then_tasks(shelf):
    hub, a, made = shelf
    assert list(views.chat_index(hub.conn())) == ["projects", "agents", "tasks", "dms"]
    for html in (get(hub, "/ui/messenger", **{"hx-request": "true"}).text, get(hub, "/ui/chats").text):
        assert groups(html)[:3] == ["Projects", "Agents", "Tasks"]
        order = chat_rows(html)
        first = {k: order.index(k) for k in ("project:proj", f"agent:{a.id}", f"task:{made['live']}")}
        assert first["project:proj"] < first[f"agent:{a.id}"] < first[f"task:{made['live']}"]


def test_direct_chats_come_after_tasks(shelf):
    hub, a, made = shelf
    hub.enroll("helper")
    say(hub, a, "dm", "helper", "psst")
    assert groups(get(hub, "/ui/chats").text) == ["Projects", "Agents", "Tasks", "Between agents"]


def test_nav_lists_projects_then_agents_then_tasks(hub):
    nav = re.search(r'<nav aria-label="Pages">(.*?)</nav>', get(hub, "/ui/dashboard").text, re.S).group(1)
    labels = re.findall(r'href="/ui/(\w+)"', nav)
    assert labels.index("projects") < labels.index("agents") < labels.index("tasks")


def test_an_archived_task_leaves_the_chat_list(shelf):
    hub, a, made = shelf
    index = views.chat_index(hub.conn())
    listed = {t["item"] for t in index["tasks"]}
    assert f"task:{made['live']}" in listed and f"task:{made['dropped']}" in listed  # cancelled isn't archived
    assert f"task:{made['done']}" not in listed
    for html in (get(hub, "/ui/messenger", **{"hx-request": "true"}).text, get(hub, "/ui/chats").text):
        assert f"task:{made['done']}" not in chat_rows(html) and "Shipped it" not in html


def test_the_tasks_page_leaves_archived_tasks_out(shelf):
    hub, a, made = shelf
    html = get(hub, "/ui/tasks").text
    assert f"/ui/tasks/{made['live']}" in html and f"/ui/tasks/{made['dropped']}" in html
    assert f"/ui/tasks/{made['done']}" not in html and "Shipped it" not in html
    assert "Done tasks are archived" in html
    html = get(hub, "/ui/tasks?state=done").text  # asking for done ones shows them
    assert f"/ui/tasks/{made['done']}" in html and "Still going" not in html


def test_at_a_glance_views_leave_archived_tasks_out(shelf):
    hub, a, made = shelf
    for path in ("/ui/dashboard", "/ui/projects", "/ui/projects/proj", "/ui/panel?item=project:proj"):
        html = get(hub, path).text
        assert "Shipped it" not in html, path
        assert "Still going" in html, path


def test_the_agent_page_and_panel_list_archived_tasks_folded(shelf):
    hub, a, made = shelf
    for path in (f"/ui/agents/{a.id}", f"/ui/panel?item=agent:{a.id}"):
        html = get(hub, path).text
        box = re.search(r'<details class="[^"]*archived-tasks">(.*?)</details>', html, re.S)
        assert box, path
        assert re.search(r'Archived tasks <span class="count">1</span>', box.group(1))
        assert f'href="/ui/tasks/{made["done"]}"' in box.group(1) and "Shipped it" in box.group(1)
        assert "Still going" not in box.group(1) and "Never mind" not in box.group(1)
        assert "archived " in box.group(1)


def test_an_agent_with_nothing_archived_says_so(hub):
    a = hub.enroll("fresh")
    html = get(hub, f"/ui/agents/{a.id}").text
    assert 'Archived tasks <span class="count">0</span>' in html and "No archived tasks" in html


def test_an_archived_task_still_opens_by_its_page(shelf):
    hub, a, made = shelf
    html = get(hub, f"/ui/tasks/{made['done']}").text
    assert "Shipped it" in html and 'class="archived-note">archived' in html
    assert "about Shipped it" in html  # its chat is still readable
    assert 'class="archived-note"' not in get(hub, f"/ui/tasks/{made['live']}").text
