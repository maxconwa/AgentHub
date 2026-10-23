"""Migration 13: the indexes behind the Board, Chats and chat-window polls."""

import sqlite3

import pytest

from agenthub.hub import db, device_board, private, store

from .conftest import OPERATOR, legacy_private

NEW_INDEXES = {"messages_channel_id", "messages_to_agent", "private_messages_unread"}


def indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def plan(conn, sql, params=()):
    return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_migration_13_applies_to_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for version, script in enumerate(db.MIGRATIONS[:12], start=1):
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {version};COMMIT;")
    conn.execute("INSERT INTO projects (name, created_at) VALUES ('proj', 'x')")
    conn.execute("INSERT INTO channels (id, project, name, created_by, created_at) VALUES (1, 'proj', 'general', 'h', 'x')")
    conn.execute("INSERT INTO messages (project, channel_id, author, body, ts) VALUES ('proj', 1, 'h', 'kept', 'x')")
    conn.close()
    new = db.connect(path)
    assert db.migrate(new) == len(db.MIGRATIONS)
    assert NEW_INDEXES <= indexes(new)
    assert [r[0] for r in new.execute("SELECT body FROM messages")] == ["kept"]
    # Running it again (or over indexes made by hand) changes nothing.
    new.executescript(db.MIGRATIONS[12])
    assert NEW_INDEXES <= indexes(new)


@pytest.mark.parametrize("sql,index", [
    ("SELECT author, ts FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT 30", "messages_channel_id"),
    ("SELECT author, to_agent FROM messages WHERE to_agent IS NOT NULL", "messages_to_agent"),
    ("SELECT id FROM messages WHERE (author = ? AND to_agent = ?) OR (author = ? AND to_agent = ?)",
     "messages_to_agent"),
    ("SELECT agent_id, COUNT(*) FROM private_messages WHERE read_at IS NULL AND author = agent_id GROUP BY agent_id",
     "private_messages_unread"),
])
def test_poll_queries_use_the_indexes(hub, sql, index):
    params = (1,) if sql.count("?") == 1 else ("a", "b", "b", "a")[: sql.count("?")]
    assert index in plan(hub.conn(), sql, params)


def test_recent_messages_of_one_board_use_the_channel_index(hub):
    conn = hub.conn()
    seen = []
    conn.set_trace_callback(seen.append)
    store.recent_messages(conn, project="proj", channel="general")
    conn.set_trace_callback(None)
    assert "messages_channel_id" in plan(conn, next(q for q in seen if "FROM messages" in q))


def test_device_last_posts_only_for_the_asked_nodes(hub):
    a = hub.enroll("worker")
    node = store.get_agent(hub.conn(), a.id).device_node_id
    conn = hub.conn()
    with db.transaction(conn):
        device_board.post(conn, node, author=OPERATOR, body="first")
        device_board.post(conn, node, author=a.id, body="second")
    posts = device_board.last_posts(conn, [node, "nobody"])
    assert set(posts) == {node} and posts[node]["authors"] == [a.id, OPERATOR]
    assert posts[node]["ts"] == device_board.recent(conn, node)[-1]["ts"]


def test_unread_counts_unchanged(hub):
    a = hub.enroll("worker")
    legacy_private(hub, a, "hi")
    conn = hub.conn()
    with db.transaction(conn):
        private.send(conn, a.id, author=OPERATOR, body="for you")
    assert private.unread_counts(conn) == {a.id: 1}
