"""Live regions: only pages that opt in poll, each has one region, a poll gets just that region back,
and polls never mark a private reply read unless they show it."""

import re

import pytest

from agenthub.hub import db, private, store, task_agents, tasks

from .conftest import OPERATOR

POLL = {"hx-request": "true", "hx-target": "live"}


def get(hub, path, **headers):
    return hub.client.get(path, headers=hub.op_headers(**headers))


def live_region(html):
    return re.search(r'<section id="live".*?</section>', html, re.S).group(0)


def agent_says(hub, agent, body):
    r = hub.client.post("/api/v1/private", json={"body": body}, headers=agent.headers)
    assert r.status_code in (200, 201), r.text


def unread(hub, agent):
    return private.unread_counts(hub.conn()).get(agent.id, 0)


@pytest.fixture
def site(hub):
    a = hub.enroll("worker")
    c = hub.enroll("helper")
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "git push -f origin x", "cwd": "/w"},
                         headers=a.headers).json()
    q = hub.client.post("/api/v1/projects/proj/questions", json={"question": "which?"}, headers=a.headers).json()
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Fix login", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
    hub.client.post("/api/v1/projects/proj/messages", headers=a.headers, json={"channel": "general", "body": "hi",
                                                                              "to": "helper"})
    node = store.get_agent(conn, a.id).device_node_id
    x, y = sorted([a.id, c.id])
    return hub, a, {"task": t["id"], "approval": ap["id"], "question": q["id"], "node": node, "pair": f"pair-{x}-{y}"}


def live_pages(ids, agent):
    return ["/ui/dashboard", "/ui/projects", "/ui/projects/proj", "/ui/tasks", f"/ui/tasks/{ids['task']}",
            "/ui/agents", f"/ui/agents/{agent.id}", "/ui/devices", f"/ui/devices/{ids['node']}", "/ui/approvals",
            "/ui/chats", f"/ui/chats/{agent.id}", f"/ui/chats/pair/{ids['pair']}", "/ui/board",
            "/ui/board/ch-proj~general"]


def still_pages(ids, agent):
    return ["/ui/activity", "/ui/reports", "/ui/search?q=push", f"/ui/approvals/{ids['approval']}",
            f"/ui/questions/{ids['question']}", f"/ui/compose?to=agent:{agent.id}"]


def test_only_pages_with_a_live_block_poll(site):
    hub, a, ids = site
    for path in live_pages(ids, a):
        html = get(hub, path).text
        assert html.count('id="live"') == 1, path
        assert live_region(html).count("<form") == 0, path  # a half-typed message survives the refresh
    for path in still_pages(ids, a):
        html = get(hub, path).text
        assert 'id="live"' not in html and 'hx-select="#live"' not in html, path


def test_activity_connection_indicator_is_not_a_live_region(site):
    hub = site[0]
    html = get(hub, "/ui/activity").text
    assert '<span id="ws-state" class="live" data-state="connecting">' in html
    assert "<title>All activity · agenthub</title>" in html


def test_poll_returns_only_the_live_fragment(site):
    hub, a, ids = site
    for path in live_pages(ids, a):
        r = get(hub, path, **POLL)
        assert r.status_code == 200, path
        assert r.text.lstrip().startswith('<section id="live"') and r.text.rstrip().endswith("</section>"), path
        assert "<nav" not in r.text and "<html" not in r.text and r.text.count('id="live"') == 1, path
        assert r.headers["content-security-policy"].startswith("default-src 'self'")
    # A page without a live block, or a plain HTMX request, still gets the whole page.
    assert "<nav" in get(hub, "/ui/reports", **POLL).text
    assert "<nav" in get(hub, "/ui/dashboard", **{"hx-request": "true"}).text


def test_poll_url_keeps_the_filters(site):
    hub = site[0]
    html = get(hub, "/ui/tasks?project=proj&state=open").text
    assert 'hx-get="/ui/tasks?project=proj&amp;state=open"' in live_region(html)


def test_agents_page_after_enrolling_polls_the_agents_page(site):
    hub = site[0]
    r = hub.client.post("/ui/agents/enroll", headers=hub.op_headers(), data={"csrf": hub.csrf(), "projects": "proj"})
    assert r.status_code == 200 and 'hx-get="/ui/agents"' in r.text


def test_approvals_tasks_and_agents_update_live(site):
    hub, a, ids = site
    assert f"/ui/approvals/{ids['approval']}" in live_region(get(hub, "/ui/approvals").text)
    assert f"/ui/questions/{ids['question']}" in live_region(get(hub, "/ui/approvals").text)
    assert f"/ui/tasks/{ids['task']}" in live_region(get(hub, "/ui/tasks").text)
    assert f"/ui/agents/{a.id}" in live_region(get(hub, "/ui/agents").text)
    # The forms on those pages stay outside the region.
    tasks_page = get(hub, "/ui/tasks").text
    assert 'action="/ui/tasks"' in tasks_page.split('id="live"')[0]
    assert 'action="/ui/agents/enroll"' in get(hub, "/ui/agents").text.split('id="live"')[0]


# --- reading private replies ----------------------------------------------------------------


def test_agent_page_chat_is_live_and_marks_read_only_what_it_shows(site):
    hub, a, ids = site
    agent_says(hub, a, "a reply you have not seen")
    # Another HTMX request for the page (not its live region) shows no chat: the reply stays unread.
    get(hub, f"/ui/agents/{a.id}", **{"hx-request": "true", "hx-target": "panel"})
    assert unread(hub, a) == 1
    # The live region's poll shows the chat, so the reply is read.
    r = get(hub, f"/ui/agents/{a.id}", **POLL)
    assert "a reply you have not seen" in r.text and unread(hub, a) == 0
    agent_says(hub, a, "second reply")
    page = get(hub, f"/ui/agents/{a.id}").text
    assert "second reply" in live_region(page) and unread(hub, a) == 0
    assert f'action="/ui/chats/{a.id}"' in page.split('id="live"')[1].split("</section>", 1)[1]


def test_chat_page_poll_marks_read_but_other_requests_do_not(site):
    hub, a, ids = site
    agent_says(hub, a, "hello operator")
    get(hub, f"/ui/chats/{a.id}", **{"hx-request": "true", "hx-target": "compose"})
    assert unread(hub, a) == 1
    get(hub, f"/ui/chats/{a.id}", **POLL)
    assert unread(hub, a) == 0


@pytest.mark.parametrize("path", ["/ui/agents/{id}", "/ui/chats/{id}", "/ui/panel?item=agent:{id}",
                                  "/ui/messenger/agent:{id}"])
def test_cross_site_get_never_marks_read(site, path):
    hub, a, ids = site
    agent_says(hub, a, "keep me unread")
    r = get(hub, path.format(id=a.id), **{"sec-fetch-site": "cross-site"})
    assert r.status_code == 200 and unread(hub, a) == 1
    get(hub, path.format(id=a.id))
    assert unread(hub, a) == 0


# --- whitespace-only posts -------------------------------------------------------------------


def test_whitespace_only_operator_posts_are_refused(site):
    hub, a, ids = site
    conn = hub.conn()
    count = lambda: sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
                        for t in ("messages", "private_messages", "device_messages", "approval_messages"))
    before = count()
    posts = [("/ui/projects/proj/notes", "body", {}), (f"/ui/tasks/{ids['task']}/notes", "body", {}),
             (f"/ui/devices/{ids['node']}/notes", "body", {}), (f"/ui/chats/{a.id}", "body", {}),
             ("/ui/compose", "body", {"to": f"agent:{a.id}", "recipients": a.id}),
             ("/ui/compose", "body", {"to": "project:proj", "mode": "board"}),
             ("/ui/board/ch-proj~general", "body", {}), (f"/ui/questions/{ids['question']}/answer", "answer", {}),
             (f"/ui/approvals/{ids['approval']}/discuss", "body", {})]
    for url, field, extra in posts:
        r = hub.client.post(url, headers=hub.op_headers(), data={"csrf": hub.csrf(), field: "  \n\t ", **extra},
                            follow_redirects=False)
        assert r.status_code == 422, (url, r.status_code)
    assert count() == before
    assert conn.execute("SELECT status FROM questions WHERE id = ?", (ids["question"],)).fetchone()[0] == "pending"


# --- small things ----------------------------------------------------------------------------


def test_narrow_screen_and_math_rules_in_the_stylesheet(hub):
    css = hub.client.get("/ui/static/app.css").text
    assert re.search(r"\.inline \{[^}]*flex-wrap: wrap", css)  # the task page's member forms wrap at 360px
    assert re.search(r"^select \{ max-width: 100%; \}", css, re.M)
    assert re.search(r"\.md \.math\.inline \.katex \{ display: inline-block; max-width: 100%; overflow-x: auto;", css)
    # A list row's subtitle is nowrap; a long task title in the row's own spans must still wrap.
    assert ".chat-list .t > span { display: block;" in css and ".chat-list .t span " not in css
    assert re.search(r"^\.member-card \{", css, re.M)


def test_task_page_member_blocks_stack(site):
    hub, a, ids = site
    html = get(hub, f"/ui/tasks/{ids['task']}").text
    agents = html.split("<h3>Agents</h3>")[1].split("</section>")[0]
    assert '<div class="member-card">' in agents and '<div class="member">' not in agents


def test_new_project_form_says_how_agents_get_in(hub):
    html = hub.client.get("/ui/panel?item=new:project", headers=hub.op_headers()).text
    assert "through its tasks" not in html and "allowed projects" in html


# --- htmx attribute inheritance --------------------------------------------------------------

INHERITED = ("hx-select", "hx-target", "hx-swap")  # what htmx would pass from the live section to its children
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def htmx_controls_in_live(html):
    """Each element inside #live that issues an htmx request, with the hx-select/target/swap it would resolve
    (its own value, else the nearest ancestor's, stopping at an ancestor whose hx-disinherit names it)."""
    from html.parser import HTMLParser

    found = []

    class P(HTMLParser):
        stack: list[dict] = []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            in_live = any(e.get("id") == "live" for e in self.stack)
            if in_live and ("hx-get" in a or "hx-post" in a):
                resolved = {}
                for name in INHERITED:
                    value = a.get(name)
                    if value is None:
                        for anc in reversed(self.stack):
                            dis = (anc.get("hx-disinherit") or "").split()
                            if "*" in dis or name in dis:
                                break
                            if name in anc:
                                value = anc[name]
                                break
                    resolved[name] = value
                found.append((tag, a.get("hx-get") or a.get("hx-post"), resolved))
            if tag not in VOID:
                self.stack.append(a)

        def handle_endtag(self, tag):
            if tag not in VOID and self.stack:
                self.stack.pop()

    P().feed(html)
    return found


def test_live_region_does_not_leak_its_htmx_attributes(site):
    hub, a, ids = site
    seen = 0
    for path in live_pages(ids, a):
        html = get(hub, path).text
        section = re.search(r'<section id="live"[^>]*>', html).group(0)
        assert 'hx-disinherit="hx-select hx-target hx-swap"' in section, path
        assert "hx-headers" not in section  # the CSRF header still comes down from <body>
        for tag, url, resolved in htmx_controls_in_live(html):
            seen += 1
            assert resolved["hx-select"] is None, (path, url)  # a Message button gets the whole compose sheet
            assert resolved["hx-target"] != "#live" and resolved["hx-swap"] != "outerHTML", (path, url)
    assert seen >= 6  # Message buttons on the pages above
