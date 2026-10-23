"""0.3.10: the operator's message to every agent on a device. One message in the device's chat, one delivery per active
agent on that machine, which wakes it in its own chat session unless its wakes (or the hub's) are paused; the
operator-only route and what it reports; who may read the device chat; the runner's side, end to end."""

from __future__ import annotations

import asyncio

import pytest

from agenthub import mcp_server
from agenthub.hub import broadcasts, chats, db, events, store, wakes
from agenthub.runner.chat import Delivery, SessionOutput, delivery_text

from .conftest import OP_LOGIN, OPERATOR, AgentCreds
from .test_chat_runner import _recording_client, chat_runner, states, texts
from .test_runner import fake_claude, live_hub, stop, wait_for  # noqa: F401 - fixtures

BOX = "nbox"
BOX_NAME = "shared-box.example.ts.net"
SECRET = "rotate the staging keys tonight"
OP_ACTOR = f"human:{OP_LOGIN}"  # the operator, through the route


def enroll_on(hub, name, node=BOX, device=BOX_NAME, projects=("proj",), approve=True):
    """Enroll an agent from a machine whose tailscale StableID is ``node``."""
    ip = hub.new_ip()
    hub.whois.add(ip, node_id=node, name=device)
    r = hub.client.post("/api/v1/enroll", json={"code": hub.code(projects), "name": name},
                        headers={"x-forwarded-for": ip})
    assert r.status_code == 201, r.text
    agent = AgentCreds(id=r.json()["agent"]["id"], name=name, token=r.json()["token"], ip=ip)
    if approve:
        hub.set_status(agent, "active")
    return agent


def online(hub, *agents, chat=1):
    """Their runners are connected (and, with ``chat``, take chat messages: 0.3.8 or later)."""
    conn = hub.conn()
    for a in agents:
        conn.execute("UPDATE agents SET connected = 1, chat_support = ? WHERE id = ?", (chat, a.id))


def broadcast(hub, body=SECRET, node=BOX, **headers):
    h = hub.op_headers(**{"x-csrf-token": hub.csrf(), **headers})
    return hub.client.post(f"/ui/api/devices/{node}/broadcast", json={"body": body}, headers=h)


def api(hub, agent, method, path, **kw):
    return hub.client.request(method, f"/api/v1{path}", headers=agent.headers, **kw)


def deliveries(hub, agent):
    r = api(hub, agent, "GET", "/chats/deliveries")
    assert r.status_code == 200, r.text
    return r.json()["deliveries"]


def by_name(result):
    return {r["name"]: r for r in result["recipients"]}


@pytest.fixture
def box(hub):
    """alice (project proj) and bob (project other) share one machine; carol runs on another; dave, on the shared
    machine, waits for approval."""
    alice = enroll_on(hub, "alice")
    bob = enroll_on(hub, "bob", projects=("other",))
    carol = hub.enroll("carol")
    dave = enroll_on(hub, "dave", approve=False)
    online(hub, alice, bob, carol)
    return hub, alice, bob, carol, dave


# --- schema ------------------------------------------------------------------------------------------------------


def test_migration_widens_chats_with_device_and_keeps_every_row(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    newest = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", newest[:-1])
    conn = db.init_db(path)  # the database before this migration, with a chat and a message in it
    with db.transaction(conn):
        store.ensure_project(conn, "proj", actor=OPERATOR)
        chat_id = conn.execute("INSERT INTO chats (kind, ref, project, session_id, operator_read_id, created_at) "
                               "VALUES ('project', 'proj', 'proj', 's1', 7, 'x')").lastrowid
        conn.execute("INSERT INTO chat_messages (chat_id, author, kind, body, created_at, updated_at) "
                     "VALUES (?, ?, 'text', 'kept', 'x', 'x')", (chat_id, OPERATOR))
        with pytest.raises(Exception, match="CHECK"):
            conn.execute("INSERT INTO chats (kind, ref, created_at) VALUES ('device', 'n1', 'x')")
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS", newest)
    conn = db.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(newest)
    assert dict(conn.execute("SELECT id, kind, ref, session_id, operator_read_id FROM chats").fetchone()) == {
        "id": chat_id, "kind": "project", "ref": "proj", "session_id": "s1", "operator_read_id": 7}
    assert conn.execute("SELECT body FROM chat_messages WHERE chat_id = ?", (chat_id,)).fetchone()[0] == "kept"
    with db.transaction(conn):
        conn.execute("INSERT INTO chats (kind, ref, created_at) VALUES ('device', 'n1', 'x')")
        with pytest.raises(Exception, match="UNIQUE"):
            conn.execute("INSERT INTO chats (kind, ref, created_at) VALUES ('device', 'n1', 'y')")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- delivery and waking -----------------------------------------------------------------------------------------


def test_one_message_reaches_every_active_agent_on_the_device_across_projects(box):
    hub, alice, bob, carol, dave = box
    r = broadcast(hub)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sent"] == 2 and out["woke"] == 2
    assert out["device"] == {"node_id": BOX, "name": BOX_NAME}
    assert [x["name"] for x in out["recipients"]] == ["alice", "bob", "dave"]  # carol is on another machine
    got = by_name(out)
    assert got["alice"] | {"delivery_id": None} == {"agent_id": alice.id, "name": "alice", "delivered": True,
                                                     "woke": True, "delivery_id": None, "reason": None}
    assert got["bob"]["delivered"] and got["bob"]["woke"]
    assert got["dave"] == {"agent_id": dave.id, "name": "dave", "delivered": False, "woke": False,
                           "delivery_id": None, "reason": "pending"}
    # one message, in the device's chat, by the operator
    conn = hub.conn()
    (message,) = chats.messages(conn, "device", BOX)
    assert message["id"] == out["message_id"] and message["author"] == OP_ACTOR and message["body"] == SECRET
    assert sorted(d["name"] for d in message["deliveries"]) == ["alice", "bob"]
    # each agent's runner gets it, as a message to its own chat session that may wake it
    for agent in (alice, bob):
        (d,) = deliveries(hub, agent)
        assert d["id"] == got[agent.name]["delivery_id"] and d["message_id"] == out["message_id"]
        assert d["verified"] is True and d["wake"] is True and d["addressed"] is True and d["body"] == SECRET
        assert d["chat"]["kind"] == "device" and d["chat"]["ref"] == BOX and d["chat"]["project"] is None
        assert d["chat"]["device"] == {"node_id": BOX, "name": BOX_NAME}
        assert "every agent on your device" in d["chat"]["title"]
        assert Delivery.from_hub(d).target == "chat"  # its own chat session, whatever its projects
    assert deliveries(hub, carol) == []
    # its runner wakes it; the record says the operator woke it, in no chain
    assert api(hub, alice, "POST", f"/chats/deliveries/{got['alice']['delivery_id']}",
               json={"mode": "woke"}).status_code == 200
    (record,) = wakes.records(conn, agent_id=alice.id)
    assert record["operator"] and record["mode"] == "woke" and record["chain_id"] is None
    assert record["chat"] == {"kind": "device", "ref": BOX, "title": f"device {BOX_NAME}"}


def test_the_chat_deliver_events_name_each_agent_and_carry_no_text(box):
    hub, alice, bob, _, _ = box
    before = hub.conn().execute("SELECT MAX(id) FROM events").fetchone()[0]
    out = broadcast(hub).json()
    rows = hub.conn().execute("SELECT * FROM events WHERE id > ? ORDER BY id", (before,)).fetchall()
    found = [events.row_to_event(r) for r in rows]
    assert {e["payload"]["agent_id"] for e in found if e["type"] == "chat.deliver"} == {alice.id, bob.id}
    assert all(e["project"] is None and e["payload"]["message_id"] == out["message_id"] for e in found)
    assert SECRET not in str(found)
    assert hub.conn().execute("SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'staging'").fetchone()[0] == 0


def test_a_paused_agent_gets_it_without_waking_until_the_pause_lifts(box):
    hub, _, bob, _, _ = box
    conn = hub.conn()
    with db.transaction(conn):
        wakes.set_agent_paused(conn, "bob", True, actor=OPERATOR)
    out = broadcast(hub).json()
    got = by_name(out)
    assert got["bob"] | {"delivery_id": None} == {"agent_id": bob.id, "name": "bob", "delivered": True,
                                                   "woke": False, "delivery_id": None, "reason": "paused"}
    assert got["alice"]["woke"] and out["sent"] == 2 and out["woke"] == 1
    (d,) = deliveries(hub, bob)
    assert d["wake"] is False
    r = api(hub, bob, "POST", f"/chats/deliveries/{d['id']}", json={"mode": "woke"})
    assert r.status_code == 409 and r.json()["wake"] is False
    # a running session may still take it in
    r = api(hub, bob, "POST", f"/chats/deliveries/{d['id']}", json={"mode": "live"})
    assert r.status_code == 200 and r.json()["mode"] == "live"


def test_lifting_a_pause_tells_the_runner_again(box):
    hub, _, bob, _, _ = box
    conn = hub.conn()
    with db.transaction(conn):
        wakes.set_agent_paused(conn, "bob", True, actor=OPERATOR)
    out = broadcast(hub).json()
    before = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
    with db.transaction(conn):
        wakes.set_agent_paused(conn, "bob", False, actor=OPERATOR)
    again = [events.row_to_event(r) for r in conn.execute(
        "SELECT * FROM events WHERE type = 'chat.deliver' AND id > ?", (before,))]
    assert [(e["payload"]["agent_id"], e["payload"]["message_id"]) for e in again] == [(bob.id, out["message_id"])]
    (d,) = deliveries(hub, bob)
    assert d["wake"] is True
    assert api(hub, bob, "POST", f"/chats/deliveries/{d['id']}", json={"mode": "woke"}).status_code == 200


def test_the_hubs_pause_holds_a_device_message_too_but_not_the_operators_others(box):
    hub, alice, _, _, _ = box
    conn = hub.conn()
    with db.transaction(conn):
        wakes.set_hub_paused(conn, True, actor=OPERATOR)
    out = broadcast(hub).json()
    assert out["woke"] == 0 and {r["reason"] for r in out["recipients"] if r["delivered"]} == {"paused"}
    with db.transaction(conn):
        chats.send(conn, "agent", alice.id, "a word in your own chat", OPERATOR)
    mine = {d["chat"]["kind"]: d["wake"] for d in deliveries(hub, alice)}
    assert mine == {"device": False, "agent": True}  # the operator's own chat still wakes


def test_offline_old_runner_and_busy_agents_are_reported(hub):
    alice, bob, erin, fay = (enroll_on(hub, n) for n in ("alice", "bob", "erin", "fay"))
    online(hub, bob, chat=0)  # a runner before 0.3.8
    online(hub, erin, fay)
    conn = hub.conn()
    with db.transaction(conn):  # erin's session is running
        chats.stream(conn, store.get_agent(conn, erin.id), "agent", "me", [{"type": "status", "state": "working"}])
    out = broadcast(hub).json()
    assert {n: (r["delivered"], r["woke"], r["reason"]) for n, r in by_name(out).items()} == {
        "alice": (True, False, "offline"), "bob": (True, False, "pending_runner"),
        "erin": (True, False, "busy_queued"), "fay": (True, True, None)}
    assert out["sent"] == 4 and out["woke"] == 1
    for agent in (alice, bob, erin):  # each waits for its runner as it is
        (d,) = deliveries(hub, agent)
        assert d["wake"] is True and d["status"] == "pending"


def test_revoked_agents_are_listed_but_get_nothing(box):
    hub, alice, bob, _, _ = box
    hub.set_status(bob, "revoked")
    out = broadcast(hub).json()
    assert by_name(out)["bob"] == {"agent_id": bob.id, "name": "bob", "delivered": False, "woke": False,
                                   "delivery_id": None, "reason": "revoked"}
    assert out["sent"] == 1 and deliveries(hub, alice)


# --- errors and access -------------------------------------------------------------------------------------------


def test_unknown_device_revoked_only_device_and_no_active_agent(box):
    hub, _, _, carol, _ = box
    r = broadcast(hub, node="nowhere")
    assert r.status_code == 404 and "no device" in r.json()["detail"]
    hub.set_status(carol, "revoked")  # carol's machine has only a revoked agent now
    assert broadcast(hub, node="ncarol").status_code == 404
    gina = enroll_on(hub, "gina", node="nlone", device="lone.example.ts.net", approve=False)
    r = broadcast(hub, node="nlone")
    assert r.status_code == 409 and "no active agent" in r.json()["detail"]
    assert chats.messages(hub.conn(), "device", "nlone") == []  # nothing is stored
    assert gina


def test_an_empty_message_is_refused(box):
    hub = box[0]
    for body in ("", "   \n"):
        r = broadcast(hub, body=body)
        assert r.status_code == 422, r.text
    with pytest.raises(store.Invalid):
        broadcasts.send(hub.conn(), BOX, " ", actor=OPERATOR)


def test_the_operators_message_has_no_length_limit(box):
    hub = box[0]
    long = "x" * (chats.MAX_BODY_CHARS * 2)
    assert broadcast(hub, body=long).status_code == 200


def test_only_the_operator_broadcasts(box):
    hub, alice, _, _, _ = box
    for method in ("POST", "GET"):
        r = hub.client.request(method, f"/ui/api/devices/{BOX}/broadcast", json={"body": "x"}, headers=alice.headers)
        assert r.status_code == 403, method
        assert r.headers["content-type"].startswith("application/json")
    # an agent token beside the operator's identity is refused as everywhere under /ui
    r = hub.client.post(f"/ui/api/devices/{BOX}/broadcast", json={"body": "x"},
                        headers={**hub.op_headers(**{"x-csrf-token": hub.csrf()}), "authorization": alice.headers[
                            "authorization"]})
    assert r.status_code in (401, 403)
    # without the CSRF token, not even the operator
    r = hub.client.post(f"/ui/api/devices/{BOX}/broadcast", json={"body": "x"}, headers=hub.op_headers())
    assert r.status_code == 403
    assert chats.messages(hub.conn(), "device", BOX) == []
    with pytest.raises(store.Forbidden):
        broadcasts.send(hub.conn(), BOX, "x", actor=alice.id)
    with pytest.raises(store.Forbidden):
        chats.send(hub.conn(), "device", BOX, "x", alice.id)


def test_no_agent_writes_in_a_device_chat(box):
    hub, alice, _, _, _ = box
    broadcast(hub)
    for path, body in (("/chats/device/me/messages", {"body": "hi all"}),
                       (f"/chats/device/{BOX}/messages", {"body": "hi all"}),
                       ("/chats/device/me/stream", {"items": [{"type": "text", "text": "forged"}]}),
                       ("/chats/device/me/runs", {"outcome": "success", "seconds": 1})):
        r = api(hub, alice, "POST", path, json=body)
        assert r.status_code == 403, (path, r.text)
    assert [m["author"] for m in chats.messages(hub.conn(), "device", BOX)] == [OP_ACTOR]


# --- who reads it ------------------------------------------------------------------------------------------------


def test_the_agents_on_the_device_read_its_chat_and_nobody_else(box):
    hub, alice, bob, carol, _ = box
    out = broadcast(hub).json()
    for agent in (alice, bob):
        for ref in ("me", BOX):
            r = api(hub, agent, "GET", f"/chats/device/{ref}/messages")
            assert r.status_code == 200, r.text
            assert [m["body"] for m in r.json()["messages"]] == [SECRET]
            assert r.json()["chat"]["title"] == f"device {BOX_NAME}"
    assert api(hub, carol, "GET", f"/chats/device/{BOX}/messages").status_code == 404
    r = api(hub, carol, "GET", "/chats/device/me/messages")  # its own machine's: nothing for it here
    assert r.status_code == 200 and r.json()["messages"] == []
    # read_inbox: the operator's message, from the device chat; not carol's
    entries = api(hub, alice, "GET", "/inbox").json()["messages"]
    (entry,) = [e for e in entries if e["id"] == out["message_id"]]
    assert entry["kind"] == "operator" and entry["chat"] == f"device:{BOX}" and entry["body"] == SECRET
    assert all(e["id"] != out["message_id"] for e in api(hub, carol, "GET", "/inbox").json()["messages"])
    # never in anyone's search
    for agent in (alice, carol):
        r = api(hub, agent, "GET", "/search", params={"q": "staging"})
        assert r.status_code == 200 and r.json()["results"] == []


def test_the_operator_reads_the_device_chat_with_its_deliveries(box):
    hub, alice, _, _, _ = box
    out = broadcast(hub).json()
    api(hub, alice, "POST", f"/chats/deliveries/{by_name(out)['alice']['delivery_id']}", json={"mode": "woke"})
    r = hub.client.get(f"/ui/api/devices/{BOX}/broadcast", headers=hub.op_headers())
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["device"] == {"node_id": BOX, "name": BOX_NAME}
    assert sorted(m["name"] for m in got["members"]) == ["alice", "bob"]
    (m,) = got["messages"]
    assert m["id"] == out["message_id"] and m["body"] == SECRET
    assert {d["name"]: (d["status"], d["mode"]) for d in m["deliveries"]} == {
        "alice": ("delivered", "woke"), "bob": ("pending", None)}
    assert hub.client.get("/ui/api/devices/nowhere/broadcast", headers=hub.op_headers()).status_code == 404


def test_the_device_board_and_post_device_are_unchanged(box):
    hub, alice, bob, _, _ = box
    broadcast(hub)
    r = api(hub, alice, "POST", "/device/messages", json={"body": "note for the box"})
    assert r.status_code in (200, 201), r.text
    notes = api(hub, bob, "GET", "/inbox").json()["messages"]
    assert [n["body"] for n in notes if n["kind"] == "device"] == ["note for the box"]
    assert [m["author"] for m in chats.messages(hub.conn(), "device", BOX)] == [OP_ACTOR]  # not in the device chat


# --- the runner and the agent's tools ----------------------------------------------------------------------------


def device_delivery(**kw):
    fields = {"id": 4, "message_id": 12, "kind": "device", "ref": BOX, "project": None, "task_id": None,
              "author": OPERATOR, "author_name": None, "body": "Pull main and rebuild.", "verified": True,
              "device": "shared-box"}
    return Delivery(**(fields | kw))


def test_a_device_message_is_read_and_answered_in_the_agents_own_chat():
    d = device_delivery()
    assert d.target == "chat" and d.spec == "me" and d.answered_in == "agent"
    text = delivery_text(d)
    assert text.startswith("From the operator (chat message #12 in the chat of your device shared-box, to every "
                           "agent on it, verified by the hub):\nPull main and rebuild.")
    assert "Answer here: what you write in this session shows in your chat with the operator." in text
    mid = delivery_text(d, session_task=7, mid_turn=True)
    assert "shows in your chat with the operator" in mid and "carry on with task #7" in mid
    row = {"id": 4, "message_id": 12, "author": OPERATOR, "body": "x", "verified": True,
           "chat": {"kind": "device", "ref": BOX, "device": {"node_id": BOX, "name": "shared-box"}}}
    assert Delivery.from_hub(row).device == "shared-box"


async def test_a_task_sessions_answer_to_a_device_message_shows_in_the_agents_own_chat():
    client, sent = _recording_client()
    out = SessionOutput(client, "task", "5", task_id=5, flush_seconds=0.01)
    await out.on_replay(device_delivery())
    await out.on_progress({"kind": "text", "text": "Rebuilding now."})
    await out.close()
    by_chat: dict[str, list] = {}
    for request in sent:
        by_chat.setdefault(request["path"], []).extend(request["items"])
    assert by_chat == {"/api/v1/chats/agent/me/stream": [{"type": "status", "state": "working"},
                                                         {"type": "text", "text": "Rebuilding now."}]}


async def test_a_device_message_wakes_an_idle_agent_into_its_chat_session(live_hub, fake_claude, tmp_path):
    claude_bin, calls = fake_claude
    runner, agent_cfg = chat_runner(live_hub, tmp_path, claude_bin)
    me = agent_cfg.agent_id
    node = live_hub.conn().execute("SELECT device_node_id FROM agents WHERE id = ?", (me,)).fetchone()[0]
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(lambda: live_hub.conn().execute(
            "SELECT connected AND chat_support FROM agents WHERE id = ?", (me,)).fetchone()[0] == 1)
        conn = live_hub.conn()
        with db.transaction(conn):
            out = broadcasts.send(conn, node, "Everyone: pull main.", actor=OPERATOR)
        await wait_for(lambda: "finished" in states(live_hub, "agent", me))
    finally:
        await stop(main)
    assert out["sent"] == 1
    (call,) = calls()
    assert f"From the operator (chat message #{out['message_id']} in the chat of your device" in call["stdin"]
    assert "Everyone: pull main." in call["stdin"]
    assert call["env"]["AGENTHUB_CHAT"] == "agent/me" and "AGENTHUB_TASK_ID" not in call["env"]
    (d,) = chats.get_message(live_hub.conn(), out["message_id"])["deliveries"]
    assert d["mode"] == "woke"
    assert texts(live_hub, "agent", me) == ["Looking at the code now."]  # its answer, in its own chat
    assert [m["kind"] for m in chats.messages(live_hub.conn(), "device", node)] == ["text"]


def test_mcp_names_the_device_chat_and_heads_the_operators_message():
    assert mcp_server._chat_spec("device") == ("device", "me")
    assert mcp_server._chat_spec("device:nbox") == ("device", "nbox")
    heading = mcp_server._operator_heading({"kind": "operator", "chat": "device:nbox", "id": 12})
    assert "every agent on your device" in heading and "answer in chat 'me'" in heading
    assert "chat 'device'" in mcp_server.INSTRUCTIONS
