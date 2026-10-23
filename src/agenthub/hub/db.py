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
    # 7: each agent's avatar look, (generator version, seed); existing agents get a random seed
    """
    ALTER TABLE agents ADD COLUMN avatar_version INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE agents ADD COLUMN avatar_seed INTEGER NOT NULL DEFAULT 0;
    UPDATE agents SET avatar_seed = abs(random() % 2147483648);
    """,
    # 8: avatar poses look up each agent's latest run on every page render
    """
    CREATE INDEX task_runs_agent ON task_runs(agent_id, id);
    """,
    # 9: where each agent's runner has its project checkouts and where its newest session is running
    """
    ALTER TABLE agents ADD COLUMN checkouts TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE agents ADD COLUMN current_dir TEXT;
    """,
    # 10: private chats between the operator and one agent, kept out of channels and the event log
    """
    CREATE TABLE private_messages (
        id       INTEGER PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agents(id),
        author   TEXT NOT NULL,
        body     TEXT NOT NULL,
        ts       TEXT NOT NULL,
        read_at  TEXT
    );
    CREATE INDEX private_messages_agent_id ON private_messages(agent_id, id);
    """,
    # 11: questions agents ask the operator (answered through a private message)
    """
    CREATE TABLE questions (
        id          INTEGER PRIMARY KEY,
        project     TEXT NOT NULL REFERENCES projects(name),
        agent_id    TEXT NOT NULL REFERENCES agents(id),
        task_id     INTEGER REFERENCES tasks(id),
        question    TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'withdrawn')),
        answer      TEXT,
        answered_by TEXT,
        created_at  TEXT NOT NULL,
        answered_at TEXT
    );
    CREATE INDEX questions_status ON questions(status, id);
    """,
    # 12: device boards, shared by the agents on one machine (device_node_id) and the operator
    """
    CREATE TABLE device_messages (
        id       INTEGER PRIMARY KEY,
        node_id  TEXT NOT NULL,
        author   TEXT NOT NULL,
        to_agent TEXT REFERENCES agents(id),
        body     TEXT NOT NULL,
        ts       TEXT NOT NULL
    );
    CREATE INDEX device_messages_node_id ON device_messages(node_id, id);

    CREATE TABLE device_cursors (
        agent_id     TEXT PRIMARY KEY REFERENCES agents(id),
        last_read_id INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 13: indexes for the pages' 10 s polls (Board, Chats, the chat window, unread badges); additive only
    """
    CREATE INDEX IF NOT EXISTS messages_channel_id ON messages(channel_id, id);
    CREATE INDEX IF NOT EXISTS messages_to_agent ON messages(to_agent, author, id) WHERE to_agent IS NOT NULL;
    CREATE INDEX IF NOT EXISTS private_messages_unread ON private_messages(agent_id) WHERE read_at IS NULL;
    """,
    # 14: the operator's prompts, which wake agents now, and one row per recipient
    """
    CREATE TABLE prompts (
        id          INTEGER PRIMARY KEY,
        target      TEXT NOT NULL,
        body        TEXT NOT NULL,
        mode        TEXT NOT NULL CHECK (mode IN ('single', 'group')),
        project     TEXT REFERENCES projects(name),
        thread      TEXT,
        status      TEXT NOT NULL CHECK (status IN ('pending', 'running', 'discussing', 'done', 'failed')),
        created_by  TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX prompts_status ON prompts(status, id);

    CREATE TABLE prompt_recipients (
        prompt_id  INTEGER NOT NULL REFERENCES prompts(id),
        agent_id   TEXT NOT NULL REFERENCES agents(id),
        status     TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'claimed', 'declined',
                                                   'skipped', 'timed_out', 'failed')),
        task_id    INTEGER REFERENCES tasks(id),
        note       TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (prompt_id, agent_id)
    );
    CREATE INDEX prompt_recipients_agent ON prompt_recipients(agent_id, status);
    """,
    # 15: a prompt's copy in the agent's private chat is tagged with its prompt and never delivered as unread
    # mail (the prompt session acts on it, not a busy task session); it is stored read too, so a hub rolled back
    # to v0.1.0 doesn't deliver it either. Copies made at schema 14 are tagged and marked read.
    """
    ALTER TABLE private_messages ADD COLUMN prompt_id INTEGER REFERENCES prompts(id);
    UPDATE private_messages SET prompt_id = (
        SELECT p.id FROM prompts p JOIN prompt_recipients r ON r.prompt_id = p.id
        WHERE r.agent_id = private_messages.agent_id AND p.created_by = private_messages.author
        AND private_messages.ts >= p.created_at
        AND substr(private_messages.body, 1, length('Prompt #' || p.id || ': ')) = 'Prompt #' || p.id || ': '
    ) WHERE author LIKE 'human:%' AND body LIKE 'Prompt #%';
    UPDATE private_messages SET read_at = COALESCE(read_at, ts) WHERE prompt_id IS NOT NULL;
    """,
    # 16: whether the agent's runner runs prompts (its heartbeat says "prompts": 1; a v0.1.0 runner's doesn't)
    """
    ALTER TABLE agents ADD COLUMN prompt_support INTEGER NOT NULL DEFAULT 0;
    """,
    # 17: whether a prompt recipient's note is the hub's own (skipped, timed out, revoked, withdrawn, ...) rather than
    # agent text; at schema 14 only skipped and timed_out rows were the hub's alone (a failed row's note isn't guessed)
    """
    ALTER TABLE prompt_recipients ADD COLUMN note_by_hub INTEGER NOT NULL DEFAULT 0;
    UPDATE prompt_recipients SET note_by_hub = 1 WHERE status IN ('skipped', 'timed_out');
    """,
    # 18: the model the agent's runner reports (its sessions' model, else the one configured), shown on its page
    """
    ALTER TABLE agents ADD COLUMN model TEXT;
    """,
    # 19: one row per prompt session a runner ran (its turns, est. cost and duration), for the Usage page; a prompt
    # paused by the usage limit and resumed has two
    """
    CREATE TABLE prompt_runs (
        id          INTEGER PRIMARY KEY,
        prompt_id   INTEGER NOT NULL REFERENCES prompts(id),
        agent_id    TEXT NOT NULL REFERENCES agents(id),
        project     TEXT REFERENCES projects(name),
        session_id  TEXT,
        started_at  TEXT NOT NULL,
        ended_at    TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        num_turns   INTEGER,
        cost_usd    REAL
    );
    CREATE INDEX prompt_runs_started ON prompt_runs(started_at);
    CREATE INDEX prompt_runs_prompt ON prompt_runs(prompt_id, id);
    CREATE INDEX IF NOT EXISTS task_runs_started ON task_runs(started_at);
    """,
    # 20: reports agents publish (the Reports page): a link to a Claude page or a self-contained HTML page
    """
    CREATE TABLE reports (
        id          INTEGER PRIMARY KEY,
        project     TEXT NOT NULL REFERENCES projects(name),
        task_id     INTEGER REFERENCES tasks(id),
        agent_id    TEXT NOT NULL REFERENCES agents(id),
        title       TEXT NOT NULL,
        summary     TEXT NOT NULL DEFAULT '',
        url         TEXT,
        html        TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        CHECK ((url IS NULL) <> (html IS NULL))
    );
    CREATE INDEX reports_created ON reports(created_at);
    CREATE INDEX reports_project ON reports(project, created_at);
    CREATE INDEX reports_task ON reports(task_id);
    """,
    # 21: the reasoning effort the agent's runner reports (low/medium/high/xhigh/max), shown next to the model
    """
    ALTER TABLE agents ADD COLUMN effort TEXT;
    """,
    # 22: chats replace prompts, private chats and boards: one chat per agent, task and project, their messages
    # (``legacy`` names the 0.3.7 row a backfilled one was copied from), one delivery per operator message and
    # recipient, and the agents' read cursors. Additive only: 0.3.7 runs on this database and ignores all of it.
    """
    CREATE TABLE chats (
        id               INTEGER PRIMARY KEY,
        kind             TEXT NOT NULL CHECK (kind IN ('agent', 'task', 'project')),
        ref              TEXT NOT NULL,
        project          TEXT REFERENCES projects(name),
        session_id       TEXT,
        operator_read_id INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        UNIQUE (kind, ref)
    );

    CREATE TABLE chat_messages (
        id         INTEGER PRIMARY KEY,
        chat_id    INTEGER NOT NULL REFERENCES chats(id),
        author     TEXT NOT NULL,
        kind       TEXT NOT NULL CHECK (kind IN ('text', 'tools', 'status', 'approval', 'question', 'took_task',
                                                 'sit_out')),
        body       TEXT NOT NULL DEFAULT '',
        meta       TEXT NOT NULL DEFAULT '{}',
        legacy     TEXT UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX chat_messages_chat ON chat_messages(chat_id, created_at, id);
    CREATE INDEX chat_messages_chat_id ON chat_messages(chat_id, id);

    CREATE TABLE chat_deliveries (
        id           INTEGER PRIMARY KEY,
        message_id   INTEGER NOT NULL REFERENCES chat_messages(id),
        agent_id     TEXT NOT NULL REFERENCES agents(id),
        status       TEXT NOT NULL CHECK (status IN ('pending', 'delivered')),
        mode         TEXT CHECK (mode IN ('live', 'queued', 'woke', 'inbox', 'failed')),
        decision     TEXT CHECK (decision IN ('took', 'sat_out')),
        task_id      INTEGER REFERENCES tasks(id),
        note         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        delivered_at TEXT,
        UNIQUE (message_id, agent_id)
    );
    CREATE INDEX chat_deliveries_agent ON chat_deliveries(agent_id, status, id);

    CREATE TABLE chat_cursors (
        agent_id     TEXT NOT NULL REFERENCES agents(id),
        chat_id      INTEGER NOT NULL REFERENCES chats(id),
        last_read_id INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (agent_id, chat_id)
    );

    -- one row per chat session a runner ran (its turns, est. cost and duration), for the Usage page
    CREATE TABLE chat_runs (
        id          INTEGER PRIMARY KEY,
        chat_id     INTEGER NOT NULL REFERENCES chats(id),
        agent_id    TEXT NOT NULL REFERENCES agents(id),
        project     TEXT REFERENCES projects(name),
        task_id     INTEGER REFERENCES tasks(id),
        session_id  TEXT,
        started_at  TEXT NOT NULL,
        ended_at    TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        num_turns   INTEGER,
        cost_usd    REAL
    );
    CREATE INDEX chat_runs_started ON chat_runs(started_at);

    ALTER TABLE agents ADD COLUMN chat_support INTEGER NOT NULL DEFAULT 0;
    """,
    # 23: names may have capitals (0.3.9.0) and are unique ignoring case: name_key is the lowercased name, kept by
    # triggers (so a rolled-back hub that doesn't know the column still fills it) under a unique index
    """
    ALTER TABLE projects ADD COLUMN name_key TEXT;
    UPDATE projects SET name_key = lower(name);
    CREATE UNIQUE INDEX projects_name_key ON projects(name_key);
    CREATE TRIGGER projects_name_key_insert AFTER INSERT ON projects
        BEGIN UPDATE projects SET name_key = lower(NEW.name) WHERE rowid = NEW.rowid; END;
    CREATE TRIGGER projects_name_key_update AFTER UPDATE OF name ON projects
        BEGIN UPDATE projects SET name_key = lower(NEW.name) WHERE rowid = NEW.rowid; END;

    ALTER TABLE agents ADD COLUMN name_key TEXT;
    UPDATE agents SET name_key = lower(name);
    CREATE UNIQUE INDEX agents_name_key ON agents(name_key);
    CREATE TRIGGER agents_name_key_insert AFTER INSERT ON agents
        BEGIN UPDATE agents SET name_key = lower(NEW.name) WHERE rowid = NEW.rowid; END;
    CREATE TRIGGER agents_name_key_update AFTER UPDATE OF name ON agents
        BEGIN UPDATE agents SET name_key = lower(NEW.name) WHERE rowid = NEW.rowid; END;
    """,
    # 24: agents wake agents (0.3.9.1). chats and approvals are rebuilt only to widen a CHECK (agent-to-agent 'dm'
    # chats, 'wake_loop' approvals; same columns, every row copied, as migration 3 did); the rest is new columns
    # and tables. 0.3.9.0 runs on this database and ignores all of it.
    """
    CREATE TABLE chats_v24 (
        id               INTEGER PRIMARY KEY,
        kind             TEXT NOT NULL CHECK (kind IN ('agent', 'task', 'project', 'dm')),
        ref              TEXT NOT NULL,
        project          TEXT REFERENCES projects(name),
        session_id       TEXT,
        operator_read_id INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        UNIQUE (kind, ref)
    );
    INSERT INTO chats_v24 (id, kind, ref, project, session_id, operator_read_id, created_at)
        SELECT id, kind, ref, project, session_id, operator_read_id, created_at FROM chats;
    DROP TABLE chats;
    ALTER TABLE chats_v24 RENAME TO chats;

    CREATE TABLE approvals_v24 (
        id            INTEGER PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN ('command', 'task_create', 'task_reassign',
                                                    'budget_exceeded', 'agent_enrollment',
                                                    'tool_permission', 'wake_loop')),
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
    INSERT INTO approvals_v24 (id, kind, project, requester, details, reason, status, decided_by, decided_at,
                               decision_note, created_at, command_key, consumed_at)
        SELECT id, kind, project, requester, details, reason, status, decided_by, decided_at, decision_note,
               created_at, command_key, consumed_at FROM approvals;
    DROP TABLE approvals;
    ALTER TABLE approvals_v24 RENAME TO approvals;
    CREATE INDEX approvals_status ON approvals(status, id);
    CREATE INDEX approvals_command_key ON approvals(command_key, status);

    -- an agent's message: the wake chain it belongs to; wake = 0 for a notice that never wakes (a decline)
    ALTER TABLE chat_deliveries ADD COLUMN chain_id TEXT;
    ALTER TABLE chat_deliveries ADD COLUMN wake INTEGER NOT NULL DEFAULT 1;
    CREATE INDEX chat_deliveries_chain ON chat_deliveries(chain_id) WHERE chain_id IS NOT NULL;
    ALTER TABLE agents ADD COLUMN wakes_paused INTEGER NOT NULL DEFAULT 0;
    -- the wake chains a session took in, as the runner reports them (JSON list)
    ALTER TABLE chat_runs ADD COLUMN chain_ids TEXT;
    ALTER TABLE task_runs ADD COLUMN chain_ids TEXT;

    CREATE TABLE wake_chains (
        id          TEXT PRIMARY KEY,
        started_by  TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        wakes       INTEGER NOT NULL DEFAULT 0,  -- since it last landed
        total       INTEGER NOT NULL DEFAULT 0,
        landed_at   TEXT,
        status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'paused')),
        paused_at   TEXT,
        approval_id INTEGER
    );
    CREATE INDEX wake_chains_status ON wake_chains(status);

    -- hub-wide switches the operator sets from the dashboard (wakes_paused)
    CREATE TABLE hub_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
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
