import pytest

BOARD = "/api/v1/projects/proj/board"


def put(hub, agent, key, value, version):
    return hub.client.put(f"{BOARD}/{key}", json={"value": value, "expected_version": version}, headers=agent.headers)


def test_create_read_update(hub):
    a = hub.enroll("a")
    assert put(hub, a, "plan", "v1", 0).json()["version"] == 1
    assert put(hub, a, "plan", "v2", 1).json()["version"] == 2
    doc = hub.client.get(f"{BOARD}/plan", headers=a.headers).json()
    assert doc["value"] == "v2" and doc["version"] == 2 and doc["updated_by_name"] == "a"


def test_stale_write_conflicts_and_reports_current_version(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    put(hub, a, "plan", "base", 0)
    assert put(hub, a, "plan", "a-edit", 1).status_code == 200
    stale = put(hub, b, "plan", "b-edit", 1)
    assert stale.status_code == 409
    assert stale.json()["current_version"] == 2
    assert hub.client.get(f"{BOARD}/plan", headers=a.headers).json()["value"] == "a-edit"


def test_create_conflicts_when_key_exists(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    put(hub, a, "k", "first", 0)
    resp = put(hub, b, "k", "second", 0)
    assert resp.status_code == 409 and resp.json()["current_version"] == 1


def test_update_of_missing_key_conflicts(hub):
    a = hub.enroll("a")
    resp = put(hub, a, "nope", "x", 3)
    assert resp.status_code == 409 and resp.json()["current_version"] == 0


def test_negative_version_rejected(hub):
    a = hub.enroll("a")
    assert put(hub, a, "k", "x", -1).status_code == 422


def test_nested_keys_and_prefix_listing(hub):
    a = hub.enroll("a")
    put(hub, a, "coordination/a-b", "split", 0)
    put(hub, a, "coordination/c-d", "order", 0)
    put(hub, a, "notes", "n", 0)
    keys = hub.client.get(BOARD, params={"prefix": "coordination/"}, headers=a.headers).json()["keys"]
    assert [k["key"] for k in keys] == ["coordination/a-b", "coordination/c-d"]


def test_prefix_wildcards_are_literal(hub):
    a = hub.enroll("a")
    put(hub, a, "a_b", "x", 0)
    put(hub, a, "axb", "x", 0)
    keys = hub.client.get(BOARD, params={"prefix": "a_"}, headers=a.headers).json()["keys"]
    assert [k["key"] for k in keys] == ["a_b"]


@pytest.mark.parametrize("key", ["%2E%2E/etc", "a/%2E%2E/b", "-x", "a//b", "x" * 201])
def test_bad_keys_rejected(hub, key):
    a = hub.enroll("a")
    assert put(hub, a, key, "x", 0).status_code in (404, 422)


def test_board_writes_emit_versioned_events(hub):
    a = hub.enroll("a")
    put(hub, a, "k", "one", 0)
    put(hub, a, "k", "two", 1)
    rows = hub.conn().execute("SELECT payload FROM events WHERE type = 'board.updated' ORDER BY id").fetchall()
    import json

    assert [json.loads(r["payload"])["version"] for r in rows] == [1, 2]


@pytest.mark.parametrize("key", ["../etc", "a/../b", "a//b", "", "/abs"])
def test_store_rejects_traversal_keys(key):
    from agenthub.hub import store

    with pytest.raises(store.Invalid):
        store.check_board_key(key)
