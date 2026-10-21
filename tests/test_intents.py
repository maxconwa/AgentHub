import json

import pytest

from agenthub.hub import intents, store


@pytest.mark.parametrize("p,q", [
    ("src/api/**", "src/api/users.py"), ("src/api/*.py", "src/api/users.py"), ("src", "src/api/users.py"),
    ("src/api/**", "src/**"), ("**/*.md", "docs/guide.md"), ("auth", "auth.tokens"), ("auth.tokens", "auth"),
    ("docs/guide.md", "docs/guide.md"), ("src/{a,b}/x.py", "src/a/x.py"),
])
def test_overlapping(p, q):
    assert intents.patterns_overlap(intents.normalize(p), intents.normalize(q))
    assert intents.patterns_overlap(intents.normalize(q), intents.normalize(p))


@pytest.mark.parametrize("p,q", [
    ("src/api/**", "src/web/**"), ("src/api/users.py", "src/api/orders.py"), ("docs/*.md", "src/main.py"),
    ("auth", "authz"), ("src/api/*.py", "docs/api.md"), ("src/app", "src/application"),
])
def test_not_overlapping(p, q):
    assert not intents.patterns_overlap(intents.normalize(p), intents.normalize(q))


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../b", "rm -rf /", "x" * 201, "", "a;b"])
def test_bad_patterns(bad):
    with pytest.raises(store.Invalid):
        intents.normalize(bad)


def test_normalization():
    assert intents.normalize("./src//api/") == "src/api"


def announce(hub, agent, patterns, **kw):
    return hub.client.post("/api/v1/projects/proj/intents", json={"patterns": patterns, **kw}, headers=agent.headers)


def test_overlap_opens_coordination_thread_and_board_doc(hub):
    a, b = hub.enroll("alice"), hub.enroll("bob")
    first = announce(hub, a, ["src/api/**"], description="refactor API")
    assert first.status_code == 201 and first.json()["conflicts"] == []
    second = announce(hub, b, ["src/api/users.py", "docs/api.md"]).json()
    (conflict,) = second["conflicts"]
    assert conflict["other_agent"] == "alice" and conflict["overlap"] == [["src/api/users.py", "src/api/**"]]
    cid = conflict["conflict_id"]

    for agent in (a, b):
        inbox = hub.client.get("/api/v1/inbox", headers=agent.headers).json()["messages"]
        kick = [m for m in inbox if m["channel"] == f"coordination-{cid}"]
        assert len(kick) == 1 and kick[0]["addressed_to_me"] and kick[0]["author"] == "system:hub"
        assert "alice" in kick[0]["body"] and "bob" in kick[0]["body"]
        assert "refactor API" not in kick[0]["body"]  # agent free text is not echoed by the hub

    doc = hub.client.get(f"/api/v1/projects/proj/board/coordination/{cid}", headers=a.headers).json()
    assert json.loads(doc["value"])["status"] == "open" and doc["version"] == 1
    # the agents record their agreement with optimistic concurrency
    agreed = {**json.loads(doc["value"]), "status": "agreed", "agreement": "alice first, bob after merge"}
    r = hub.client.put(f"/api/v1/projects/proj/board/coordination/{cid}", headers=b.headers,
                       json={"value": json.dumps(agreed), "expected_version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2

    types = [e["type"] for e in (dict(r) for r in hub.conn().execute("SELECT type FROM events"))]
    assert "intent.conflict" in types


def test_own_and_cleared_and_expired_intents_do_not_conflict(hub):
    a, b = hub.enroll("alice"), hub.enroll("bob")
    i1 = announce(hub, a, ["src/**"]).json()["intent_id"]
    assert announce(hub, a, ["src/x.py"]).json()["conflicts"] == []  # same agent
    hub.client.post(f"/api/v1/intents/{i1}/clear", headers=a.headers)
    announce(hub, a, ["lib/**"]).json()
    hub.conn().execute("UPDATE intents SET expires_at = '2000-01-01T00:00:00.000000Z' WHERE agent_id = ?", (a.id,))
    assert announce(hub, b, ["src/x.py", "lib/y.py"]).json()["conflicts"] == []
    assert [i["agent_name"] for i in hub.client.get("/api/v1/projects/proj/intents", headers=b.headers).json()["intents"]] == ["bob"]


def test_only_owner_can_clear(hub):
    a, b = hub.enroll("alice"), hub.enroll("bob")
    i1 = announce(hub, a, ["src/**"]).json()["intent_id"]
    assert hub.client.post(f"/api/v1/intents/{i1}/clear", headers=b.headers).status_code == 403


def test_intents_are_project_scoped(hub):
    a = hub.enroll("alice", projects=("proj",))
    b = hub.enroll("bob", projects=("other",))
    announce(hub, a, ["src/**"])
    r = hub.client.post("/api/v1/projects/other/intents", json={"patterns": ["src/x.py"]}, headers=b.headers)
    assert r.json()["conflicts"] == []
    assert hub.client.get("/api/v1/projects/proj/intents", headers=b.headers).status_code == 403


def test_limits(hub):
    a = hub.enroll("alice")
    assert announce(hub, a, [f"f{i}" for i in range(21)]).status_code == 422
    assert announce(hub, a, ["x"], ttl_minutes=0).status_code == 422
    assert announce(hub, a, ["../x"]).status_code == 422


def test_conflict_notifies_operator():
    from agenthub.config import HubConfig
    from agenthub.hub.notify import build_notification

    cfg = HubConfig(public_url="https://hub", ntfy_url="https://n", ntfy_topic="t")
    e = {"id": 1, "ts": "", "type": "intent.conflict", "project": "proj", "actor": "system:hub", "device": None,
         "payload": {"agents": ["alice", "bob"], "intents": [1, 2], "overlap": [["a", "a"]]}}
    msg = build_notification(e, {}, cfg)
    assert "alice and bob" in msg["message"] and msg["click"] == "https://hub/ui/feed?project=proj"
