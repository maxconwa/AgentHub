import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agenthub import security
from agenthub.hub import bus, db, events, store

from .conftest import OPERATOR


def ws(hub, headers):
    return hub.client.websocket_connect("/api/v1/ws", headers=headers)


def test_websocket_requires_valid_token_and_device(hub):
    a = hub.enroll("a")
    b = hub.enroll("b")
    for headers in [{"x-forwarded-for": a.ip},
                    {"authorization": f"Bearer {security.new_token(a.id)}", "x-forwarded-for": a.ip},
                    {"authorization": f"Bearer {a.token}", "x-forwarded-for": b.ip},
                    {"authorization": f"Bearer {a.token}"}]:
        with pytest.raises(WebSocketDisconnect), ws(hub, headers) as conn:
            conn.receive_json()


def test_pending_agent_cannot_open_websocket(hub):
    a = hub.enroll("a", approve=False)
    with pytest.raises(WebSocketDisconnect), ws(hub, a.headers) as conn:
        conn.receive_json()


def test_hello_and_heartbeat(hub):
    a = hub.enroll("a")
    with ws(hub, a.headers) as conn:
        hello = conn.receive_json()
        assert hello["type"] == "hello" and hello["agent"]["id"] == a.id
        conn.send_json({"type": "heartbeat"})
        assert conn.receive_json()["type"] == "heartbeat_ack"
    row = hub.conn().execute("SELECT last_heartbeat, connected FROM agents WHERE id = ?", (a.id,)).fetchone()
    assert row["last_heartbeat"] and row["connected"] == 0
    types = [r["type"] for r in hub.conn().execute("SELECT type FROM events WHERE actor = ?", (a.id,))]
    assert "agent.connected" in types and "agent.disconnected" in types


def test_revoked_agent_heartbeat_closes_socket(hub):
    a = hub.enroll("a")
    with ws(hub, a.headers) as conn:
        conn.receive_json()
        hub.set_status(a, "revoked")
        conn.send_json({"type": "heartbeat"})
        with pytest.raises(WebSocketDisconnect) as info:
            conn.receive_json()
    assert info.value.code == bus.CLOSE_REVOKED


def test_bus_pushes_events_and_closes_on_revoke(tmp_path):
    from .conftest import make_hub

    hub = make_hub(tmp_path)
    a, b = hub.enroll("a"), hub.enroll("b")
    hub.state.bus.poll_seconds = 0.02
    with TestClient(hub.client.app, client=("127.0.0.1", 40000)) as live:  # runs lifespan: starts the bus
        with live.websocket_connect("/api/v1/ws", headers=b.headers) as conn:
            assert conn.receive_json()["type"] == "hello"
            live.post("/api/v1/projects/proj/messages", json={"body": "hi b", "to": "b"}, headers=a.headers)
            msg = conn.receive_json()
            if msg["event"]["type"] == "channel.created":  # the first post also creates the channel
                msg = conn.receive_json()
            assert msg["event"]["type"] == "message.posted"
            assert msg["event"]["data"]["addressed_to_me"] is True
            assert "body" not in json.dumps(msg["event"]["data"])  # notifications carry no content
            hub.set_status(b, "revoked")  # written straight to the DB, like the operator CLI
            with pytest.raises(WebSocketDisconnect) as info:
                conn.receive_json()
            assert info.value.code == bus.CLOSE_REVOKED


def _event(hub, etype, project, actor, payload):
    conn = hub.conn()
    with db.transaction(conn):
        event_id = events.emit(conn, type=etype, project=project, actor=actor, payload=payload)
    return events.row_to_event(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())


def test_routing_is_scoped(hub):
    a = hub.enroll("a", projects=("proj",))
    b = hub.enroll("b", projects=("proj",))
    c = hub.enroll("c", projects=("other",))
    conn = hub.conn()
    assert bus.recipients(_event(hub, "message.posted", "proj", a.id, {"to": None}), conn) == {b.id}
    assert bus.recipients(_event(hub, "board.updated", "other", c.id, {}), conn) == set()
    added = _event(hub, "task.agent_added", "proj", OPERATOR, {"task_id": 1, "agent_id": a.id, "agents": [a.id, b.id]})
    assert bus.recipients(added, conn) == {a.id, b.id}
    assert bus.recipients(_event(hub, "task.assigned", "proj", OPERATOR, {"agent_id": b.id, "agents": [b.id]}),
                          conn) == {b.id}
    assert bus.recipients(_event(hub, "task.transitioned", "proj", OPERATOR, {"agents": []}), conn) == set()
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "rm -rf x", "cwd": "/"},
                         headers=a.headers).json()
    decided = _event(hub, "approval.decided", None, OPERATOR, {"approval_id": ap["id"]})
    assert bus.recipients(decided, conn) == {a.id}
    assert bus.recipients(_event(hub, "agent.enrolled", None, a.id, {}), conn) == set()


def test_connection_limit_per_agent(hub):
    a = hub.enroll("a")
    sockets = [ws(hub, a.headers) for _ in range(bus.MAX_SOCKETS_PER_AGENT)]
    opened = [s.__enter__() for s in sockets]
    for s in opened:
        s.receive_json()
    with pytest.raises(WebSocketDisconnect), ws(hub, a.headers) as extra:
        extra.receive_json()
    for s in sockets:
        s.__exit__(None, None, None)


def test_store_unchanged_by_revoked_reactivation(hub):
    a = hub.enroll("a")
    hub.set_status(a, "revoked")
    with pytest.raises(store.Conflict):
        hub.set_status(a, "active")


def _agent_dirs(hub, agent_id):
    row = hub.conn().execute("SELECT checkouts, current_dir FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return json.loads(row["checkouts"]), row["current_dir"]


def _beat(hub, a, *frames):
    """Send each frame as a heartbeat and wait for its ack."""
    with ws(hub, a.headers) as conn:
        conn.receive_json()
        for frame in frames:
            conn.send_json({"type": "heartbeat", **frame})
            assert conn.receive_json()["type"] == "heartbeat_ack"


def test_heartbeat_stores_checkouts_and_current_dir(hub):
    a = hub.enroll("a")
    assert _agent_dirs(hub, a.id) == ({}, None)
    _beat(hub, a, {"checkouts": {"web": "/src/web", "api": "/src/api"}, "current_dir": "/src/web"})
    assert _agent_dirs(hub, a.id) == ({"web": "/src/web", "api": "/src/api"}, "/src/web")
    raw = hub.conn().execute("SELECT checkouts FROM agents WHERE id = ?", (a.id,)).fetchone()[0]
    assert " " not in raw.replace("/src/web", "").replace("/src/api", "")  # compact JSON
    _beat(hub, a, {"checkouts": {"web": "/src/web"}, "current_dir": None})
    assert _agent_dirs(hub, a.id) == ({"web": "/src/web"}, None)


def test_heartbeat_is_one_update_and_rewrites_dirs_only_when_they_change(hub):
    a = hub.enroll("a")
    hub.conn().executescript(
        "CREATE TABLE agent_writes (dirs INTEGER);"
        "CREATE TRIGGER any_write AFTER UPDATE ON agents BEGIN INSERT INTO agent_writes VALUES (0); END;"
        "CREATE TRIGGER dir_write AFTER UPDATE OF checkouts, current_dir ON agents "
        "BEGIN INSERT INTO agent_writes VALUES (1); END;")

    def writes():
        rows = [r[0] for r in hub.conn().execute("SELECT dirs FROM agent_writes")]
        hub.conn().execute("DELETE FROM agent_writes")
        return rows.count(0), rows.count(1)  # (UPDATE statements, ones that set the directories)

    dirs = {"checkouts": {"web": "/w"}, "current_dir": "/w"}
    with ws(hub, a.headers) as conn:
        conn.receive_json()
        writes()  # connecting
        for frame, expected in ((dirs, (1, 1)), (dirs, (1, 0)), ({}, (1, 0)),
                                ({"checkouts": {"web": "/w"}, "current_dir": None}, (1, 1))):
            conn.send_json({"type": "heartbeat", **frame})
            assert conn.receive_json()["type"] == "heartbeat_ack"
            assert writes() == expected, frame
    assert _agent_dirs(hub, a.id) == ({"web": "/w"}, None)


def test_old_style_heartbeat_keeps_stored_checkouts(hub):
    a = hub.enroll("a")
    _beat(hub, a, {"checkouts": {"web": "/w"}, "current_dir": "/w"}, {})
    assert _agent_dirs(hub, a.id) == ({"web": "/w"}, "/w")


@pytest.mark.parametrize("bad", [
    {"checkouts": {f"p{i}": "/x" for i in range(51)}, "current_dir": "/new"},
    {"checkouts": {"web": "/" + "x" * 500}, "current_dir": "/new"},
    {"checkouts": {"web": 5}, "current_dir": "/new"},
    {"checkouts": {"Bad Name": "/x"}, "current_dir": "/new"},
    {"checkouts": {"web": "/new"}, "current_dir": "/" + "x" * 500},
    {"checkouts": {"web": "/new"}, "current_dir": 7},
    {"checkouts": ["web"], "current_dir": "/new"},
])
def test_invalid_checkouts_keep_old_values_but_count_as_heartbeat(hub, bad):
    a = hub.enroll("a")
    _beat(hub, a, {"checkouts": {"web": "/w"}, "current_dir": "/w"})
    with ws(hub, a.headers) as conn:
        conn.receive_json()
        hub.conn().execute("UPDATE agents SET last_heartbeat = NULL WHERE id = ?", (a.id,))
        conn.send_json({"type": "heartbeat", **bad})
        assert conn.receive_json()["type"] == "heartbeat_ack"
        assert hub.conn().execute("SELECT last_heartbeat FROM agents WHERE id = ?", (a.id,)).fetchone()[0]
    assert _agent_dirs(hub, a.id) == ({"web": "/w"}, "/w")


def test_exactly_50_checkouts_and_500_char_paths_are_accepted(hub):
    a = hub.enroll("a")
    full = {f"p{i}": "/" + "x" * 499 for i in range(50)}
    _beat(hub, a, {"checkouts": full, "current_dir": "/" + "y" * 499})
    assert _agent_dirs(hub, a.id) == (full, "/" + "y" * 499)
