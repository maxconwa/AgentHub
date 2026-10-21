from datetime import UTC, datetime, timedelta

import pytest

from agenthub.hub import approvals, db, store

from .conftest import OPERATOR

CMD = {"command": "git push --force origin feature", "cwd": "/work/repo"}


def request_cmd(hub, agent, **overrides):
    return hub.client.post("/api/v1/approvals/command", json={**CMD, **overrides}, headers=agent.headers)


def consume(hub, agent, **overrides):
    return hub.client.post("/api/v1/approvals/command/consume", json={**CMD, **overrides}, headers=agent.headers)


def decide(hub, approval_id, approve=True, actor=OPERATOR):
    conn = hub.conn()
    with db.transaction(conn):
        return approvals.decide(conn, approval_id, approve=approve, actor=actor)


def test_approved_command_is_allowed_exactly_once(hub):
    a = hub.enroll("a")
    ap = request_cmd(hub, a).json()
    assert ap["status"] == "pending"
    assert consume(hub, a).json() == {"allowed": False, "approval_id": None}
    decide(hub, ap["id"])
    assert consume(hub, a).json() == {"allowed": True, "approval_id": ap["id"]}
    assert consume(hub, a).json()["allowed"] is False


def test_rejected_command_is_never_allowed(hub):
    a = hub.enroll("a")
    ap = request_cmd(hub, a).json()
    decide(hub, ap["id"], approve=False)
    assert consume(hub, a).json()["allowed"] is False


@pytest.mark.parametrize("override", [{"command": "git push --force origin other"}, {"cwd": "/elsewhere"},
                                      {"command": CMD["command"] + " "}])
def test_approval_binds_exact_command_and_cwd(hub, override):
    a = hub.enroll("a")
    decide(hub, request_cmd(hub, a).json()["id"])
    assert consume(hub, a, **override).json()["allowed"] is False
    assert consume(hub, a).json()["allowed"] is True


def test_approval_binds_requesting_agent(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    decide(hub, request_cmd(hub, a).json()["id"])
    assert consume(hub, b).json()["allowed"] is False
    assert consume(hub, a).json()["allowed"] is True


def test_approved_command_expires(hub):
    a = hub.enroll("a")
    decide(hub, request_cmd(hub, a).json()["id"])
    conn = hub.conn()
    agent = store.get_agent(conn, a.id)
    later = datetime.now(UTC) + approvals.COMMAND_APPROVAL_TTL + timedelta(seconds=1)
    with db.transaction(conn):
        assert approvals.consume_command(conn, agent, now=later, **CMD) is None


def test_pending_command_requests_are_deduplicated(hub):
    a = hub.enroll("a")
    first, second = request_cmd(hub, a).json(), request_cmd(hub, a).json()
    assert first["id"] == second["id"]
    decide(hub, first["id"])
    consume(hub, a)
    assert request_cmd(hub, a).json()["id"] != first["id"]  # used up: a new request is needed


def test_only_humans_can_decide(hub):
    a = hub.enroll("a")
    ap = request_cmd(hub, a).json()
    with pytest.raises(store.Forbidden):
        decide(hub, ap["id"], actor=a.id)


def test_decisions_are_final(hub):
    a = hub.enroll("a")
    ap = request_cmd(hub, a).json()
    decide(hub, ap["id"])
    with pytest.raises(store.Conflict):
        decide(hub, ap["id"], approve=False)


def test_no_agent_endpoint_can_decide(hub):
    a = hub.enroll("a")
    ap = request_cmd(hub, a).json()
    for path in [f"/api/v1/approvals/{ap['id']}/approve", f"/api/v1/approvals/{ap['id']}/decide",
                 f"/api/v1/approvals/{ap['id']}"]:
        for method in ("POST", "PUT", "PATCH"):
            r = hub.client.request(method, path, headers=a.headers, json={"status": "approved"})
            assert r.status_code in (404, 405), (method, path)
    assert hub.client.get(f"/api/v1/approvals/{ap['id']}", headers=a.headers).json()["status"] == "pending"


@pytest.mark.parametrize("kind", ["task_create", "agent_enrollment", "nonsense"])
def test_agents_cannot_file_privileged_kinds(hub, kind):
    a = hub.enroll("a")
    r = hub.client.post("/api/v1/approvals", json={"kind": kind, "details": {"agent_id": a.id}}, headers=a.headers)
    assert r.status_code in (403, 422)


def test_agents_see_only_their_own_approvals(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    ap = request_cmd(hub, a).json()
    assert hub.client.get(f"/api/v1/approvals/{ap['id']}", headers=b.headers).status_code == 404


def test_budget_exceeded_only_by_assignee(hub):
    from agenthub.hub import tasks

    a, b = hub.enroll("a"), hub.enroll("b")
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="t", assignee="a")
    body = {"kind": "budget_exceeded", "details": {"task_id": t["id"], "limit": "max_turns"}, "reason": "turns"}
    assert hub.client.post("/api/v1/approvals", json=body, headers=b.headers).status_code == 403
    assert hub.client.post("/api/v1/approvals", json=body, headers=a.headers).status_code == 201


def test_enrollment_files_an_approval_and_deciding_it_activates(hub):
    a = hub.enroll("a", approve=False)
    pending = approvals.pending_enrollment(hub.conn(), a.id)
    assert pending and pending["details"]["name"] == "a"
    decide(hub, pending["id"])
    assert store.get_agent(hub.conn(), a.id).status == "active"


def test_rejecting_enrollment_revokes(hub):
    a = hub.enroll("a", approve=False)
    decide(hub, approvals.pending_enrollment(hub.conn(), a.id)["id"], approve=False)
    assert store.get_agent(hub.conn(), a.id).status == "revoked"


def test_command_size_limit(hub):
    a = hub.enroll("a")
    assert request_cmd(hub, a, command="x" * 8001).status_code == 422
