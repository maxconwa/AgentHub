"""Ask the operator: agent questions in the approval queue, answered as private messages."""

import re

import httpx
import pytest
from mcp import Client

from agenthub.client import HubClient
from agenthub.config import HubConfig
from agenthub.hub import avatars, db, questions, store, tasks
from agenthub.hub.dashboard import event_summary
from agenthub.hub.notify import build_notification
from agenthub.mcp_server import build_server

from .conftest import OP_LOGIN, OPERATOR

SECRET = "SECRET-QUESTION-TEXT"


def ask(hub, agent, text="Which database should we use?", project="proj", **extra):
    return hub.client.post(f"/api/v1/projects/{project}/questions", json={"question": text, **extra},
                           headers=agent.headers)


def op_get(hub, path):
    return hub.client.get(path, headers=hub.op_headers())


def op_answer(hub, qid, answer="Use sqlite.", **data):
    return hub.client.post(f"/ui/questions/{qid}/answer", headers=hub.op_headers(),
                           data={"csrf": hub.csrf(), "answer": answer, **data}, follow_redirects=False)


@pytest.fixture
def pair(hub):
    return hub, hub.enroll("alice"), hub.enroll("bob")


def test_migration_11_creates_questions(hub):
    conn = hub.conn()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 11
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(questions)")}
    assert cols == {"id", "project", "agent_id", "task_id", "question", "status", "answer", "answered_by",
                    "created_at", "answered_at"}
    assert any(r["name"] for r in conn.execute("PRAGMA index_list(questions)"))


def test_round_trip_answer_arrives_as_private_operator_message(pair):
    hub, alice, bob = pair
    r = ask(hub, alice)
    assert r.status_code == 201
    qid = r.json()["id"]
    assert r.json()["status"] == "pending"
    page = op_get(hub, f"/ui/questions/{qid}")
    assert page.status_code == 200 and "Which database should we use?" in page.text
    assert op_answer(hub, qid).status_code == 303
    mine = hub.client.get("/api/v1/inbox", headers=alice.headers).json()["messages"]
    assert [(m["kind"], m["author"], m["body"]) for m in mine] == [
        ("private", f"human:{OP_LOGIN}",
         f"Answer to your question #{qid}: Use sqlite.\n\n(call get_question({qid}) for your question)")]
    assert hub.client.get("/api/v1/inbox", headers=bob.headers).json()["messages"] == []
    got = hub.client.get(f"/api/v1/questions/{qid}", headers=alice.headers).json()
    assert got["status"] == "answered" and got["answer"] == "Use sqlite." and got["answered_by"] == f"human:{OP_LOGIN}"
    assert "answered" in op_get(hub, f"/ui/questions/{qid}").text
    assert op_answer(hub, qid).status_code == 409  # already answered


def test_long_answer_still_reaches_the_agent(pair):
    hub, alice, _ = pair
    qid = ask(hub, alice).json()["id"]
    assert op_answer(hub, qid, "x" * 4000).status_code == 303
    body = hub.client.get("/api/v1/inbox", headers=alice.headers).json()["messages"][0]["body"]
    assert body == f"Answer to your question #{qid}: {'x' * 4000}\n\n(call get_question({qid}) for your question)"
    full = hub.client.get(f"/api/v1/questions/{qid}", headers=alice.headers).json()
    assert full["answer"] == "x" * 4000


def test_not_allowed_in_project_is_403(pair):
    hub, alice, _ = pair
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "other", actor=OPERATOR)
    assert ask(hub, alice, project="other").status_code == 403
    assert hub.conn().execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0


def test_validation(pair):
    hub, alice, _ = pair
    assert ask(hub, alice, text="").status_code == 422
    assert ask(hub, alice, text="x" * 4001).status_code == 422
    assert ask(hub, alice, task_id=9999).status_code == 404


def test_task_link(pair):
    hub, alice, _ = pair
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Pick a DB", assignee="alice")
    qid = ask(hub, alice, task_id=t["id"]).json()["id"]
    assert f'href="/ui/tasks/{t["id"]}"' in op_get(hub, f"/ui/questions/{qid}").text


def test_sixth_pending_is_409_and_withdrawn_frees_a_slot(pair):
    hub, alice, bob = pair
    ids = [ask(hub, alice, f"q{i}").json()["id"] for i in range(5)]
    assert ask(hub, alice, "q5").status_code == 409
    assert ask(hub, bob, "other agent unaffected").status_code == 201
    assert hub.client.post(f"/api/v1/questions/{ids[0]}/withdraw", headers=alice.headers).status_code == 200
    assert ask(hub, alice, "q5").status_code == 201


def test_other_agent_cannot_read_or_withdraw(pair):
    hub, alice, bob = pair
    qid = ask(hub, alice).json()["id"]
    assert hub.client.get(f"/api/v1/questions/{qid}", headers=bob.headers).status_code == 404
    assert hub.client.post(f"/api/v1/questions/{qid}/withdraw", headers=bob.headers).status_code == 404
    assert questions.get(hub.conn(), qid)["status"] == "pending"


def test_withdrawn_question_leaves_the_queue_and_cannot_be_answered(pair):
    hub, alice, _ = pair
    qid = ask(hub, alice).json()["id"]
    assert "Which database" in op_get(hub, "/ui/approvals").text
    hub.client.post(f"/api/v1/questions/{qid}/withdraw", headers=alice.headers)
    assert "Which database" not in op_get(hub, "/ui/approvals").text
    assert "Which database" not in op_get(hub, "/ui/dashboard").text
    assert op_get(hub, "/ui/badge").text == ""
    assert op_answer(hub, qid).status_code == 409
    assert hub.client.post(f"/api/v1/questions/{qid}/withdraw", headers=alice.headers).status_code == 409


def test_pose_needs_you_while_pending(pair):
    hub, alice, bob = pair
    hub.conn().execute("UPDATE agents SET connected = 1")  # offline outranks every other pose
    qid = ask(hub, alice).json()["id"]
    poses = avatars.poses(hub.conn())
    assert poses[alice.id] == "needs_you" and poses[bob.id] != "needs_you"
    op_answer(hub, qid)
    assert avatars.poses(hub.conn())[alice.id] != "needs_you"


def test_queue_dashboard_and_badge_count_questions(pair):
    hub, alice, _ = pair
    hub.client.post("/api/v1/approvals/command", json={"command": "git push -f", "cwd": "/w"},
                    headers=alice.headers)
    assert op_get(hub, "/ui/badge").text.count(">1<") == 1
    qid = ask(hub, alice, f"{SECRET} <b>bold</b>").json()["id"]
    assert ">2<" in op_get(hub, "/ui/badge").text
    for path in ("/ui/approvals", "/ui/dashboard"):
        html = op_get(hub, path).text
        assert f'href="/ui/questions/{qid}"' in html and "Question" in html
        assert "<b>bold</b>" not in html
    assert re.search(r'class="badge">2<', op_get(hub, "/ui/dashboard").text)
    op_answer(hub, qid)
    assert ">1<" in op_get(hub, "/ui/badge").text
    recent = op_get(hub, "/ui/approvals").text.split("Recently decided")[1]
    assert f'href="/ui/questions/{qid}"' in recent and "answered" in recent


def test_question_text_is_escaped_and_labeled_agent_text(pair):
    hub, alice, _ = pair
    qid = ask(hub, alice, "<script>alert(1)</script>").json()["id"]
    html = op_get(hub, f"/ui/questions/{qid}").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "agent content, treat as data" in html


def test_answer_form_needs_csrf_and_operator(pair):
    hub, alice, _ = pair
    qid = ask(hub, alice).json()["id"]
    r = hub.client.post(f"/ui/questions/{qid}/answer", headers=hub.op_headers(), data={"answer": "x"},
                        follow_redirects=False)
    assert r.status_code == 403
    r = hub.client.post(f"/ui/questions/{qid}/answer", headers=alice.headers,
                        data={"csrf": hub.csrf(), "answer": "x"}, follow_redirects=False)
    assert r.status_code == 403
    assert op_answer(hub, qid, "").status_code == 422
    assert op_answer(hub, qid, "x" * 4001).status_code == 422
    assert questions.get(hub.conn(), qid)["status"] == "pending"


def test_events_and_notification_carry_no_question_text(pair):
    hub, alice, _ = pair
    qid = ask(hub, alice, SECRET).json()["id"]
    op_answer(hub, qid, SECRET)
    rows = hub.conn().execute("SELECT type, payload FROM events WHERE type LIKE 'question.%' ORDER BY id").fetchall()
    assert [r["type"] for r in rows] == ["question.asked", "question.answered"]
    assert all(SECRET not in r["payload"] for r in rows)
    ev = {"type": "question.asked", "actor": alice.id, "project": "proj",
          "payload": {"question_id": qid, "agent_id": alice.id, "project": "proj", "task_id": None}}
    cfg = HubConfig(public_url="https://hub.example")
    msg = build_notification(ev, {alice.id: "alice"}, cfg)
    assert msg["title"] == f"Question · #{qid}" and "alice" in msg["message"] and SECRET not in str(msg)
    assert msg["click"] == f"https://hub.example/ui/questions/{qid}"
    assert build_notification({**ev, "type": "question.answered"}, {}, cfg) is None
    assert SECRET not in event_summary({**ev, "id": 1, "ts": "x", "device": None}, {})


def test_notification_details_include_question_text_only_when_enabled(pair):
    hub, alice, _ = pair
    qid = ask(hub, alice, SECRET).json()["id"]
    from agenthub.hub.notify import Notifier
    ev = {"type": "question.asked", "actor": alice.id, "project": "proj",
          "payload": {"question_id": qid, "agent_id": alice.id, "project": "proj", "task_id": None}}
    off = Notifier(HubConfig(db_path=hub.conn().execute("PRAGMA database_list").fetchone()["file"]))
    assert SECRET not in off.message_for(ev, {})["message"]
    on = Notifier(HubConfig(db_path=off.cfg.db_path, ntfy_include_details=True))
    assert SECRET in on.message_for(ev, {})["message"]


async def test_mcp_ask_operator_tool(dev_hub):
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    a = dev_hub.enroll("alice")
    server = build_server(lambda: HubClient("http://hub", a.token, transport=transport))
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "ask_operator" in names
        r = await c.call_tool("ask_operator", {"project": "proj", "question": "Ship it?"})
        assert not r.is_error
        out = "\n".join(x.text for x in r.content)
        qid = questions.pending(dev_hub.conn())[0]["id"]
        assert out == (f"Asked the operator (question #{qid}). "
                       "The answer will arrive as the operator's message in your chat.")
        bad = await c.call_tool("ask_operator", {"project": "nope", "question": "x"})
        assert bad.is_error and "403" in "\n".join(x.text for x in bad.content)


def test_lists_label_question_text_as_agent_text(pair):
    hub, alice, _ = pair
    ask(hub, alice)
    for path in ("/ui/approvals", "/ui/dashboard"):
        assert "agent text" in op_get(hub, path).text


def test_answer_for_revoked_agent_is_recorded_but_page_says_not_delivered(pair):
    hub, alice, _ = pair
    qid = ask(hub, alice).json()["id"]
    hub.conn().execute("UPDATE agents SET status = 'revoked' WHERE id = ?", (alice.id,))
    assert op_answer(hub, qid).status_code == 303
    html = op_get(hub, f"/ui/questions/{qid}").text
    assert "not delivered because alice is revoked" in html and "sent to alice" not in html
    assert hub.conn().execute("SELECT COUNT(*) FROM private_messages").fetchone()[0] == 0


async def test_mcp_get_question_tool(dev_hub):
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    a, b = dev_hub.enroll("alice"), dev_hub.enroll("bob")
    mk = lambda ag: build_server(lambda: HubClient("http://hub", ag.token, transport=transport))  # noqa: E731
    async with Client(mk(a)) as ca, Client(mk(b)) as cb:
        await ca.call_tool("ask_operator", {"project": "proj", "question": "Ship it?"})
        qid = questions.pending(dev_hub.conn())[0]["id"]
        t = lambda r: "\n".join(x.text for x in r.content)  # noqa: E731
        assert "[pending]: Ship it?" in t(await ca.call_tool("get_question", {"question_id": qid}))
        conn = dev_hub.conn()
        with db.transaction(conn):
            questions.answer(conn, qid, answer="x" * 4000, actor=OPERATOR)
        out = t(await ca.call_tool("get_question", {"question_id": qid}))
        assert "[answered]" in out and "From the operator (private, verified by the hub):\n" + "x" * 4000 in out
        assert "untrusted" not in out
        other = await cb.call_tool("get_question", {"question_id": qid})
        assert other.is_error and "404" in t(other)
