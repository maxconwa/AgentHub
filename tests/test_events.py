import sqlite3

import pytest

from agenthub.hub import db, events


def test_events_are_append_only(hub):
    hub.enroll("a")
    conn = hub.conn()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET actor = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_every_mutation_emits_an_event(hub):
    a = hub.enroll("a")
    hub.client.post("/api/v1/chats/project/proj/messages", json={"body": "hi"}, headers=a.headers)
    hub.client.put("/api/v1/projects/proj/board/k", json={"value": "v", "expected_version": 0}, headers=a.headers)
    types = {r["type"] for r in hub.conn().execute("SELECT type FROM events")}
    assert {"chat.message", "board.updated", "agent.enrolled", "agent.approved"} <= types


def test_search_finds_messages_and_board(hub):
    a = hub.enroll("a")
    hub.client.post("/api/v1/chats/project/proj/messages", json={"body": "the flux capacitor"}, headers=a.headers)
    hub.client.put("/api/v1/projects/proj/board/design", json={"value": "capacitor notes", "expected_version": 0},
                   headers=a.headers)
    results = hub.client.get("/api/v1/search", params={"q": "capacitor"}, headers=a.headers).json()["results"]
    assert {r["type"] for r in results} == {"chat.message", "board.updated"}
    assert any(r["payload"].get("body") == "the flux capacitor" for r in results)


def test_search_scoped_to_allowed_projects(hub):
    a = hub.enroll("a", projects=("proj", "secret"))
    b = hub.enroll("b", projects=("proj",))
    hub.client.post("/api/v1/chats/project/secret/messages", json={"body": "zebra"}, headers=a.headers)
    assert hub.client.get("/api/v1/search", params={"q": "zebra"}, headers=b.headers).json()["results"] == []
    assert len(hub.client.get("/api/v1/search", params={"q": "zebra"}, headers=a.headers).json()["results"]) == 1


def test_search_never_returns_projectless_events(hub):
    """Enrollment/admin events have no project and are invisible to agents."""
    a = hub.enroll("a")
    results = hub.client.get("/api/v1/search", params={"q": "enrolled"}, headers=a.headers).json()["results"]
    assert results == []


@pytest.mark.parametrize("q", ['"', "a OR", "text:foo", "NEAR(a b)", "*", "-x", "^a", "a AND (b"])
def test_search_query_syntax_is_neutralized(hub, q):
    a = hub.enroll("a")
    assert hub.client.get("/api/v1/search", params={"q": q}, headers=a.headers).status_code == 200


def test_fts_query_quotes_terms():
    assert events.fts_query('foo bar"; DROP') == '"foo" "bar" "DROP"'
    assert events.fts_query("!!!") is None


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "x.db"
    conn = db.init_db(path)
    assert db.migrate(conn) == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert (path.stat().st_mode & 0o777) == 0o600
