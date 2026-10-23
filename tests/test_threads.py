"""Boards as Reddit-style threads: messages.parent_id/root_id, replies over the API, MCP and the dashboard, and
the Board page listing only project and task boards."""

from __future__ import annotations

import re

import httpx
import pytest
from mcp import Client

from agenthub.client import HubClient
from agenthub.hub import db, prompts, store, tasks, views
from agenthub.mcp_server import build_server

from .conftest import OP_LOGIN

OP_ACTOR = f"human:{OP_LOGIN}"


def say(hub, agent, body, project="proj", **extra):
    return hub.client.post(f"/api/v1/projects/{project}/messages", headers=agent.headers, json={"body": body, **extra})


def get(hub, path):
    return hub.client.get(path, headers=hub.op_headers(), follow_redirects=False)


def live(html):
    return re.search(r'<section id="live".*?</section>\s*(?=<|$)', html, re.S).group(0)


def make_task(hub, title="Fix login", project="proj"):
    conn = hub.conn()
    with db.transaction(conn):
        return tasks.create_task(conn, actor=OP_ACTOR, project=project, title=title, body="", assignee=None)


# --- schema ------------------------------------------------------------------------------------


def test_migration_keeps_existing_messages_as_top_level_posts(tmp_path):
    conn = db.connect(tmp_path / "hub.sqlite3")
    before = db.MIGRATIONS.index(next(m for m in db.MIGRATIONS if "parent_id" in m))
    for v, script in enumerate(db.MIGRATIONS[:before], start=1):
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {v};COMMIT;")
    conn.execute("INSERT INTO projects (name, created_at) VALUES ('p', 'x')")
    conn.execute("INSERT INTO channels (project, name, created_by, created_at) VALUES ('p', 'general', 'h', 'x')")
    conn.execute("INSERT INTO messages (project, channel_id, author, body, ts) VALUES ('p', 1, 'h', 'old', 'x')")
    assert db.migrate(conn) == len(db.MIGRATIONS)
    row = conn.execute("SELECT parent_id, root_id FROM messages").fetchone()
    assert (row["parent_id"], row["root_id"]) == (None, None)
    assert [p["body"] for p in views.board_posts(conn, "p", "general")] == ["old"]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- replies over the API ----------------------------------------------------------------------


def test_reply_goes_to_its_parents_channel_and_hangs_from_the_root(hub):
    a = hub.enroll("worker")
    t = make_task(hub)
    post = say(hub, a, "Plan\nfirst", channel=f"task-{t['id']}").json()
    reply = say(hub, a, "ok", reply_to=post["id"])  # no channel: the parent's
    assert reply.status_code == 201, reply.text
    r = reply.json()
    assert (r["channel"], r["parent_id"], r["root_id"]) == (f"task-{t['id']}", post["id"], post["id"])
    deeper = say(hub, a, "deeper", channel=f"task-{t['id']}", reply_to=r["id"]).json()
    assert (deeper["parent_id"], deeper["root_id"]) == (r["id"], post["id"])
    assert say(hub, a, "top").json()["channel"] == "general" and say(hub, a, "top").json()["parent_id"] is None


def test_reply_must_be_in_the_parents_project_and_channel(hub):
    a = hub.enroll("worker", projects=("proj", "other"))
    post = say(hub, a, "hello").json()
    wrong_channel = say(hub, a, "x", channel="task-1", reply_to=post["id"])
    assert wrong_channel.status_code == 422 and "general" in wrong_channel.text
    wrong_project = say(hub, a, "x", project="other", reply_to=post["id"])
    assert wrong_project.status_code == 422 and "no message" in wrong_project.text
    assert say(hub, a, "x", reply_to=999999).status_code == 422
    assert say(hub, a, "x", reply_to=0).status_code == 422
    outsider = hub.enroll("outsider", projects=("other",))
    assert say(hub, outsider, "x", reply_to=post["id"]).status_code == 403  # not in the project at all
    assert hub.conn().execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_read_inbox_shows_a_reply_as_a_reply(hub):
    a, b = hub.enroll("worker"), hub.enroll("helper")
    post = say(hub, a, "question").json()
    say(hub, b, "answer", reply_to=post["id"])
    got = hub.client.get("/api/v1/inbox", headers=a.headers).json()["messages"]
    assert [(m["body"], m["parent_id"]) for m in got] == [("answer", post["id"])]


# --- MCP and client ----------------------------------------------------------------------------


async def test_client_and_mcp_post_reply_to(dev_hub):
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    a, b = dev_hub.enroll("alice"), dev_hub.enroll("bob")
    ca, cb = (HubClient("http://hub", x.token, transport=transport) for x in (a, b))
    post = await ca.post("proj", "Plan\nthe post", channel="task-7")
    reply = await ca.post("proj", "client reply", reply_to=post["id"])
    assert (reply["channel"], reply["parent_id"]) == ("task-7", post["id"])
    async with Client(build_server(lambda: cb)) as c:
        r = await c.call_tool("post", {"project": "proj", "body": "mcp reply", "reply_to": post["id"]})
        out = "\n".join(x.text for x in (await c.call_tool("read_inbox", {})).content)
    text = "\n".join(x.text for x in r.content)
    assert not r.is_error and f"proj/task-7 as a reply to #{post['id']}" in text
    last = store.recent_messages(dev_hub.conn(), project="proj", channel="task-7")[-1]
    assert (last["body"], last["parent_id"], last["author"]) == ("mcp reply", post["id"], b.id)
    # bob's inbox: alice's post, then her reply, labeled as a reply to the post
    assert re.search(rf'<untrusted-content [^>]*channel="task-7"[^>]*reply_to="{post["id"]}">\nclient reply', out)
    assert re.search(r'<untrusted-content [^>]*channel="task-7"[^>]*>\nPlan', out)
    async with Client(build_server(lambda: cb)) as c:  # a reply in another channel than its parent's
        bad = await c.call_tool("post", {"project": "proj", "body": "x", "channel": "general", "reply_to": post["id"]})
    assert bad.is_error and "422" in "\n".join(x.text for x in bad.content)


# --- the Board page ----------------------------------------------------------------------------


def test_board_index_lists_only_project_and_task_boards(hub):
    a = hub.enroll("worker", projects=("proj", "quiet"))
    live_task, finished, finished_quiet = make_task(hub, "Live one"), make_task(hub, "Done one"), make_task(hub, "Old")
    say(hub, a, "on the finished task", channel=f"task-{finished['id']}")
    say(hub, a, "coordination", channel="coordination-1")
    say(hub, a, "prompt talk", channel="prompt-3")
    conn = hub.conn()
    with db.transaction(conn):
        for t in (finished, finished_quiet):
            conn.execute("UPDATE tasks SET state = 'done' WHERE id = ?", (t["id"],))
    idx = views.board_index(conn)
    assert set(idx) == {"projects", "tasks"}
    assert {i["key"] for i in idx["projects"]} == {"ch-proj~general", "ch-quiet~general"}  # posted on or not
    assert [i["key"] for i in idx["tasks"]] == [f"ch-proj~task-{finished['id']}", f"ch-proj~task-{live_task['id']}"]
    html = get(hub, "/ui/board").text
    assert "coordination-1" not in html and "prompt-3" not in html and "Old" not in html
    assert f'href="/ui/board/ch-proj~task-{live_task["id"]}"' in html and 'href="/ui/board/ch-quiet~general"' in html


# --- thread pages ------------------------------------------------------------------------------


@pytest.fixture
def thread(hub):
    """On proj/general: a post by worker with replies two deep, and an older post nobody answered."""
    a, b = hub.enroll("worker"), hub.enroll("helper")
    old = say(hub, a, "Old news\n<b>stale</b>").json()
    post = say(hub, a, "Deploy plan <i>v2</i>\nThe **details** are here.").json()
    r1 = say(hub, b, "first reply", reply_to=post["id"]).json()
    r2 = say(hub, a, "reply to the reply", reply_to=r1["id"]).json()
    return hub, a, b, old, post, r1, r2


def test_board_lists_posts_by_latest_activity_with_titles_and_tallies(thread):
    hub, a, b, old, post, r1, r2 = thread
    html = get(hub, "/ui/board/ch-proj~general").text
    region = live(html)
    assert "<form" not in region  # the polled region holds no forms
    assert region.index(f'id="post-{post["id"]}"') < region.index(f'id="post-{old["id"]}"')
    assert "Deploy plan &lt;i&gt;v2&lt;/i&gt;" in region and "<i>v2" not in region  # a title is plain text
    assert "2 replies" in region and "0 replies" in region
    assert f'href="/ui/board/ch-proj~general/posts/{post["id"]}"' in region
    assert "first reply" not in region  # replies are on the post's page
    assert 'action="/ui/board/ch-proj~general"' in html and 'name="reply_to"' not in html  # New post form
    # A reply to the old post brings it up
    say(hub, b, "bump", reply_to=old["id"])
    region = live(get(hub, "/ui/board/ch-proj~general").text)
    assert region.index(f'id="post-{old["id"]}"') < region.index(f'id="post-{post["id"]}"')


def test_post_page_nests_replies_and_offers_a_reply_form(thread):
    hub, a, b, old, post, r1, r2 = thread
    url = f"/ui/board/ch-proj~general/posts/{post['id']}"
    html = get(hub, url).text
    assert "<h1>Deploy plan &lt;i&gt;v2&lt;/i&gt;</h1>" in html and "<strong>details</strong>" in html
    region = live(html)
    assert "<form" not in region
    outer = re.search(rf'<details class="reply" id="post-{r1["id"]}".*', region, re.S).group(0)
    assert f'id="post-{r2["id"]}"' in outer and outer.index("first reply") < outer.index("reply to the reply")
    assert f'?reply_to={r2["id"]}#reply' in region
    form = re.search(r'<form[^>]*action="/ui/board/ch-proj~general".*?</form>', html, re.S).group(0)
    assert f'name="reply_to" value="{post["id"]}"' in form
    # A reply's Reply link: the form names that reply and quotes it
    html = get(hub, f"{url}?reply_to={r1['id']}").text
    assert f'name="reply_to" value="{r1["id"]}"' in html and f"Reply to #{r1['id']}" in html
    assert get(hub, f"{url}?reply_to={old['id']}").status_code == 404  # not on this thread
    # A reply's own id is its post's thread, scrolled to it
    r = get(hub, f"/ui/board/ch-proj~general/posts/{r2['id']}")
    assert r.status_code == 303 and r.headers["location"] == f"{url}#post-{r2['id']}"
    assert get(hub, "/ui/board/ch-proj~general/posts/999999").status_code == 404


def test_operator_replies_from_the_dashboard(thread):
    hub, a, b, old, post, r1, r2 = thread
    r = hub.client.post("/ui/board/ch-proj~general", headers=hub.op_headers(), follow_redirects=False,
                        data={"csrf": hub.csrf(), "body": "operator says yes", "reply_to": str(r1["id"])})
    last = store.recent_messages(hub.conn(), project="proj", channel="general")[-1]
    assert (last["body"], last["author"], last["parent_id"]) == ("operator says yes", OP_ACTOR, r1["id"])
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/board/ch-proj~general/posts/{post['id']}#post-{last['id']}"
    assert "operator says yes" in get(hub, r.headers["location"].split("#")[0]).text
    # The agent sees the operator's reply as a reply in its inbox
    got = hub.client.get("/api/v1/inbox", headers=b.headers).json()["messages"]
    assert any(m["body"] == "operator says yes" and m["parent_id"] == r1["id"] for m in got)
    # A reply_to on another board is refused and nothing is stored
    t = make_task(hub)
    other = say(hub, a, "task post", channel=f"task-{t['id']}").json()
    r = hub.client.post("/ui/board/ch-proj~general", headers=hub.op_headers(), follow_redirects=False,
                        data={"csrf": hub.csrf(), "body": "misplaced", "reply_to": str(other["id"])})
    assert r.status_code == 422
    assert "misplaced" not in [m["body"] for m in store.recent_messages(hub.conn(), project="proj")]


def test_task_and_project_pages_list_their_board_posts(thread):
    hub, a, b, old, post, r1, r2 = thread
    region = live(get(hub, "/ui/projects/proj").text)
    assert f'href="/ui/board/ch-proj~general/posts/{post["id"]}"' in region and "2 replies" in region


# --- device-prompt discussion ------------------------------------------------------------------


def test_device_prompt_discussion_is_on_its_prompt_page(hub):
    a, b = hub.enroll("worker"), hub.enroll("helper")
    conn = hub.conn()
    node = conn.execute("SELECT device_node_id FROM agents WHERE id = ?", (a.id,)).fetchone()[0]
    with db.transaction(conn):  # both on one machine, connected, with runners that run prompts
        conn.execute("UPDATE agents SET device_node_id = ?, connected = 1, prompt_support = 1 WHERE id IN (?, ?)",
                     (node, a.id, b.id))
    with db.transaction(conn):
        p = prompts.create(conn, target=f"device:{node}", body="split the GPUs", actor=OP_ACTOR)
    assert p["thread"] == f"device:{node}"
    assert hub.client.post("/api/v1/device/messages", json={"body": "I take GPU <b>0</b>"},
                           headers=a.headers).status_code == 201
    page = views.prompt(conn, p["id"])
    assert page["thread_href"] == f"/ui/prompts/{p['id']}#discussion"
    html = get(hub, f"/ui/prompts/{p['id']}").text
    assert 'id="discussion"' in html and "I take GPU &lt;b&gt;0&lt;/b&gt;" in html
    device_page = get(hub, f"/ui/devices/{node}").text
    assert "I take GPU" not in device_page and "/notes" not in device_page
