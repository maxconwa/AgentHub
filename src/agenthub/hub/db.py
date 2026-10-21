"""SQLite storage: connection setup and ordered schema migrations.

Every connection uses WAL mode, foreign keys, and autocommit mode
(``isolation_level=None``) so that transactions are always explicit via
:func:`transaction`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from agenthub import paths

# Each entry is applied once, in order. Never edit a released migration;
# append a new one instead.
MIGRATIONS: list[str] = [
    # 1: phase 1 core
    """
    CREATE TABLE events (
        id      INTEGER PRIMARY KEY,
        ts      TEXT NOT NULL,
        project TEXT,
        actor   TEXT NOT NULL,
        device  TEXT,
        type    TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX events_project_id ON events(project, id);
    CREATE INDEX events_type_id ON events(type, id);
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    -- rowid of events_fts == events.id
    CREATE VIRTUAL TABLE events_fts USING fts5(text, tokenize = 'unicode61');

    CREATE TABLE projects (
        name        TEXT PRIMARY KEY,
        description TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    );

    CREATE TABLE agents (
        id               TEXT PRIMARY KEY,
        name             TEXT NOT NULL UNIQUE,
        device_name      TEXT NOT NULL,
        device_ip        TEXT NOT NULL,
        device_node_id   TEXT NOT NULL,
        token_hash       TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked')),
        allowed_projects TEXT NOT NULL DEFAULT '[]',
        default_budget   TEXT NOT NULL DEFAULT '{}',
        created_at       TEXT NOT NULL,
        last_heartbeat   TEXT
    );

    CREATE TABLE enrollments (
        code_hash        TEXT PRIMARY KEY,
        created_by       TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        expires_at       TEXT NOT NULL,
        allowed_projects TEXT NOT NULL DEFAULT '[]',
        used_at          TEXT,
        used_by_agent    TEXT
    );

    CREATE TABLE channels (
        id         INTEGER PRIMARY KEY,
        project    TEXT NOT NULL REFERENCES projects(name),
        name       TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (project, name)
    );

    CREATE TABLE messages (
        id         INTEGER PRIMARY KEY,
        project    TEXT NOT NULL REFERENCES projects(name),
        channel_id INTEGER NOT NULL REFERENCES channels(id),
        author     TEXT NOT NULL,
        to_agent   TEXT,
        body       TEXT NOT NULL,
        ts         TEXT NOT NULL
    );
    CREATE INDEX messages_project_id ON messages(project, id);

    CREATE TABLE inbox_cursors (
        agent_id     TEXT NOT NULL REFERENCES agents(id),
        project      TEXT NOT NULL REFERENCES projects(name),
        last_read_id INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (agent_id, project)
    );

    CREATE TABLE board (
        project    TEXT NOT NULL REFERENCES projects(name),
        key        TEXT NOT NULL,
        version    INTEGER NOT NULL,
        value      TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (project, key)
    );
    """,
]


def now_iso(dt: datetime | None = None) -> str:
    """Fixed-width UTC timestamp, so string comparison equals time comparison."""
    dt = (dt or datetime.now(UTC)).astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE so concurrent writers serialize instead of failing late."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; return the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        # executescript commits implicitly, so wrap the script and the version
        # bump in one explicit transaction inside the script itself.
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)


def init_db(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    paths.ensure_private_dir(db_path.parent)
    conn = connect(db_path)
    migrate(conn)
    db_path.chmod(0o600)
    return conn
