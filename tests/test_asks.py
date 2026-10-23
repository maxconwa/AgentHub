import asyncio

import httpx
from mcp import Client

from agenthub.client import HubClient
from agenthub.mcp_server import build_server


def ask(hub, frm, to, q="What port does the API use?", timeout=60):
    return hub.client.post("/api/v1/projects/proj/asks", json={"to": to, "question": q, "timeout_seconds": timeout},
                           headers=frm.headers)


def test_ask_is_delivered_as_addressed_message_and_answered(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    r = ask(hub, a, "b")
    assert r.status_code == 201 and r.json()["status"] == "pending"
    ask_id = r.json()["id"]
    inbox = hub.client.get("/api/v1/inbox", headers=b.headers).json()["messages"]
    assert inbox[0]["addressed_to_me"] and f"[ask #{ask_id}]" in inbox[0]["body"] and inbox[0]["chat"] == "project:proj"
    assert [x["id"] for x in hub.client.get("/api/v1/asks", headers=b.headers).json()["asks"]] == [ask_id]
    ans = hub.client.post(f"/api/v1/asks/{ask_id}/answer", json={"answer": "8080"}, headers=b.headers)
    assert ans.json()["status"] == "answered"
    got = hub.client.get(f"/api/v1/asks/{ask_id}", headers=a.headers).json()
    assert got["answer"] == "8080"
    back = hub.client.get("/api/v1/inbox", headers=a.headers).json()["messages"]
    assert back[0]["addressed_to_me"] and "[answer to ask" in back[0]["body"]


def test_only_target_can_answer_and_only_once(hub):
    a, b, c = hub.enroll("a"), hub.enroll("b"), hub.enroll("c")
    ask_id = ask(hub, a, "b").json()["id"]
    assert hub.client.post(f"/api/v1/asks/{ask_id}/answer", json={"answer": "x"}, headers=c.headers).status_code == 404
    assert hub.client.post(f"/api/v1/asks/{ask_id}/answer", json={"answer": "x"}, headers=a.headers).status_code == 403
    assert hub.client.post(f"/api/v1/asks/{ask_id}/answer", json={"answer": "x"}, headers=b.headers).status_code == 200
    assert hub.client.post(f"/api/v1/asks/{ask_id}/answer", json={"answer": "y"}, headers=b.headers).status_code == 409


def test_third_parties_cannot_read_asks(hub):
    a, b, c = hub.enroll("a"), hub.enroll("b"), hub.enroll("c")
    ask_id = ask(hub, a, "b").json()["id"]
    assert hub.client.get(f"/api/v1/asks/{ask_id}", headers=c.headers).status_code == 404


def test_expired_ask_cannot_be_answered(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    ask_id = ask(hub, a, "b", timeout=10).json()["id"]
    hub.conn().execute("UPDATE asks SET expires_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?", (ask_id,))
    assert hub.client.get(f"/api/v1/asks/{ask_id}", headers=a.headers).json()["status"] == "expired"
    assert hub.client.post(f"/api/v1/asks/{ask_id}/answer", json={"answer": "x"}, headers=b.headers).status_code == 409


def test_cannot_ask_outside_project_or_self(hub):
    a = hub.enroll("a")
    hub.enroll("outsider", projects=("other",))
    assert ask(hub, a, "outsider").status_code == 422
    assert ask(hub, a, "a").status_code == 422
    assert ask(hub, a, "b", timeout=5).status_code == 422


async def test_ask_agent_tool_blocks_until_answered(dev_hub):
    a, b = dev_hub.enroll("a"), dev_hub.enroll("b")
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    ca, cb = (HubClient("http://hub", x.token, transport=transport) for x in (a, b))
    sa, sb = build_server(lambda: ca, poll_seconds=0.01), build_server(lambda: cb, poll_seconds=0.01)
    async with Client(sa) as alice, Client(sb) as bob:
        waiter = asyncio.create_task(alice.call_tool("ask_agent", {"project": "proj", "to": "b",
                                                                   "question": "ready?", "timeout_seconds": 20}))
        for _ in range(100):
            pending = await cb.pending_asks()
            if pending:
                break
            await asyncio.sleep(0.01)
        await bob.call_tool("answer_ask", {"ask_id": pending[0]["id"], "answer": "yes, go ahead"})
        out = "\n".join(x.text for x in (await waiter).content)
    assert "yes, go ahead" in out and 'author="agent:b (' in out and "<untrusted-content" in out
