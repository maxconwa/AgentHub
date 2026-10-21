from datetime import UTC, datetime, timedelta

import pytest

from agenthub.hub import db, store
from agenthub.hub.store import Device

from .conftest import OPERATOR

DEVICE = Device(node_id="nX", name="x.example.ts.net", ip="100.64.0.99")


def _code(conn, now=None, projects=("proj",)):
    with db.transaction(conn):
        return store.create_enrollment(conn, created_by=OPERATOR, projects=list(projects), now=now)


def _redeem(conn, code, name="a1", now=None):
    with db.transaction(conn):
        return store.redeem_enrollment(conn, code=code, name=name, device=DEVICE, now=now)


def test_only_code_hash_is_stored(hub):
    conn = hub.conn()
    code = _code(conn)
    dump = "\n".join(conn.iterdump())
    assert code not in dump and store.security.normalize_code(code) not in dump


def test_code_expires_after_15_minutes(hub):
    conn = hub.conn()
    t0 = datetime(2030, 1, 1, tzinfo=UTC)
    code = _code(conn, now=t0)
    with pytest.raises(store.Forbidden):
        _redeem(conn, code, now=t0 + timedelta(minutes=15, seconds=1))
    # still valid just before expiry
    agent, _ = _redeem(conn, code, now=t0 + timedelta(minutes=14, seconds=59))
    assert agent.status == "pending"


def test_code_is_single_use(hub):
    conn = hub.conn()
    code = _code(conn)
    _redeem(conn, code, name="first")
    with pytest.raises(store.Forbidden):
        _redeem(conn, code, name="second")


def test_failed_redeem_for_taken_name_does_not_burn_code(hub):
    conn = hub.conn()
    _redeem(conn, _code(conn), name="taken")
    code = _code(conn)
    with pytest.raises(store.Conflict):
        _redeem(conn, code, name="taken")
    agent, _ = _redeem(conn, code, name="other")
    assert agent.name == "other"


def test_api_enroll_single_use_and_uniform_error(hub):
    code = hub.code()
    ip = hub.new_ip()
    hub.whois.add(ip, "n1", "one.example.ts.net")
    first = hub.client.post("/api/v1/enroll", json={"code": code, "name": "one"}, headers={"x-forwarded-for": ip})
    assert first.status_code == 201
    again = hub.client.post("/api/v1/enroll", json={"code": code, "name": "two"}, headers={"x-forwarded-for": ip})
    bogus = hub.client.post(
        "/api/v1/enroll", json={"code": "AAAA-BBBB-CCCC", "name": "two"}, headers={"x-forwarded-for": ip}
    )
    assert again.status_code == bogus.status_code == 403
    assert again.json()["detail"] == bogus.json()["detail"]


def test_enroll_records_whois_device_and_projects(hub):
    agent = hub.enroll("alpha", projects=("p1", "p2"), approve=False)
    row = store.get_agent(hub.conn(), agent.id)
    assert row.device_node_id == "nalpha"
    assert row.device_name == "alpha-host.example.ts.net"
    assert row.device_ip == agent.ip
    assert row.allowed_projects == ("p1", "p2")
    assert row.status == "pending"


def test_enroll_requires_tailnet_identity(hub):
    code = hub.code()
    no_xff = hub.client.post("/api/v1/enroll", json={"code": code, "name": "x"})
    unknown = hub.client.post(
        "/api/v1/enroll", json={"code": code, "name": "x"}, headers={"x-forwarded-for": "100.64.9.9"}
    )
    assert no_xff.status_code == unknown.status_code == 403
    # the code was not consumed by the failed attempts
    hub.enroll("x")


def test_pending_agent_can_only_read_own_status(hub):
    agent = hub.enroll("pend", approve=False)
    me = hub.client.get("/api/v1/agents/me", headers=agent.headers)
    assert me.status_code == 200 and me.json()["status"] == "pending"
    for method, path in [("GET", "/api/v1/projects"), ("GET", "/api/v1/inbox"),
                         ("POST", "/api/v1/projects/proj/messages")]:
        resp = hub.client.request(method, path, headers=agent.headers, json={"body": "hi"})
        assert resp.status_code == 403, path
    hub.set_status(agent, "active")
    assert hub.client.get("/api/v1/projects", headers=agent.headers).status_code == 200


def test_enroll_emits_events(hub):
    agent = hub.enroll("ev")
    types = [r["type"] for r in hub.conn().execute("SELECT type FROM events ORDER BY id")]
    assert types[:3] == ["project.created", "enrollment.created", "agent.enrolled"]
    assert "agent.approved" in types
    actors = [r["actor"] for r in hub.conn().execute("SELECT actor FROM events WHERE type='agent.enrolled'")]
    assert actors == [agent.id]
