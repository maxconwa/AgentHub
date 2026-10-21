import pytest

from agenthub import security

ME = "/api/v1/agents/me"


def test_missing_and_malformed_tokens_rejected(hub):
    agent = hub.enroll("a")
    xff = {"x-forwarded-for": agent.ip}
    for headers in [xff, {**xff, "authorization": "Bearer"}, {**xff, "authorization": "Basic abc"},
                    {**xff, "authorization": "Bearer not-a-token"}]:
        assert hub.client.get(ME, headers=headers).status_code == 401


def test_wrong_secret_rejected(hub):
    agent = hub.enroll("a")
    forged = security.new_token(agent.id)
    resp = hub.client.get(ME, headers={"authorization": f"Bearer {forged}", "x-forwarded-for": agent.ip})
    assert resp.status_code == 401


def test_secret_cannot_be_moved_to_other_agent_id(hub):
    a = hub.enroll("a")
    b = hub.enroll("b")
    _, secret_a = security.parse_token(a.token)
    spliced = f"ahk_{b.id}_{secret_a}"
    resp = hub.client.get(ME, headers={"authorization": f"Bearer {spliced}", "x-forwarded-for": b.ip})
    assert resp.status_code == 401


def test_unknown_agent_same_error_as_bad_secret(hub):
    agent = hub.enroll("a")
    unknown = hub.client.get(ME, headers={"authorization": f"Bearer {security.new_token('agnope')}",
                                          "x-forwarded-for": agent.ip})
    bad = hub.client.get(ME, headers={"authorization": f"Bearer {security.new_token(agent.id)}",
                                      "x-forwarded-for": agent.ip})
    assert unknown.status_code == bad.status_code == 401
    assert unknown.json() == bad.json()


def test_valid_token_and_device_accepted(hub):
    agent = hub.enroll("a")
    resp = hub.client.get(ME, headers=agent.headers)
    assert resp.status_code == 200 and resp.json()["name"] == "a"


def test_revocation_is_immediate(hub):
    agent = hub.enroll("a")
    assert hub.client.get(ME, headers=agent.headers).status_code == 200
    hub.set_status(agent, "revoked")
    assert hub.client.get(ME, headers=agent.headers).status_code == 401
    assert hub.client.get("/api/v1/inbox", headers=agent.headers).status_code == 401


def test_revoked_agent_cannot_be_reactivated(hub):
    from agenthub.hub import store

    agent = hub.enroll("a")
    hub.set_status(agent, "revoked")
    with pytest.raises(store.Conflict):
        hub.set_status(agent, "active")


def test_token_from_another_device_rejected(hub):
    a = hub.enroll("a")
    b = hub.enroll("b")
    stolen = {"authorization": f"Bearer {a.token}", "x-forwarded-for": b.ip}
    resp = hub.client.get(ME, headers=stolen)
    assert resp.status_code == 403
    assert "different tailnet device" in resp.json()["detail"]


def test_same_node_new_ip_is_accepted(hub):
    """Binding is to the node's StableID, not its address."""
    agent = hub.enroll("a")
    hub.whois.add("100.64.1.1", node_id="na", name="a-host.example.ts.net")
    headers = {**agent.headers, "x-forwarded-for": "100.64.1.1"}
    assert hub.client.get(ME, headers=headers).status_code == 200


@pytest.mark.parametrize(
    "xff",
    [None, "", "203.0.113.7", "100.64.0.11, 100.64.0.12", "not-an-ip", "127.0.0.1", "100.128.0.1"],
)
def test_bad_forwarded_for_rejected(hub, xff):
    agent = hub.enroll("a")
    headers = {"authorization": f"Bearer {agent.token}"}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    assert hub.client.get(ME, headers=headers).status_code == 403


def test_forwarded_for_ignored_unless_peer_is_loopback(hub):
    """A request that did not come through tailscale serve cannot assert an XFF."""
    from fastapi.testclient import TestClient

    agent = hub.enroll("a")
    direct = TestClient(hub.client.app, client=("100.64.0.50", 5555))
    assert direct.get(ME, headers=agent.headers).status_code == 403


def test_whois_failure_fails_closed(hub):
    agent = hub.enroll("a")
    hub.whois.nodes.clear()
    assert hub.client.get(ME, headers=agent.headers).status_code == 403


def test_tailnet_ipv6_accepted(hub):
    agent = hub.enroll("a")
    hub.whois.add("fd7a:115c:a1e0::5", node_id="na", name="a-host.example.ts.net")
    headers = {**agent.headers, "x-forwarded-for": "fd7a:115c:a1e0::5"}
    assert hub.client.get(ME, headers=headers).status_code == 200


def test_insecure_dev_skips_whois(dev_hub):
    code = dev_hub.code()
    resp = dev_hub.client.post("/api/v1/enroll", json={"code": code, "name": "dev"})
    assert resp.status_code == 201
    token = resp.json()["token"]
    assert resp.json()["agent"]["device"]["node_id"] == "insecure-dev"
    agent_id = resp.json()["agent"]["id"]
    from .conftest import AgentCreds

    dev_hub.set_status(AgentCreds(agent_id, "dev", token, ""), "active")
    assert dev_hub.client.get(ME, headers={"authorization": f"Bearer {token}"}).status_code == 200
    assert dev_hub.whois.calls == []


def test_project_access_enforced(hub):
    agent = hub.enroll("a", projects=("mine",))
    hub.enroll("b", projects=("theirs",))
    h = agent.headers
    checks = [
        hub.client.post("/api/v1/projects/theirs/messages", json={"body": "x"}, headers=h),
        hub.client.get("/api/v1/projects/theirs/messages", headers=h),
        hub.client.get("/api/v1/projects/theirs/board", headers=h),
        hub.client.get("/api/v1/projects/theirs/board/k", headers=h),
        hub.client.put("/api/v1/projects/theirs/board/k", json={"value": "v", "expected_version": 0}, headers=h),
        hub.client.get("/api/v1/inbox", params={"project": "theirs"}, headers=h),
        hub.client.get("/api/v1/search", params={"q": "x", "project": "theirs"}, headers=h),
    ]
    assert [r.status_code for r in checks] == [403] * len(checks)


def test_no_operator_endpoints_exist_for_agents(hub):
    """Phase 1 exposes no approval/enrollment-creation/revocation endpoints over HTTP at all."""
    agent = hub.enroll("a")
    for path in ["/api/v1/enrollments", "/api/v1/agents/a/approve", "/api/v1/agents/a/revoke"]:
        assert hub.client.post(path, headers=agent.headers).status_code in (404, 405)


def test_dev_enrolled_token_is_useless_outside_dev_mode(tmp_path):
    """An agent enrolled under --insecure-dev is bound to the 'insecure-dev' placeholder device,
    which no real tailnet node matches once the hub runs normally."""
    from fastapi.testclient import TestClient

    from agenthub.hub.app import HubState, create_app

    from .conftest import AgentCreds, make_hub

    dev = make_hub(tmp_path, insecure_dev=True)
    resp = dev.client.post("/api/v1/enroll", json={"code": dev.code(), "name": "devagent"}).json()
    dev.set_status(AgentCreds(resp["agent"]["id"], "devagent", resp["token"], ""), "active")
    assert dev.client.get(ME, headers={"authorization": f"Bearer {resp['token']}"}).status_code == 200

    prod = HubState(dev.state.config, insecure_dev=False, whois=dev.whois)
    dev.whois.add("100.64.0.77", "nreal", "real.example.ts.net")
    client = TestClient(create_app(prod), client=("127.0.0.1", 40000))
    r = client.get(ME, headers={"authorization": f"Bearer {resp['token']}", "x-forwarded-for": "100.64.0.77"})
    assert r.status_code == 403 and "different tailnet device" in r.json()["detail"]
