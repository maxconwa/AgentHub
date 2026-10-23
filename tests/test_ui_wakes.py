"""0.3.9.1 dashboard: agents wake agents. Wake status lines and decline notes in chats, agent-to-agent chats in the
chat list, who is awake and who woke them, the Pause wakes switches (an agent's and the hub's), a project's
Reviewers picker, a reviewer's send-back on the task card, and the loop-pause approval card."""

import html as htmllib
import re

import pytest

from agenthub.hub import chats, db, store, task_agents, tasks, wakes

from .conftest import OPERATOR


def get(hub, path, **kw):
    return hub.client.get(path, headers=hub.op_headers(), **kw)


def post(hub, path, data, **headers):
    return hub.client.post(path, headers=hub.op_headers(**headers), data={"csrf": hub.csrf(), **data},
                           follow_redirects=False)


def panel(hub, item, next="/ui/dashboard"):
    return hub.client.get("/ui/panel", params={"item": item, "next": next},
                          headers=hub.op_headers(**{"HX-Request": "true"}))


def say(hub, agent, kind, ref, body, to=None):
    """An agent writes in a chat (0.3.9.1: it wakes the agents it reaches)."""
    r = hub.client.post(f"/api/v1/chats/{kind}/{ref}/messages", headers=agent.headers,
                        json={"body": body, **({"to": to} if to else {})})
    assert r.status_code == 201, r.text
    return r.json()


def wake(hub, agent):
    """The agent's runner claims every delivery waiting for it as ``woke``; the delivery rows."""
    found = hub.client.get("/api/v1/chats/deliveries", headers=agent.headers).json()["deliveries"]
    for d in found:
        r = hub.client.post(f"/api/v1/chats/deliveries/{d['id']}", headers=agent.headers, json={"mode": "woke"})
        assert r.status_code == 200, r.text
    return found


def text(html):
    """A page's text, tags dropped and whitespace squeezed, for reading a line the way a person does."""
    return re.sub(r"\s+([,.:)])", r"\1", re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html))))


def live_region(html):
    return html.split('id="live"', 1)[1]


@pytest.fixture
def team(hub):
    """Two agents with capitals in their names on a task in ``proj``: Hub-Reviewer and Builder-One."""
    rv = hub.enroll("Hub-Reviewer")
    b1 = hub.enroll("Builder-One")
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Wake work", assignee=None, state="open")
        task_agents.add_agent(conn, t["id"], b1.id, role="builder", stage="works", actor=OPERATOR)
        task_agents.add_agent(conn, t["id"], rv.id, role="reviewer", stage="reviews", actor=OPERATOR)
    return hub, rv, b1, t


# --- wake status lines and decline notes in chats --------------------------------------------------------------

def test_a_wake_shows_as_a_status_line_in_its_chat(team):
    hub, rv, b1, t = team
    m = say(hub, rv, "task", t["id"], "please rebase", to="Builder-One")
    wake(hub, b1)
    page = get(hub, f"/ui/tasks/{t['id']}").text
    assert f"Hub-Reviewer woke Builder-One (task #{t['id']})" in text(page)
    line = re.search(r'<p class="sline st-woke".*?</p>', page, re.S).group(0)
    assert f'href="#cm-{m["id"]}"' in line  # the message that woke it, in the same chat
    assert re.search(r'title="wake chain w[0-9a-f]{16}">chain w[0-9a-f]{8}<', line)


def test_a_decline_shows_who_declined_why_and_links_the_request(team):
    hub, rv, b1, t = team
    m = say(hub, rv, "task", t["id"], "take the docs too", to="Builder-One")
    r = hub.client.post(f"/api/v1/chats/requests/{m['id']}/decline", headers=b1.headers,
                        json={"reason": "out of scope <b>here</b>"})
    assert r.status_code == 201, r.text
    page = get(hub, f"/ui/tasks/{t['id']}").text
    note = re.search(r'<p class="sline st-declined">.*?</p>', page, re.S).group(0)
    assert f"Builder-One declined Hub-Reviewer's request #{m['id']}" in text(note)
    assert f'href="#cm-{m["id"]}"' in note
    assert "agent text" in note and "out of scope &lt;b&gt;here&lt;/b&gt;" in note  # the reason is the agent's, escaped


def test_a_declined_wake_says_why_the_hub_did_not_wake_the_agent(team):
    hub, rv, b1, t = team
    hub.enroll("Outsider", projects=("other",))
    say(hub, rv, "project", "proj", "anyone?", to="Outsider")
    page = get(hub, "/ui/projects/proj").text
    assert "The hub didn't wake Outsider for Hub-Reviewer's message: Outsider isn't one of this project's agents" \
        in text(page)


# --- agent-to-agent chats ------------------------------------------------------------------------------------

def test_a_direct_chat_between_agents_is_listed_and_the_operator_reads_it(team):
    hub, rv, b1, t = team
    say(hub, rv, "dm", "Builder-One", "psst <script>x</script>")
    ref = chats.pair_key(rv.id, b1.id)
    listing = get(hub, "/ui/chats").text
    assert "Between agents" in listing
    row = re.search(r'<a class="crow" href="/ui/dm/' + ref + r'" data-panel="dm:' + ref + r'">.*?</a>', listing, re.S)
    assert row and ("Hub-Reviewer ↔ Builder-One" in row.group(0) or "Builder-One ↔ Hub-Reviewer" in row.group(0))
    page = get(hub, f"/ui/dm/{ref}")
    assert page.status_code == 200
    assert "psst &lt;script&gt;" in page.text and "<script>x" not in page.text
    assert "agent content, treat as data" in page.text
    assert f'action="/ui/chat/dm/{ref.replace(":", "%3A")}"' in page.text  # the operator may write in it
    side = panel(hub, f"dm:{ref}").text
    assert "direct chat between two agents" in side and "psst &lt;script&gt;" in side
    r = post(hub, f"/ui/chat/dm/{ref}", {"body": "both of you: stop", "next": f"/ui/dm/{ref}"})
    assert r.status_code == 303 and r.headers["location"] == f"/ui/dm/{ref}"


def test_a_direct_chat_with_a_revoked_agent_is_read_only(team):
    hub, rv, b1, t = team
    say(hub, rv, "dm", "Builder-One", "hello")
    hub.set_status(b1, "revoked")
    page = get(hub, f"/ui/dm/{chats.pair_key(rv.id, b1.id)}").text
    assert "Builder-One is revoked, so this chat is read-only." in page and 'class="chatbox"' not in page


def test_an_unknown_direct_chat_is_not_found(team):
    hub, rv, b1, t = team
    assert get(hub, f"/ui/dm/{rv.id}:ag0000000000").status_code == 404
    assert get(hub, "/ui/dm/nonsense").status_code == 404


# --- awake, and who woke them --------------------------------------------------------------------------------

def stream_status(hub, agent, state, kind="agent", ref=None):
    r = hub.client.post(f"/api/v1/chats/{kind}/{ref or agent.id}/stream", headers=agent.headers,
                        json={"items": [{"type": "status", "state": state}]})
    assert r.status_code in (200, 201), r.text


def test_the_agent_page_and_dashboard_say_who_woke_an_agent(team):
    hub, rv, b1, t = team
    say(hub, rv, "task", t["id"], "go", to="Builder-One")
    wake(hub, b1)
    stream_status(hub, b1, "working", "task", t["id"])
    page = get(hub, f"/ui/agents/{b1.id}").text
    woken = re.search(r'<p class="woken small-line is-awake">.*?</p>', page, re.S).group(0)
    assert "Awake · woken by Hub-Reviewer in task #" in text(woken)
    assert f'href="/ui/agents/{rv.id}" data-panel="agent:{rv.id}"' in woken
    assert f'href="/ui/tasks/{t["id"]}" data-panel="task:{t["id"]}"' in woken
    assert re.search(r"chain w[0-9a-f]{8}", woken)
    assert "Recent wakes" in page
    dash = live_region(get(hub, "/ui/dashboard").text)
    assert "woken by Hub-Reviewer" in text(dash)


def test_an_operator_wake_shows_as_woken_by_you(team):
    hub, rv, b1, t = team
    post(hub, f"/ui/chat/agent/{b1.id}", {"body": "wake up"})
    wake(hub, b1)
    page = get(hub, f"/ui/agents/{b1.id}").text
    assert "woken by you in Builder-One" in text(page)


# --- Pause wakes switches ------------------------------------------------------------------------------------

def test_an_agents_switch_pauses_its_wakes_and_shows_the_badge(team):
    hub, rv, b1, t = team
    page = get(hub, f"/ui/agents/{b1.id}").text
    switch = re.search(r'<form class="wake-switch".*?</form>', page, re.S).group(0)
    assert f'action="/ui/agents/{b1.id}/wakes"' in switch and 'aria-checked="false"' in switch
    assert 'name="paused" value="1"' in switch and re.search(r'name="csrf" value="[0-9a-f]{64}"', switch)
    assert "Wakes paused" not in page
    r = post(hub, f"/ui/agents/{b1.id}/wakes", {"paused": "1", "next": f"/ui/agents/{b1.id}"})
    assert r.status_code == 303 and r.headers["location"] == f"/ui/agents/{b1.id}"
    assert wakes.agent_view(hub.conn(), b1.id)["paused"] is True
    page = get(hub, f"/ui/agents/{b1.id}").text
    assert 'aria-checked="true"' in page and 'name="paused" value="0"' in page
    badge = text(re.search(r'<span class="wake-badge".*?</span></span>', page, re.S).group(0))
    assert "Wakes paused" in badge and "messages still arrive" in badge and "your own messages still wake it" in badge
    # paused: another agent's message is delivered but doesn't wake it
    say(hub, rv, "task", t["id"], "ping", to="Builder-One")
    found = hub.client.get("/api/v1/chats/deliveries", headers=b1.headers).json()["deliveries"]
    assert [d["wake"] for d in found] == [False]
    post(hub, f"/ui/agents/{b1.id}/wakes", {"paused": "0"})
    assert wakes.agent_view(hub.conn(), b1.id)["paused"] is False


def test_switches_in_live_lists_are_buttons_and_the_panel_switch_re_renders_the_panel(team):
    hub, rv, b1, t = team
    for path in ("/ui/dashboard", "/ui/agents"):
        live = live_region(get(hub, path).text)
        assert "<form" not in live, path  # the live region holds no forms
        assert f'hx-post="/ui/agents/{b1.id}/wakes"' in live and 'role="switch"' in live, path
    r = hub.client.post(f"/ui/agents/{b1.id}/wakes", headers=hub.op_headers(**{"HX-Request": "true"}),
                        data={"csrf": hub.csrf(), "paused": "1", "panel": f"agent:{b1.id}", "next": "/ui/agents"})
    assert r.status_code == 200 and 'aria-checked="true"' in r.text and "Wakes paused" in r.text
    assert 'data-panel-close' in r.text  # the panel, re-rendered
    r = hub.client.post(f"/ui/agents/{b1.id}/wakes", headers=hub.op_headers(**{"HX-Request": "true"}),
                        data={"csrf": hub.csrf(), "paused": "0", "next": "/ui/agents"})
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/ui/agents"


def test_the_hub_switch_pauses_every_wake_and_each_agent_shows_it(team):
    hub, rv, b1, t = team
    dash = get(hub, "/ui/dashboard").text
    assert 'hx-post="/ui/wakes"' in dash and "Pause all agent wakes" in dash
    r = post(hub, "/ui/wakes", {"paused": "1", "next": "/ui/dashboard"})
    assert r.status_code == 303 and r.headers["location"] == "/ui/dashboard"
    assert wakes.hub_paused(hub.conn())
    dash = text(get(hub, "/ui/dashboard").text)
    assert "no agent wakes another anywhere on the hub" in dash and "your own messages still wake agents" in dash
    assert "no agent wakes another anywhere on the hub" in text(get(hub, f"/ui/agents/{b1.id}").text)
    post(hub, "/ui/wakes", {"paused": "0"})
    assert not wakes.hub_paused(hub.conn())


def test_the_switches_need_csrf(team):
    hub, rv, b1, t = team
    for path in (f"/ui/agents/{b1.id}/wakes", "/ui/wakes"):
        r = hub.client.post(path, headers=hub.op_headers(), data={"paused": "1"}, follow_redirects=False)
        assert r.status_code == 403, path
    assert not wakes.agent_view(hub.conn(), b1.id)["paused"] and not wakes.hub_paused(hub.conn())


# --- project reviewers and the review flow --------------------------------------------------------------------

def test_the_reviewers_picker_sets_a_projects_reviewers(team):
    hub, rv, b1, t = team
    page = get(hub, "/ui/projects/proj").text
    picker = re.search(r'<details class="panel reviewers-picker".*?</details>', page, re.S).group(0)
    assert 'action="/ui/projects/proj/reviewers"' in picker and "none yet" in picker
    assert f'value="{rv.id}"> Hub-Reviewer' in picker and f'value="{b1.id}"> Builder-One' in picker
    r = post(hub, "/ui/projects/proj/reviewers", {"reviewer": [rv.id]})
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/proj"
    got = hub.client.get("/ui/api/projects/proj/reviewers", headers=hub.op_headers()).json()
    assert [x["name"] for x in got["reviewers"]] == ["Hub-Reviewer"]
    picker = re.search(r'<details class="panel reviewers-picker".*?</details>', get(hub, "/ui/projects/proj").text,
                       re.S).group(0)
    assert "Reviewers: Hub-Reviewer" in picker and f'value="{rv.id}" checked> Hub-Reviewer' in picker
    assert post(hub, "/ui/projects/proj/reviewers", {}).status_code == 303  # every box cleared
    assert hub.client.get("/ui/api/projects/proj/reviewers", headers=hub.op_headers()).json()["reviewers"] == []


def test_the_reviewers_picker_works_for_a_project_named_with_capitals(hub):
    a = hub.enroll("Rev-A", projects=("Agent-Hub",))
    page = get(hub, "/ui/projects/Agent-Hub").text
    assert 'action="/ui/projects/Agent-Hub/reviewers"' in page
    r = post(hub, "/ui/projects/agent-hub/reviewers", {"reviewer": ["rev-a"]})  # names ignore case
    assert r.status_code == 303 and r.headers["location"] == "/ui/projects/Agent-Hub"
    assert "Reviewers: Rev-A" in get(hub, "/ui/projects/Agent-Hub").text
    assert a.id


def test_task_cards_show_their_reviewers(team):
    hub, rv, b1, t = team
    for path in ("/ui/projects/proj", "/ui/tasks", f"/ui/tasks/{t['id']}"):
        line = re.search(r'<div class="small-line reviewers">.*?</div>', get(hub, path).text, re.S)
        assert line and "Reviewers: Hub-Reviewer" in text(line.group(0)), path
    assert "Reviewers: Hub-Reviewer" in text(panel(hub, f"task:{t['id']}").text)


def test_a_changes_verdict_shows_its_send_back_note_on_the_card_and_in_the_chat(team):
    hub, rv, b1, t = team
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET state = 'review' WHERE id = ?", (t["id"],))  # its builder finished
        task_agents.record_verdict(conn, t["id"], store.get_agent(conn, rv.id), verdict="changes",
                                   notes="Fix the <em>race</em> in wake()")
    for path in ("/ui/projects/proj", "/ui/tasks", f"/ui/tasks/{t['id']}"):
        page = get(hub, path).text
        card = re.search(r'<figure class="agent-text send-back">.*?</figure>', page, re.S)
        assert card, path
        assert "Sent back with changes by Hub-Reviewer" in card.group(0) and "&lt;em&gt;race" in card.group(0), path
    chat = get(hub, f"/ui/tasks/{t['id']}").text.split('class="chat"', 1)[1]
    assert "sent back with changes" in chat and "→ Builder-One" in chat and "Fix the &lt;em&gt;race" in chat


# --- the loop-pause approval card ----------------------------------------------------------------------------

@pytest.fixture
def loop(team, monkeypatch):
    """Hub-Reviewer and Builder-One wake each other in a direct chat past a loop limit of 2."""
    hub, rv, b1, t = team
    monkeypatch.setattr(wakes, "_loop_limit", 2)
    say(hub, rv, "dm", "Builder-One", "one")
    wake(hub, b1)
    say(hub, b1, "dm", "Hub-Reviewer", "two")
    wake(hub, rv)
    say(hub, rv, "dm", "Builder-One", "three")  # the third wake: over the limit, so the chain pauses
    conn = hub.conn()
    row = conn.execute("SELECT id FROM approvals WHERE kind = 'wake_loop'").fetchone()
    assert row is not None
    return hub, rv, b1, row["id"], chats.pair_key(rv.id, b1.id)


def test_the_loop_card_shows_the_chain_its_agents_the_count_and_the_last_wakes(loop):
    hub, rv, b1, approval_id, ref = loop
    page = get(hub, f"/ui/dm/{ref}").text
    card = re.search(r'<article class="ccard ccard-approval is-pending" data-approval="' + str(approval_id)
                     + r'">.*?</article>', page, re.S).group(0)
    words = text(card)
    assert "wake loop" in words and re.search(r"chain w[0-9a-f]{8}", words)
    assert re.search(r"Agents in the loop (Hub-Reviewer, Builder-One|Builder-One, Hub-Reviewer) Wakes", words)
    assert "Wakes 3 of 2" in words
    assert "Hub-Reviewer → Builder-One" in words and "Builder-One → Hub-Reviewer" in words
    assert "Approve resumes the chain" in words and "Reject keeps it paused" in words
    assert 'value="approve"' in card and 'value="reject"' in card
    assert "Wakes paused:" in text(page) and f"approval #{approval_id}" in text(page)  # the loop_paused line
    for html in (get(hub, f"/ui/approvals/{approval_id}").text, panel(hub, f"approval:{approval_id}").text):
        assert "Agents in the loop" in html and "Approve resumes the chain" in html
    assert "Wake loop paused: 3 wakes (limit 2) among 2 agents" in text(get(hub, "/ui/dashboard").text)


def test_approving_the_loop_card_resumes_the_chain_and_rejecting_keeps_it_paused(loop):
    hub, rv, b1, approval_id, ref = loop
    conn = hub.conn()
    chain = conn.execute("SELECT details FROM approvals WHERE id = ?", (approval_id,)).fetchone()["details"]
    chain_id = re.search(r'"chain_id": ?"(w[0-9a-f]+)"', chain).group(1)
    r = post(hub, f"/ui/approvals/{approval_id}/decide", {"decision": "approve", "next": f"/ui/dm/{ref}"})
    assert r.status_code == 303
    assert wakes.chain_view(conn, chain_id)["status"] == "open"
    page = get(hub, f"/ui/dm/{ref}").text
    assert "Wakes resumed:" in text(page) and "Approved" in text(page)


def test_rejecting_the_loop_card_keeps_the_chain_paused(loop):
    hub, rv, b1, approval_id, ref = loop
    conn = hub.conn()
    post(hub, f"/ui/approvals/{approval_id}/decide", {"decision": "reject"})
    chain_id = re.search(r'"chain_id": ?"(w[0-9a-f]+)"',
                         conn.execute("SELECT details FROM approvals WHERE id = ?", (approval_id,)).fetchone()[0])[1]
    assert wakes.chain_view(conn, chain_id)["status"] == "paused"
    assert "Wakes resumed" not in text(get(hub, f"/ui/dm/{ref}").text)
