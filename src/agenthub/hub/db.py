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
    # 2: phase 2 tasks and approvals
    """
    CREATE TABLE approvals (
        id            INTEGER PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('command', 'task_create', 'task_reassign',
                                                    'budget_exceeded', 'agent_enrollment')),
        project       TEXT REFERENCES projects(name),
        requester     TEXT NOT NULL,
        details       TEXT NOT NULL,
        reason        TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by    TEXT,
        decided_at    TEXT,
        decision_note TEXT,
        created_at    TEXT NOT NULL,
        -- command approvals only: hash of (agent, cwd, command) and one-time use
        command_key   TEXT,
        consumed_at   TEXT
    );
    CREATE INDEX approvals_status ON approvals(status, id);
    CREATE INDEX approvals_command_key ON approvals(command_key, status);

    CREATE TABLE tasks (
        id          INTEGER PRIMARY KEY,
        project     TEXT NOT NULL REFERENCES projects(name),
        title       TEXT NOT NULL,
        body        TEXT NOT NULL DEFAULT '',
        created_by  TEXT NOT NULL,
        assignee    TEXT REFERENCES agents(id),
        state       TEXT NOT NULL CHECK (state IN ('proposed', 'awaiting_approval', 'open', 'claimed',
                                                   'in_progress', 'blocked', 'review', 'done',
                                                   'rejected', 'cancelled')),
        budget      TEXT NOT NULL DEFAULT '{}',
        approval_id INTEGER REFERENCES approvals(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX tasks_project_state ON tasks(project, state);
    CREATE INDEX tasks_assignee_state ON tasks(assignee, state);
    """,
    # 3: phase 3: approval discussions, tool_permission approvals, asks, task runs.
    # SQLite cannot alter a CHECK constraint, so approvals is rebuilt (migrate()
    # disables foreign keys around migrations and re-checks them afterwards).
    """
    CREATE TABLE approvals_v3 (
        id            INTEGER PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('command', 'task_create', 'task_reassign',
                                                    'budget_exceeded', 'agent_enrollment',
                                                    'tool_permission')),
        project       TEXT REFERENCES projects(name),
        requester     TEXT NOT NULL,
        details       TEXT NOT NULL,
        reason        TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
        decided_by    TEXT,
        decided_at    TEXT,
        decision_note TEXT,
        created_at    TEXT NOT NULL,
        command_key   TEXT,
        consumed_at   TEXT
    );
    INSERT INTO approvals_v3 SELECT * FROM approvals;
    DROP TABLE approvals;
    ALTER TABLE approvals_v3 RENAME TO approvals;
    CREATE INDEX approvals_status ON approvals(status, id);
    CREATE INDEX approvals_command_key ON approvals(command_key, status);

    CREATE TABLE approval_messages (
        id          INTEGER PRIMARY KEY,
        approval_id INTEGER NOT NULL REFERENCES approvals(id),
        author      TEXT NOT NULL,
        body        TEXT NOT NULL,
        ts          TEXT NOT NULL
    );
    CREATE INDEX approval_messages_approval ON approval_messages(approval_id, id);

    CREATE TABLE asks (
        id          INTEGER PRIMARY KEY,
        project     TEXT NOT NULL REFERENCES projects(name),
        from_agent  TEXT NOT NULL REFERENCES agents(id),
        to_agent    TEXT NOT NULL REFERENCES agents(id),
        question    TEXT NOT NULL,
        answer      TEXT,
        status      TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'expired')),
        message_id  INTEGER REFERENCES messages(id),
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        answered_at TEXT
    );
    CREATE INDEX asks_to_status ON asks(to_agent, status);

    CREATE TABLE task_runs (
        id          INTEGER PRIMARY KEY,
        task_id     INTEGER NOT NULL REFERENCES tasks(id),
        agent_id    TEXT NOT NULL REFERENCES agents(id),
        session_id  TEXT,
        started_at  TEXT NOT NULL,
        ended_at    TEXT,
        outcome     TEXT,
        num_turns   INTEGER,
        cost_usd    REAL
    );
    CREATE INDEX task_runs_task ON task_runs(task_id, id);

    -- the most recent transition note, so a resumed session sees e.g. the
    -- operator's "changes requested" message
    ALTER TABLE tasks ADD COLUMN last_note TEXT NOT NULL DEFAULT '';
    ALTER TABLE tasks ADD COLUMN last_moved_by TEXT;
    ALTER TABLE agents ADD COLUMN connected INTEGER NOT NULL DEFAULT 0;
    """,
    # 4: phase 5: work intents and overlap coordination
    """
    CREATE TABLE intents (
        id          INTEGER PRIMARY KEY,
        project     TEXT NOT NULL REFERENCES projects(name),
        agent_id    TEXT NOT NULL REFERENCES agents(id),
        patterns    TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        cleared_at  TEXT
    );
    CREATE INDEX intents_project_active ON intents(project, cleared_at, expires_at);

    CREATE TABLE intent_conflicts (
        id         INTEGER PRIMARY KEY,
        project    TEXT NOT NULL REFERENCES projects(name),
        intent_a   INTEGER NOT NULL REFERENCES intents(id),
        intent_b   INTEGER NOT NULL REFERENCES intents(id),
        channel    TEXT NOT NULL,
        board_key  TEXT NOT NULL,
        overlap    TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (intent_a, intent_b)
    );
    """,
    # 5: several agents per task, each with an operator-written role and a stage
    """
    CREATE TABLE task_agents (
        task_id       INTEGER NOT NULL REFERENCES tasks(id),
        agent_id      TEXT NOT NULL REFERENCES agents(id),
        role          TEXT NOT NULL DEFAULT '',
        stage         TEXT NOT NULL CHECK (stage IN ('works', 'reviews')),
        budget        TEXT NOT NULL,
        finished_at   TEXT,
        verdict       TEXT CHECK (verdict IN ('approve', 'changes')),
        verdict_notes TEXT NOT NULL DEFAULT '',
        paused_until  TEXT,
        added_by      TEXT NOT NULL,
        added_at      TEXT NOT NULL,
        PRIMARY KEY (task_id, agent_id)
    );
    CREATE INDEX task_agents_agent ON task_agents(agent_id);
    INSERT INTO task_agents (task_id, agent_id, role, stage, budget, added_by, added_at)
        SELECT id, assignee, '', 'works', budget, 'migration', updated_at FROM tasks WHERE assignee IS NOT NULL;
    UPDATE tasks SET assignee = NULL;
    """,
    # 6: the note an implementer gave when it finished its part (shown to reviewers)
    """
    ALTER TABLE task_agents ADD COLUMN finish_note TEXT NOT NULL DEFAULT '';
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
    if current >= len(MIGRATIONS):
        return current
    # Table rebuilds need foreign keys off; this pragma is a no-op inside a
    # transaction, so it is toggled around the whole batch.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        _apply_migrations(conn, current)
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise sqlite3.IntegrityError(f"foreign key violations after migration: {problems[:5]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return len(MIGRATIONS)


def _apply_migrations(conn: sqlite3.Connection, current: int) -> None:
    for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
        # executescript commits implicitly, so wrap the script and the version
        # bump in one explicit transaction inside the script itself.
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def init_db(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    paths.ensure_private_dir(db_path.parent)
    conn = connect(db_path)
    migrate(conn)
    db_path.chmod(0o600)
    return conn
