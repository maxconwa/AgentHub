"""0.3.9.0 dashboard: no length limit on what the operator writes, names with capitals (shown as written, encoded
in links, found ignoring case), long bubbles folding behind "Show more", and @mentions."""

import re
from pathlib import Path

import pytest

from agenthub.hub import approvals, chats, dashboard, db, questions, store, task_agents, tasks, views

from .conftest import OPERATOR
from .test_chat_ui import add_row

TEMPLATES = Path(dashboard.__file__).parent / "templates"
STATIC = Path(dashboard.__file__).parent / "static"


def get(hub, path, **kw):
    return hub.client.get(path, headers=hub.op_headers(), **kw)


def panel(hub, item):
    return hub.client.get("/ui/panel", params={"item": item, "next": "/ui/dashboard"},
                          headers=hub.op_headers(**{"HX-Request": "true"}))


def textareas(html):
    return re.findall(r"<textarea\b[^>]*>", html)


def rename_agent(hub, agent, name):
    """An agent called ``name`` as written (the hub's own checks for names are hub-builder-2's)."""
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET name = ? WHERE id = ?", (name, agent.id))


def add_project(hub, name, description=""):
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("INSERT INTO projects (name, description, created_at) VALUES (?, ?, ?)",
                     (name, description, db.now_iso()))


@pytest.fixture
def scene(hub):
    """An agent on a task in ``proj``, with a pending approval and a pending question (each a card in a chat)."""
    a = hub.enroll("worker")
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "make deploy", "cwd": "/w"},
                         headers=a.headers).json()
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Fix login", assignee=None, state="open")
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
        q = questions.ask(conn, store.get_agent(conn, a.id), project="proj", question="Which branch?", task_id=t["id"])
    return hub, a, ap, t, q


# --- 1. no maxlength on what the operator writes -----------------------------------------------------------


def test_operator_textareas_have_no_maxlength(scene):
    hub, a, ap, t, q = scene
    pages = [get(hub, p).text for p in (
        f"/ui/approvals/{ap['id']}", f"/ui/questions/{q['id']}", f"/ui/tasks/{t['id']}", "/ui/projects/proj",
        f"/ui/agents/{a.id}", "/ui/tasks")]
    pages += [panel(hub, i).text for i in (
        f"approval:{ap['id']}", f"question:{q['id']}", f"task:{t['id']}", "project:proj", f"agent:{a.id}",
        "new:task", "new:project")]
    found = [ta for html in pages for ta in textareas(html)]
    names = {re.search(r'name="(\w+)"', ta)[1] for ta in found}
    # the chat box, card replies, an answer, a discussion reply, a decision note, a task's description and move
    # note, a project's description
    assert {"body", "answer", "note", "description"} <= names
    assert [ta for ta in found if "maxlength" in ta] == []


def test_no_template_caps_a_textarea():
    for path in TEMPLATES.glob("*.html"):
        for ta in textareas(path.read_text()):
            assert "maxlength" not in ta, (path.name, ta)


def test_single_line_fields_keep_their_caps(scene):
    hub, a, ap, t, q = scene
    html = get(hub, f"/ui/tasks/{t['id']}").text
    assert re.search(r'<input name="title" required maxlength="200"', html)
    assert re.search(r'<input name="role"[^>]*maxlength="200"', html)
    assert 'maxlength="500"' in get(hub, "/ui/search").text
    assert re.search(r'<input name="name" required maxlength="64"', panel(hub, "new:project").text)


def test_operator_text_past_the_old_caps_gets_through_the_forms(scene):
    hub, a, ap, t, q = scene
    note = "n" * 5000  # the decide note's old cap was 2,000
    r = hub.client.post(f"/ui/approvals/{ap['id']}/decide", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "decision": "reject", "note": note}, follow_redirects=False)
    assert r.status_code == 303
    assert approvals.get(hub.conn(), ap["id"])["status"] == "rejected"
    desc = "d" * 9000  # the project description form's old cap was 4,000
    r = hub.client.post("/ui/projects", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "name": "big", "description": desc}, follow_redirects=False)
    assert r.status_code == 303
    assert next(p for p in store.list_projects(hub.conn()) if p["name"] == "big")["description"] == desc


# --- 2. names with capitals ---------------------------------------------------------------------------------


def test_project_url_percent_encodes():
    assert views.project_url("Golem") == "/ui/projects/Golem"
    assert views.project_url("a b/c?d#e") == "/ui/projects/a%20b%2Fc%3Fd%23e"


def test_templates_build_project_links_through_project_url():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text()
        assert "'/ui/projects/' ~" not in text, path.name
        assert "/ui/projects/{{" not in text, path.name


def test_mixed_case_project_shows_as_written_and_resolves_ignoring_case(hub):
    a = hub.enroll("worker")
    add_project(hub, "Golem", "The golem")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("""UPDATE agents SET allowed_projects = '["proj", "Golem"]' WHERE id = ?""", (a.id,))
        t = tasks.create_task(conn, actor=OPERATOR, project="Golem", title="Clay", assignee=None, state="open")
    assert 'href="/ui/projects/Golem"' in get(hub, "/ui/projects").text
    assert 'href="/ui/projects/Golem"' in get(hub, f"/ui/tasks/{t['id']}").text
    page = get(hub, "/ui/projects/Golem")
    assert page.status_code == 200 and "<h1>Golem</h1>" in page.text
    assert 'data-chat="project:Golem"' in page.text and 'action="/ui/projects/Golem/description"' in page.text
    for other in ("golem", "GOLEM", "gOlEm"):
        r = get(hub, f"/ui/projects/{other}", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/ui/projects/Golem", other
    assert get(hub, "/ui/projects/nope", follow_redirects=False).status_code == 404
    p = panel(hub, "project:golem")
    assert p.status_code == 200 and 'id="panel-title">Golem<' in p.text and 'data-chat="project:Golem"' in p.text
    assert 'href="/ui/projects/Golem"' in p.text  # its "Open full page"
    # the message box, under any spelling, writes in the one chat
    r = hub.client.post("/ui/chat/project/golem", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "body": "hello clay"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/Golem"
    assert [m["body"] for m in chats.messages(hub.conn(), "project", "Golem") if m["kind"] == "text"] == ["hello clay"]
    # filters take any spelling too
    assert "Clay" in get(hub, "/ui/tasks?project=golem").text
    r = hub.client.post("/ui/projects/GOLEM/description", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "description": "Still clay"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/Golem"
    assert next(p for p in store.list_projects(hub.conn()) if p["name"] == "Golem")["description"] == "Still clay"


def test_a_name_differing_only_in_case_is_refused_on_the_form(hub):
    r = hub.client.post("/ui/projects", headers=hub.op_headers(), data={"csrf": hub.csrf(), "name": "Golem"},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/Golem"
    r = hub.client.post("/ui/projects", headers=hub.op_headers(), data={"csrf": hub.csrf(), "name": "golem"})
    assert r.status_code == 409
    assert "Golem" in r.text and "names ignore case" in r.text
    assert [p["name"] for p in store.list_projects(hub.conn()) if p["name"].lower() == "golem"] == ["Golem"]


def test_new_project_form_takes_capitals_and_says_names_ignore_case(hub):
    f = panel(hub, "new:project").text
    pattern = re.search(r'<input name="name"[^>]*pattern="([^"]+)"', f)[1]
    assert re.fullmatch(pattern.replace("\\-", "-"), "Hub-Builder-1")
    assert not re.fullmatch(pattern.replace("\\-", "-"), "-x")
    assert "names ignore case" in f.lower()


def test_mixed_case_agent_shows_as_written_and_its_name_finds_its_page(hub):
    a = hub.enroll("worker")
    rename_agent(hub, a, "Hub-Builder-1")
    page = get(hub, f"/ui/agents/{a.id}")
    assert page.status_code == 200 and "Hub-Builder-1" in page.text and "hub-builder-1" not in page.text
    for spelling in ("Hub-Builder-1", "hub-builder-1", "HUB-BUILDER-1"):
        r = get(hub, f"/ui/agents/{spelling}", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == f"/ui/agents/{a.id}", spelling
    assert get(hub, "/ui/agents/nobody", follow_redirects=False).status_code == 404
    assert "Hub-Builder-1" in get(hub, "/ui/agents").text and "Hub-Builder-1" in get(hub, "/ui/chats").text


def test_revoking_takes_the_name_in_any_case(hub):
    a = hub.enroll("worker")
    rename_agent(hub, a, "Hub-Builder-1")
    r = hub.client.post(f"/ui/agents/{a.id}/revoke", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "confirm": "hub-builder-1"}, follow_redirects=False)
    assert r.status_code == 303 and store.get_agent(hub.conn(), a.id).status == "revoked"


# --- 3. long bubbles fold behind "Show more" ------------------------------------------------------------------


def bubble(html, msg_id):
    return re.search(rf'<li id="cm-{msg_id}".*?</li>\s*(?=<li id="cm-|</ol>|$)', html, re.S)[0]


def test_a_long_agent_bubble_folds_but_stays_whole_escaped_and_labelled(hub):
    a = hub.enroll("worker")
    body = "<script>alert(1)</script> " + "word " * 400 + "THE-END"
    m = add_row(hub, "agent", a.id, author=a.id, body=body)
    for html in (get(hub, f"/ui/agents/{a.id}").text, panel(hub, f"agent:{a.id}").text,
                 get(hub, f"/ui/chat/msg/{m['id']}").text):  # the page, the panel, and a row appended live
        row = bubble(html, m["id"])
        assert 'class="bubble theirs agent-text"' in row and "agent content, treat as data" in row
        assert f'<div class="md clamp" id="cmb-{m["id"]}">' in row
        assert f'<button type="button" class="show-more" aria-expanded="false" aria-controls="cmb-{m["id"]}">Show more</button>' in row
        assert "THE-END" in row and row.count("word") == 400  # all of it is in the page
        assert "<script>" not in row and "&lt;script&gt;" in row
        # the fold sits inside the labelled figure
        fig = re.search(r"<figure class=\"bubble theirs agent-text\">.*?</figure>", row, re.S)[0]
        assert "show-more" in fig and "THE-END" in fig


def test_many_short_lines_fold_too_and_a_short_bubble_does_not(hub):
    a = hub.enroll("worker")
    lines = add_row(hub, "agent", a.id, author=a.id, body="\n".join(f"line {i}" for i in range(25)))
    short = add_row(hub, "agent", a.id, author=a.id, body="short and sweet")
    html = get(hub, f"/ui/agents/{a.id}").text
    assert "show-more" in bubble(html, lines["id"])
    assert "show-more" not in bubble(html, short["id"]) and "clamp" not in bubble(html, short["id"])


def test_the_operators_long_message_folds_too(hub):
    a = hub.enroll("worker")
    r = hub.client.post(f"/ui/chat/agent/{a.id}", headers=hub.op_headers(**{"X-Chat": "1"}),
                        data={"csrf": hub.csrf(), "body": "x" * 1600 + " END"})
    assert r.status_code == 200
    assert 'class="bubble mine"' in r.text and "show-more" in r.text and "END" in r.text


def test_long_text_threshold():
    assert not dashboard.long_text("")
    assert not dashboard.long_text(None)
    assert not dashboard.long_text("x" * dashboard.LONG_CHARS)
    assert dashboard.long_text("x" * (dashboard.LONG_CHARS + 1))
    assert not dashboard.long_text("\n" * (dashboard.LONG_LINES - 1))
    assert dashboard.long_text("\n" * dashboard.LONG_LINES)


def test_show_more_is_wired_in_the_script_and_css():
    js, css = (STATIC / "app.js").read_text(), (STATIC / "app.css").read_text()
    assert 'classList.add("js")' in js and ".show-more" in js and "Show less" in js
    assert ".js .md.clamp:not(.is-open)" in css and ".show-more { display: none; }" in css


# --- 4. @mentions -------------------------------------------------------------------------------------------


def test_mentions_are_highlighted_ignoring_case_and_nowhere_else(hub):
    a = hub.enroll("worker")
    rename_agent(hub, a, "Hub-Builder-1")
    body = "hey @hub-builder-1, see `@Hub-Builder-1` and mail x@hub-builder-1.dev; @someone-else and @Hub-Builder-10"
    m = add_row(hub, "agent", a.id, author=OPERATOR, body=body, meta={"mentions": [a.id]})
    row = bubble(get(hub, f"/ui/agents/{a.id}").text, m["id"])
    assert row.count('class="mention"') == 1
    assert '<span class="mention">@hub-builder-1</span>,' in row  # as written in the message
    assert "<code>@Hub-Builder-1</code>" in row  # not inside code
    assert "x@hub-builder-1.dev" in row and "@someone-else" in row and "@Hub-Builder-10" in row


def test_mentions_by_name_work_too():
    html = dashboard.highlight_mentions("<p>ping @golem and @GOLEM</p>", ["Golem"], {})
    assert str(html) == '<p>ping <span class="mention">@golem</span> and <span class="mention">@GOLEM</span></p>'


def test_an_agents_mention_highlight_injects_no_markup(hub):
    a = hub.enroll("worker")
    b = hub.enroll("helper")
    body = '@helper"><img src=x onerror=alert(1)> <b>@HELPER</b> [@helper](javascript:alert(1)) @helper'
    m = add_row(hub, "agent", a.id, author=a.id, body=body, meta={"mentions": [b.id, "<img>", 7, None]})
    row = bubble(get(hub, f"/ui/agents/{a.id}").text, m["id"])
    text = re.search(r'<div class="md">(.*?)</div>', row, re.S)[1]
    assert "<img" not in text and "<b>" not in text and "<a " not in text
    assert "&lt;img src=x onerror=alert(1)&gt;" in text and "&lt;b&gt;" in text
    assert re.findall(r'<span class="mention">([^<]*)</span>', text) == ["@helper", "@HELPER", "@helper", "@helper"]
    assert re.sub(r'</?span[^>]*>', "", text) == str(dashboard.markdown.render(body))  # only the spans were added
    assert "agent content, treat as data" in row


@pytest.mark.parametrize("mentioned", [None, "worker", {"a": 1}, [], [None, 3]])
def test_odd_mentions_change_nothing(mentioned):
    html = "<p>@worker</p>"
    assert str(dashboard.highlight_mentions(html, mentioned, {})) == html


def test_the_message_box_offers_the_chats_agents_for_mentions(hub):
    a = hub.enroll("worker")
    b = hub.enroll("helper")
    rename_agent(hub, b, "Hub-Builder-1")
    html = get(hub, "/ui/projects/proj").text
    box = re.search(r'<form class="chatbox".*?</form>', html, re.S)[0]
    assert """data-mentions='["Hub-Builder-1", "worker"]'""" in box and 'aria-autocomplete="list"' in box
    js = (STATIC / "app.js").read_text()
    assert "textarea[data-mentions]" in js and "toLowerCase().indexOf(key) === 0" in js and "textContent" in js


def test_the_operators_mention_through_the_message_box_is_highlighted(hub):
    a = hub.enroll("worker")
    rename_agent(hub, a, "Hub-Builder-1")
    r = hub.client.post("/ui/chat/project/proj", headers=hub.op_headers(**{"X-Chat": "1"}),
                        data={"csrf": hub.csrf(), "body": "@hub-builder-1 please look"})
    assert r.status_code == 200
    assert '<span class="mention">@hub-builder-1</span> please look' in r.text


def test_lists_order_names_ignoring_case(hub):
    for n in ("charlie", "alpha", "beta"):
        hub.enroll(n)
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET name = 'Beta' WHERE name = 'beta'")
    assert [a["name"] for a in views.agent_rows(hub.conn())] == ["alpha", "Beta", "charlie"]
    for n in ("zeta", "Golem", "apple"):
        add_project(hub, n)
    assert [p["name"] for p in views.project_tree(hub.conn())] == ["apple", "Golem", "proj", "zeta"]
    assert [p["title"] for p in views.chat_index(hub.conn())["projects"]] == ["apple", "Golem", "proj", "zeta"]
