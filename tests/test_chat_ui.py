"""0.3.8's chat UI: the agent, task and project panels and pages are chats (bubbles, status lines, tool folds,
approval and question cards, one message box), kept live by app.js over the operator socket."""

import re

import pytest

from agenthub.hub import approvals, chats, db, questions, task_agents, tasks

from .conftest import OPERATOR


def get(hub, path, **headers):
    return hub.client.get(path, headers=hub.op_headers(**headers), follow_redirects=False)


def post(hub, path, data, chat=True, csrf=True):
    headers = hub.op_headers(**({"x-chat": "1"} if chat else {}))
    body = dict(data) | ({"csrf": hub.csrf()} if csrf else {})
    return hub.client.post(path, data=body, headers=headers, follow_redirects=False)


def add_row(hub, kind, ref, *, author, type="text", body="", meta=None):
    """A chat row of any kind and shape, as the hub stores it (from a runner's stream, a card, a took_task)."""
    conn = hub.conn()
    with db.transaction(conn):
        mid = chats._insert(conn, chats.chat(conn, kind, str(ref)), author=author, kind=type, body=body,
                            meta=meta or {})
    return chats.get_message(hub.conn(), mid)


def update_row(hub, message_id, *, meta):
    """The row changed in place, as a growing tool stretch does."""
    conn = hub.conn()
    m = chats.get_message(conn, message_id)
    with db.transaction(conn):
        chats._update_meta(conn, chats.chat(conn, m["chat_kind"], m["chat_ref"]), message_id, meta, actor=m["author"],
                           kind=m["kind"])
    return chats.get_message(hub.conn(), message_id)


def card_row(hub, kind, item_id):
    """The card the hub put in a chat for an approval or a question."""
    m = chats.card(hub.conn(), kind, item_id)
    assert m, f"no card for {kind} {item_id}"
    return m


def row_html(html, message_id):
    m = re.search(rf'<li id="cm-{message_id}"[^>]*>.*?</li>\s*(?=<li id="cm-|</ol>|$)', html, re.S)
    assert m, f"no row {message_id}"
    return m.group(0)


@pytest.fixture
def team(hub):
    a = hub.enroll("worker")
    b = hub.enroll("helper")
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Fix login", assignee=None)
        task_agents.add_agent(conn, t["id"], a.id, role="tester", stage="works", actor=OPERATOR)
        task_agents.add_agent(conn, t["id"], b.id, role="", stage="reviews", actor=OPERATOR)
    return hub, a, b, t


# --- panels and pages are chats, with one message box ----------------------------------------------------------

@pytest.mark.parametrize("which", ["agent", "task", "project"])
def test_panel_and_page_are_one_chat_with_one_message_box(team, which):
    hub, a, _, t = team
    ref = {"agent": a.id, "task": str(t["id"]), "project": "proj"}[which]
    page = {"agent": f"/ui/agents/{a.id}", "task": f"/ui/tasks/{t['id']}", "project": "/ui/projects/proj"}[which]
    for html in (get(hub, f"/ui/panel?item={which}:{ref}").text, get(hub, page).text):
        assert html.count('class="chat"') == 1
        assert f'data-chat="{which}:{ref}"' in html
        assert html.count("data-chat-send") == 1
        assert f'action="/ui/chat/{which}/{ref}"' in html
        assert "/notes" not in html and "/ui/prompts" not in html  # no other composer: no Prompt, Post or board note
        assert ">Prompt<" not in html and ">Post" not in html and not re.search(r"\bboard\b", html, re.I)
    assert get(hub, f"/ui/panel?item={which}:{ref}").text.count("<textarea") == 1


def test_hints_say_who_a_message_reaches(team):
    hub, a, b, t = team
    assert "Wakes worker if it&#39;s idle" in get(hub, f"/ui/panel?item=agent:{a.id}").text
    assert f"Goes to everyone on task #{t['id']}: helper and worker." in get(hub, f"/ui/panel?item=task:{t['id']}").text
    assert "Goes to every agent in proj: helper and worker." in get(hub, "/ui/panel?item=project:proj").text


def test_a_runner_too_old_for_chat_is_named(team):
    """A connected agent whose runner doesn't say "chat": 1 (0.3.7's) won't get a message until it's upgraded."""
    hub, a, b, t = team
    conn = hub.conn()

    def runner(agent, connected, chat):
        with db.transaction(conn):
            conn.execute("UPDATE agents SET connected = ?, chat_support = ? WHERE id = ?", (connected, chat, agent.id))

    runner(a, 1, 0)
    runner(b, 1, 1)
    html = get(hub, f"/ui/panel?item=task:{t['id']}").text
    assert '<p class="chat-hint small-line too-old">worker&#39;s runner is too old for chat (before 0.3.8): upgrade it.' in html
    assert "too old" in get(hub, f"/ui/panel?item=agent:{a.id}").text
    assert "too old" not in get(hub, f"/ui/panel?item=agent:{b.id}").text
    runner(b, 1, 0)
    assert "The runners of helper and worker are too old for chat" in get(hub, "/ui/panel?item=project:proj").text
    runner(a, 0, 0)  # offline: nothing to say yet (the message waits as for any offline agent)
    runner(b, 1, 1)
    assert "too old" not in get(hub, f"/ui/panel?item=task:{t['id']}").text


def test_device_panel_has_no_chat(team):
    hub, a, _, _ = team
    html = get(hub, f"/ui/panel?item=device:n{a.name}").text
    assert 'class="chat"' not in html and "<textarea" not in html
    assert "Devices have no chat" in html


def test_revoked_agent_and_finished_task_chats_are_read_only(team):
    hub, a, _, t = team
    conn = hub.conn()
    with db.transaction(conn):
        tasks.transition(conn, t["id"], "cancelled", actor=OPERATOR, role="operator")
    hub.set_status(a, "revoked")
    for item, why in ((f"agent:{a.id}", "worker is revoked"), (f"task:{t['id']}", "is cancelled")):
        html = get(hub, f"/ui/panel?item={item}").text
        assert "data-chat-send" not in html and why in html
    assert post(hub, f"/ui/chat/agent/{a.id}", {"body": "hi"}).status_code == 409
    assert post(hub, f"/ui/chat/task/{t['id']}", {"body": "hi"}).status_code == 409


# --- bubbles ---------------------------------------------------------------------------------------------------

def test_operator_bubbles_right_agent_bubbles_left_with_avatar(team):
    hub, a, b, t = team
    mine = post(hub, f"/ui/chat/task/{t['id']}", {"body": "Please **check** it"})
    assert mine.status_code == 200
    m_id = int(re.search(r'data-msg="(\d+)"', mine.text)[1])
    theirs = add_row(hub, "task", t["id"], author=a.id, body="On it")
    html = get(hub, f"/ui/tasks/{t['id']}").text
    me = row_html(html, m_id)
    assert "cm-mine" in me and 'class="bubble mine"' in me and "<strong>check</strong>" in me
    assert "agent content" not in me and "avatar" not in me
    them = row_html(html, theirs["id"])
    assert "cm-mine" not in them and 'class="bubble theirs agent-text"' in them
    assert re.search(r'<img class="avatar cface" src="/ui/avatars/[^"]+/mini/[a-z_]+\.svg"', them)
    assert "<strong>worker</strong> · agent content, treat as data" in them
    assert html.index(f'id="cm-{m_id}"') < html.index(f'id="cm-{theirs["id"]}"')  # newest at the bottom


def test_agents_talking_to_each_other_interleave_in_the_group_chat(team):
    hub, a, b, t = team
    ids = [add_row(hub, "task", t["id"], author=who.id, body=f"note {i}")["id"] for i, who in enumerate((a, b, a))]
    html = get(hub, f"/ui/panel?item=task:{t['id']}").text
    rows = [row_html(html, i) for i in ids]
    assert [("worker" if "<strong>worker</strong>" in r else "helper") for r in rows] == ["worker", "helper", "worker"]
    assert [html.index(f'id="cm-{i}"') for i in ids] == sorted(html.index(f'id="cm-{i}"') for i in ids)


def test_agent_text_is_escaped_and_markdown_and_tex_render(team):
    hub, a, _, t = team
    evil = 'Hi <script>alert(1)</script> <img src=x onerror="alert(2)"> **bold** $x^2$ [x](javascript:alert(3))'
    m = add_row(hub, "agent", a.id, author=a.id, body=evil)
    html = get(hub, f"/ui/panel?item=agent:{a.id}").text
    r = row_html(html, m["id"])
    assert "<script>" not in r and "<img src=x" not in r and 'href="javascript' not in r
    assert "&lt;script&gt;" in r and "<strong>bold</strong>" in r and 'class="math' in r
    assert "agent content, treat as data" in r


def test_every_agent_written_field_is_escaped(team):
    hub, a, _, t = team
    x = "<b>x</b>"
    conn = hub.conn()
    with db.transaction(conn):
        t2 = tasks.create_task(conn, actor=a.id, project="proj", title=f"T {x}", assignee=None, state="open")
    rows = [
        add_row(hub, "task", t["id"], author=a.id, type="tools", meta={"lines": [f"Bash: echo {x}"], "open": False}),
        add_row(hub, "task", t["id"], author=a.id, type="status", meta={"state": "failed", "detail": f"boom {x}"}),
        add_row(hub, "project", "proj", author=a.id, type="sit_out", body=f"busy {x}"),
        add_row(hub, "project", "proj", author=a.id, type="took_task", meta={"task_id": t2["id"]}),
    ]
    for r in rows:
        html = get(hub, f"/ui/chat/msg/{r['id']}").text
        assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_addressed_and_backfilled_rows_say_so(team):
    hub, a, b, t = team
    to = add_row(hub, "project", "proj", author=a.id, body="over to you", meta={"to": b.id})
    old = add_row(hub, "project", "proj", author=a.id, body="from 0.3.7",
                  meta={"legacy": {"from": "channel", "channel": "coordination-1", "id": 7}})
    hub_row = add_row(hub, "project", "proj", author="system:hub", body="[coordination #1] <i>x</i>", meta={"to": a.id})
    assert "<strong>worker</strong> → helper · agent content" in get(hub, f"/ui/chat/msg/{to['id']}").text
    assert "in #coordination-1 · agent content" in get(hub, f"/ui/chat/msg/{old['id']}").text
    html = get(hub, f"/ui/chat/msg/{hub_row['id']}").text
    assert '<span class="tag">hub</span> → worker [coordination #1] &lt;i&gt;x&lt;/i&gt;' in html
    st = add_row(hub, "task", t["id"], author=a.id, type="status", meta={"state": "failed", "detail": "no checkout",
                                                                         "detail_by_hub": True})
    assert '<span class="tag">hub note</span> <span class="sline-note">no checkout</span>' in get(
        hub, f"/ui/chat/msg/{st['id']}").text


# --- status lines ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state, text", [
    ("started", "worker started on task #"), ("working", "worker is working"),
    ("usage_limit", "worker is waiting for the usage limit to reset"),
    ("approval_wait", "worker is waiting for your approval"), ("finished", "worker finished"),
    ("failed", "worker failed"), ("queued", "worker gets your message at its next turn"),
])
def test_status_lines(team, state, text):
    hub, a, _, t = team
    r = add_row(hub, "task", t["id"], author=a.id, type="status", meta={"state": state, "task_id": t["id"]})
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert 'class="cm cm-status"' in html and f'<p class="sline st-{state}' in html and text in html
    assert "bubble" not in html


def test_failed_status_line_uses_the_failed_look(team):
    hub, a, _, t = team
    r = add_row(hub, "task", t["id"], author=a.id, type="status", meta={"state": "failed"})
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert "is-failed" in html and f'data-fail-key="chat:{r["id"]}" data-failed' in html
    ok = add_row(hub, "task", t["id"], author=a.id, type="status", meta={"state": "finished"})
    assert "data-failed" not in get(hub, f"/ui/chat/msg/{ok['id']}").text


def test_queued_line_names_the_agent_not_the_hub(team):
    hub, a, _, _ = team
    r = add_row(hub, "agent", a.id, author="system:hub", type="status", meta={"state": "queued", "agent_id": a.id})
    assert "worker gets your message at its next turn" in get(hub, f"/ui/chat/msg/{r['id']}").text


def test_took_task_and_sit_out_lines(team):
    hub, a, b, _ = team
    conn = hub.conn()
    with db.transaction(conn):
        t2 = tasks.create_task(conn, actor=a.id, project="proj", title="Split part", assignee=None, state="open")
    took = add_row(hub, "project", "proj", author=a.id, type="took_task", meta={"task_id": t2["id"], "request_id": 1})
    sat = add_row(hub, "project", "proj", author=b.id, type="sit_out", body="nothing for a reviewer yet")
    html = get(hub, "/ui/panel?item=project:proj").text
    took_row = row_html(html, took["id"])
    assert f"worker took <a href=\"/ui/tasks/{t2['id']}\" data-panel=\"task:{t2['id']}\">task #{t2['id']}</a>" in took_row
    assert "Split part" in took_row
    assert "helper is sitting this one out" in row_html(html, sat["id"])
    assert "agent text" in row_html(html, sat["id"])


# --- tool folds ----------------------------------------------------------------------------------------------------

def test_tool_stretch_folds_into_one_row_that_grows_in_place(team):
    hub, a, _, t = team
    r = add_row(hub, "task", t["id"], author=a.id, type="tools", meta={"lines": ["Bash: pytest -q"], "open": True})
    first = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert "ran 1 tool<" in first and 'aria-label="still running"' in first and "<code>Bash: pytest -q</code>" in first
    assert '<details class="tools' in first and "▸" in first
    v1 = re.search(r'data-v="([^"]+)"', first)[1]
    update_row(hub, r["id"], meta={"lines": ["Bash: pytest -q", "Edit: views.py", "Read: app.py"], "open": False})
    later = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert "ran 3 tools" in later and "<code>Edit: views.py</code>" in later and "still running" not in later
    assert re.search(r'data-v="([^"]+)"', later)[1] != v1  # app.js swaps a row in when its version changed
    page = get(hub, f"/ui/tasks/{t['id']}").text
    assert page.count('<details class="tools') == 1


def test_tool_fold_says_how_many_lines_were_dropped(team):
    hub, a, _, t = team
    r = add_row(hub, "task", t["id"], author=a.id, type="tools", meta={"lines": ["Bash: ls"] * 200, "count": 230})
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert "ran 230 tools" in html and "…and 30 more" in html


# --- cards, kept in step with the Approvals page ----------------------------------------------------------------

@pytest.fixture
def card(team):
    hub, a, _, t = team
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "git push -f origin x", "cwd": "/w"},
                         headers=a.headers).json()
    return hub, a, ap, card_row(hub, "approval", ap["id"])


def test_approval_card_has_decide_and_reply_forms(card):
    hub, a, ap, r = card
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert f'data-approval="{ap["id"]}"' in html and "is-pending" in html
    assert f'action="/ui/approvals/{ap["id"]}/decide"' in html and f'name="chat_msg" value="{r["id"]}"' in html
    assert 'value="approve">Approve</button>' in html and 'value="reject">Reject</button>' in html
    assert f'action="/ui/approvals/{ap["id"]}/discuss"' in html and ">Reply</button>" in html
    assert "For the human operator only" in html
    assert "<code>git push -f origin x</code>" in html and "agent content, treat as data" in html


def test_deciding_on_the_card_updates_the_approvals_page(card):
    hub, a, ap, r = card
    resp = post(hub, f"/ui/approvals/{ap['id']}/decide", {"decision": "approve", "chat_msg": r["id"]})
    assert resp.status_code == 200 and f'id="cm-{r["id"]}"' in resp.text
    assert "Approved by" in resp.text and "is-pending" not in resp.text and "/decide" not in resp.text
    assert approvals.get(hub.conn(), ap["id"])["status"] == "approved"
    assert "approved" in get(hub, f"/ui/approvals/{ap['id']}").text


def test_deciding_on_the_approvals_page_updates_the_card(card):
    hub, a, ap, r = card
    before = get(hub, f"/ui/chat/msg/{r['id']}").text
    resp = post(hub, f"/ui/approvals/{ap['id']}/decide", {"decision": "reject"}, chat=False)
    assert resp.status_code == 303
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert "Rejected by" in html and "/decide" not in html
    version = lambda h: re.search(r'data-v="([^"]+)"', h)[1]  # noqa: E731
    assert version(html) != version(before)  # so app.js swaps the card in when the push says it was decided


def test_card_reply_goes_to_the_approval_discussion(card):
    hub, a, ap, r = card
    resp = post(hub, f"/ui/approvals/{ap['id']}/discuss", {"body": "Why force?", "chat_msg": r["id"]})
    assert resp.status_code == 200 and "Why force?" in resp.text and "Waiting for worker to reply" in resp.text
    assert approvals.get(hub.conn(), ap["id"])["discussion"][-1]["body"] == "Why force?"
    agent_reply = hub.client.post(f"/api/v1/approvals/{ap['id']}/discussion", json={"body": "<i>because</i>"},
                                  headers=a.headers)
    assert agent_reply.status_code in (200, 201), agent_reply.text
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert "&lt;i&gt;because&lt;/i&gt;" in html and "worker · agent content, treat as data" in html


@pytest.fixture
def qcard(team):
    hub, a, _, t = team
    q = hub.client.post("/api/v1/projects/proj/questions", json={"question": "Which *branch*?"}, headers=a.headers)
    assert q.status_code in (200, 201), q.text
    qid = q.json()["id"]
    return hub, a, qid, card_row(hub, "question", qid)


def test_question_card_answer_and_reply(qcard):
    hub, a, qid, r = qcard
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert f'action="/ui/questions/{qid}/answer"' in html and "<em>branch</em>" in html
    assert f'name="reply_to" value="{r["id"]}"' in html
    resp = post(hub, f"/ui/questions/{qid}/answer", {"answer": "main", "chat_msg": r["id"]})
    assert resp.status_code == 200 and "Your answer" in resp.text and f"/questions/{qid}/answer\"" not in resp.text
    assert questions.get(hub.conn(), qid)["status"] == "answered"
    assert "answered" in get(hub, f"/ui/questions/{qid}").text


def test_answering_on_the_question_page_updates_the_card(qcard):
    hub, a, qid, r = qcard
    assert post(hub, f"/ui/questions/{qid}/answer", {"answer": "develop"}, chat=False).status_code == 303
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    assert "develop" in html and f"/questions/{qid}/answer" not in html


@pytest.mark.parametrize("on_task", [False, True])
def test_question_reply_without_answering_lands_next_to_the_card(team, on_task):
    """The card's Reply posts into the chat the card is in: the asker's own chat, or the task's chat for a question
    about a task (where hub-reviewer found it 404ing when it posted to the agent chat instead)."""
    hub, a, _, t = team
    ask = {"question": "Which *branch*?"} | ({"task_id": t["id"]} if on_task else {})
    qid = hub.client.post("/api/v1/projects/proj/questions", json=ask, headers=a.headers).json()["id"]
    r = card_row(hub, "question", qid)
    where = f"task:{t['id']}" if on_task else f"agent:{a.id}"
    assert f"{r['chat_kind']}:{r['chat_ref']}" == where
    html = get(hub, f"/ui/chat/msg/{r['id']}").text
    action = re.findall(r'<form class="card-reply" method="post" action="([^"]+)"', html)[-1]
    assert action == "/ui/chat/" + where.replace(":", "/") and f'name="reply_to" value="{r["id"]}"' in html
    resp = post(hub, action, {"body": "Which repo do you mean?", "reply_to": r["id"]})
    assert resp.status_code == 200, resp.text
    assert f'data-in="{where}"' in resp.text and f"↩ about question #{qid}" in resp.text
    assert questions.get(hub.conn(), qid)["status"] == "pending"  # replying isn't answering
    resp = post(hub, action, {"body": "plain form", "reply_to": r["id"]}, chat=False)  # without JavaScript
    assert resp.status_code == 303


# --- sending, catching up, the chat list ------------------------------------------------------------------------

def test_send_needs_csrf_and_text(team):
    hub, a, _, _ = team
    assert post(hub, f"/ui/chat/agent/{a.id}", {"body": "hi"}, csrf=False).status_code == 403
    assert post(hub, f"/ui/chat/agent/{a.id}", {"body": "   "}).status_code == 422
    assert post(hub, "/ui/chat/agent/agnope", {"body": "hi"}).status_code == 404
    assert post(hub, "/ui/chat/device/x", {"body": "hi"}).status_code == 404


def test_send_without_javascript_comes_back_to_the_page(team):
    hub, a, _, t = team
    resp = post(hub, f"/ui/chat/task/{t['id']}", {"body": "plain form", "next": f"/ui/tasks/{t['id']}"}, chat=False)
    assert resp.status_code == 303 and resp.headers["location"] == f"/ui/tasks/{t['id']}"
    resp = post(hub, f"/ui/chat/task/{t['id']}", {"body": "no next"}, chat=False)
    assert resp.headers["location"] == f"/ui/tasks/{t['id']}"
    resp = post(hub, f"/ui/chat/task/{t['id']}", {"body": "x", "next": "https://evil.example/"}, chat=False)
    assert resp.headers["location"] == f"/ui/tasks/{t['id']}"
    assert "plain form" in get(hub, f"/ui/tasks/{t['id']}").text


def test_sent_message_shows_who_got_it(team):
    hub, a, b, t = team
    html = post(hub, f"/ui/chat/task/{t['id']}", {"body": "hello"}).text
    assert 'aria-label="Who got it"' in html and "worker: not picked up yet" in html and "helper: not picked up yet" in html


def test_catch_up_returns_only_newer_rows(team):
    hub, a, _, _ = team
    first = add_row(hub, "agent", a.id, author=a.id, body="one")
    second = add_row(hub, "agent", a.id, author=a.id, body="two")
    html = get(hub, f"/ui/chat/agent/{a.id}?after={first['id']}", **{"x-chat": "1"}).text
    assert f'id="cm-{second["id"]}"' in html and f'id="cm-{first["id"]}"' not in html
    assert get(hub, f"/ui/chat/agent/{a.id}").headers["location"] == f"/ui/agents/{a.id}"
    assert get(hub, "/ui/chat/msg/999999").status_code == 404


def test_rows_say_which_chat_they_belong_to(team):
    hub, a, _, t = team
    r = add_row(hub, "task", t["id"], author=a.id, body="x")
    assert f'data-in="task:{t["id"]}"' in get(hub, f"/ui/chat/msg/{r['id']}").text


def test_chat_list_lists_agents_tasks_and_projects_as_panels(team):
    hub, a, _, t = team
    add_row(hub, "agent", a.id, author=a.id, body="<u>done</u> with it")
    for path in ("/ui/chats", "/ui/messenger"):
        html = get(hub, path).text
        for item in (f"agent:{a.id}", f"task:{t['id']}", "project:proj"):
            assert f'data-panel="{item}"' in html
        assert '<span class="tag">agent text</span> worker: &lt;u&gt;done&lt;/u&gt; with it' in html
        assert 'class="unread"' in html
        assert "Device boards" not in html and "Between agents" not in html


def test_viewing_a_chat_marks_it_read(team):
    hub, a, _, _ = team
    add_row(hub, "agent", a.id, author=a.id, body="news")
    unread = lambda: {(o["kind"], o["ref"]): o["unread"] for o in chats.overview(hub.conn())}[("agent", a.id)]  # noqa: E731
    before = int(re.search(r">(\d+)<", get(hub, "/ui/badge/chats").text)[1])
    assert unread() >= 1
    get(hub, f"/ui/panel?item=agent:{a.id}")
    assert unread() == 0
    after = re.search(r">(\d+)<", get(hub, "/ui/badge/chats").text)
    assert (int(after[1]) if after else 0) == before - 2  # its enrollment card and "news"


def test_chat_fragments_have_no_inline_style_or_script(card):
    hub, a, ap, r = card
    add_row(hub, "agent", a.id, author=a.id, type="tools", meta={"lines": ["Bash: ls"], "open": True})
    for html in (get(hub, f"/ui/panel?item=agent:{a.id}").text, get(hub, f"/ui/agents/{a.id}").text):
        assert not re.search(r"\sstyle=", html) and not re.search(r"<script(?![^>]*\ssrc=)", html)
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", html)


# --- what's gone ----------------------------------------------------------------------------------------------------

def test_prompt_board_and_send_are_gone(team):
    hub, a, _, t = team
    assert ">Board<" not in get(hub, "/ui/dashboard").text
    for path in ("/ui/dashboard", f"/ui/agents/{a.id}", f"/ui/tasks/{t['id']}", "/ui/projects/proj", "/ui/chats"):
        html = get(hub, path).text
        assert "/ui/prompts" not in html and "/ui/compose" not in html and "Send to each agent" not in html
        assert ">Prompt<" not in html and ">Send<" not in html and "Prompts</h2>" not in html
    for path in ("/ui/prompts/1", "/ui/compose", "/ui/messenger/agent:x"):
        assert get(hub, path).status_code == 404
    assert post(hub, "/ui/prompts", {"target": f"agent:{a.id}", "body": "x"}, chat=False).status_code in (404, 405)
    assert get(hub, "/ui/board").headers["location"] == "/ui/chats"
    assert get(hub, "/ui/board/ch-proj~general").headers["location"] == "/ui/chats"
    assert get(hub, f"/ui/chats/{a.id}").headers["location"] == f"/ui/agents/{a.id}"


# --- layout ---------------------------------------------------------------------------------------------------------

def test_layout_rules_for_a_phone():
    """What keeps the chat usable at 360px: the panel stays inside the screen, the log scrolls and the box stays
    put, nothing wide (code, tool lines, long words) pushes the bubbles out."""
    from pathlib import Path

    from agenthub.hub import dashboard

    css = (Path(dashboard.__file__).parent / "static" / "app.css").read_text()
    assert "width: min(600px, 94vw)" in css
    assert ".drawer:has(> .chat) { display: flex; flex-direction: column; overflow: hidden;" in css
    assert ".drawer > .chat .chat-log { flex: 1 1 auto; min-height: 0; }" in css
    assert re.search(r"\.bubble \{ max-width: min\(85%, 640px\); min-width: 0;[^}]*overflow-wrap: anywhere", css)
    assert ".tool-lines code { font-size: 0.8rem; overflow-wrap: anywhere;" in css
    assert ".bubble .md pre { max-width: 100%; overflow-x: auto; }" in css
    assert re.search(r"@media \(max-width: 420px\) \{\s*\.bubble, details\.tools \{ max-width: 88%; \}", css)
    assert ".send-btn { width: 44px; height: 44px;" in css  # a full-size tap target
