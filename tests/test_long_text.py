"""No length limit on what the operator writes (0.3.9.0): a 200,000-character message is stored and reaches the
runner and the session byte for byte; events still carry ids only; agents' messages keep a 64 KiB cap."""

import json
from urllib.parse import urlencode

import httpx
import pytest
from mcp import Client

from agenthub.client import HubClient
from agenthub.hub import chats, db, limits, questions, store
from agenthub.mcp_server import build_server

from .conftest import OPERATOR


def _long_text(n: int = 200_000) -> str:
    """``n`` characters of everything a message may hold: ASCII, CRLF and tabs, quotes and backslashes, markup,
    accents, CJK, emoji outside the BMP, a combining mark and a line separator."""
    piece = 'Line "one"\\ <b>&amp;</b>\r\n\ttab café 漢字 \U0001f600 é   end.\n'
    text = (piece * (n // len(piece) + 1))[:n]
    assert len(text) == n
    return text


def api(hub, agent, method, path, **kw):
    return hub.client.request(method, f"/api/v1{path}", headers=agent.headers, **kw)


@pytest.fixture
def pair(hub):
    return hub, hub.enroll("Alice", projects=("Golem",)), hub.enroll("bob", projects=("Golem",))


def test_a_200k_operator_message_is_stored_and_delivered_byte_for_byte(pair):
    hub, a, b = pair
    body = _long_text()
    conn = hub.conn()
    with db.transaction(conn):
        sent = chats.send(conn, "project", "golem", body, OPERATOR)
    stored = conn.execute("SELECT body FROM chat_messages WHERE id = ?", (sent["id"],)).fetchone()["body"]
    assert stored == body and stored.encode() == body.encode()

    # the runner's fetch path: its pending deliveries, then its claim
    deliveries = api(hub, a, "GET", "/chats/deliveries").json()["deliveries"]
    assert [d["body"] for d in deliveries] == [body]
    claimed = api(hub, a, "POST", f"/chats/deliveries/{deliveries[0]['id']}", json={"mode": "live"})
    assert claimed.status_code == 200 and claimed.json()["body"] == body

    # a session's read_inbox (the other agent's copy) and chat history
    mail = [m for m in api(hub, b, "GET", "/inbox").json()["messages"] if m["id"] == sent["id"]]
    assert [m["body"] for m in mail] == [body]
    history = api(hub, b, "GET", "/chats/project/Golem/messages").json()["messages"]
    assert [m["body"] for m in history if m["id"] == sent["id"]] == [body]

    # events carry ids only: neither the message's event nor its deliveries' hold any of the text
    for r in conn.execute("SELECT type, payload FROM events WHERE type IN ('chat.message', 'chat.deliver')"):
        assert len(r["payload"]) < 500 and "Line" not in r["payload"], r["type"]


async def test_the_runner_client_gets_the_whole_message(dev_hub):
    hub, a = dev_hub, dev_hub.enroll("Alice", projects=("Golem",))
    body = _long_text()
    conn = hub.conn()
    with db.transaction(conn):
        chats.send(conn, "agent", "alice", body, OPERATOR)  # its own chat, named in any case
    client = HubClient("http://hub", a.token, transport=httpx.ASGITransport(app=hub.client.app))
    (delivery,) = await client.pending_deliveries()
    assert delivery["body"] == body and delivery["verified"] is True
    assert (await client.claim_delivery(delivery["id"], "woke"))["body"] == body


async def test_a_session_reads_the_whole_message_through_mcp(dev_hub):
    a = dev_hub.enroll("Alice", projects=("Golem",))
    body = _long_text()
    conn = dev_hub.conn()
    with db.transaction(conn):
        chats.send(conn, "project", "Golem", body, OPERATOR)
    client = HubClient("http://hub", a.token, transport=httpx.ASGITransport(app=dev_hub.client.app))
    async with Client(build_server(lambda: client, poll_seconds=0.01)) as c:
        out = "\n".join(x.text for x in (await c.call_tool("read_inbox", {})).content if hasattr(x, "text"))
    assert body in out


def test_the_operators_dashboard_request_may_carry_long_text(hub):
    """A form field past Starlette's 1 MB default and a body past an agent's limit: 200,000 characters that are 3 to
    12 bytes each once form-encoded."""
    text = (_long_text(100_000) + "\U0001f600\u6f22" * 50_000)  # 200,000 characters
    assert len(text) == 200_000 and len(urlencode({"description": text})) > limits.AGENT_MIN_BODY_BYTES
    r = hub.client.post("/ui/projects", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "name": "Golem", "description": text}, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    row = hub.conn().execute("SELECT description FROM projects WHERE name = 'Golem'").fetchone()
    assert row["description"] == text


def test_the_operators_other_text_has_no_length_limit(pair):
    hub, a, _ = pair
    text = _long_text(100_000)
    conn = hub.conn()
    with db.transaction(conn):
        q = questions.ask(conn, store.find_agent(conn, a.id), project="Golem", question="what now?")
        assert questions.answer(conn, q["id"], answer=text, actor=OPERATOR)["answer"] == text
        store.set_project_description(conn, "GOLEM", text, actor=OPERATOR)
    assert store.list_projects(conn)[0]["description"] == text
    answer = [d for d in api(hub, a, "GET", "/chats/deliveries").json()["deliveries"]]
    assert len(answer) == 1 and text in answer[0]["body"]


# --- agents keep a cap --------------------------------------------------------------------------------------------


def test_an_agents_message_is_capped_at_64_kib(pair):
    hub, a, b = pair
    cap = limits.AGENT_TEXT_CHARS
    assert cap == 64 * 1024 == chats.MAX_BODY_CHARS
    at_cap = "é" * cap  # characters, not bytes
    r = api(hub, a, "POST", "/chats/project/golem/messages", json={"body": at_cap})
    assert r.status_code == 201, r.text[:300]
    assert r.json()["body"] == at_cap
    assert api(hub, a, "POST", "/chats/project/golem/messages", json={"body": at_cap + "x"}).status_code == 422
    conn = hub.conn()
    with pytest.raises(store.Invalid), db.transaction(conn):  # and the model checks it too, not just the API
        chats.post(conn, store.find_agent(conn, a.id), "project", "Golem", at_cap + "x")
    for path, payload in (("/projects/golem/questions", {"question": at_cap + "x"}),
                          ("/projects/golem/asks", {"to": "bob", "question": at_cap + "x", "timeout_seconds": 10}),
                          ("/device/messages", {"body": at_cap + "x"})):
        assert api(hub, a, "POST", path, json=payload).status_code == 422, path


async def test_mcp_reports_an_overlong_agent_message(dev_hub):
    a = dev_hub.enroll("Alice", projects=("Golem",))
    client = HubClient("http://hub", a.token, transport=httpx.ASGITransport(app=dev_hub.client.app))
    async with Client(build_server(lambda: client, poll_seconds=0.01)) as c:
        r = await c.call_tool("chat_post", {"chat": "project:Golem", "body": "x" * (limits.AGENT_TEXT_CHARS + 1)})
    assert r.is_error
    assert dev_hub.conn().execute("SELECT COUNT(*) FROM chat_messages WHERE kind = 'text'").fetchone()[0] == 0


def test_the_hub_never_truncates_an_operator_message_in_a_payload(pair):
    """Every field of a delivery and an inbox entry that holds the message holds all of it."""
    hub, a, _ = pair
    body = _long_text(150_000)
    conn = hub.conn()
    with db.transaction(conn):
        chats.send(conn, "agent", a.id, body, OPERATOR)
    raw = api(hub, a, "GET", "/chats/deliveries").content
    assert json.loads(raw)["deliveries"][0]["body"] == body
    inbox = api(hub, a, "GET", "/inbox", params={"peek": "true"}).json()["messages"]
    assert [m["body"] for m in inbox if m.get("kind") == "private"] == [body]
