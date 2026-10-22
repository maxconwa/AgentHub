"""Stored avatar looks and the pose each agent's state maps to."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from agenthub.hub import avatar_art, avatars, db
from agenthub.hub.db import now_iso
from agenthub.hub.store import NotFound

from .conftest import OPERATOR

LATER = now_iso(datetime.now(UTC) + timedelta(hours=1))
EARLIER = now_iso(datetime.now(UTC) - timedelta(hours=1))


@pytest.fixture
def setup(hub):
    a = hub.enroll("worker")
    conn = hub.conn()
    conn.execute("UPDATE agents SET connected = 1 WHERE id = ?", (a.id,))
    return hub, a, conn


def _task(conn, agent_id, *, state="in_progress", stage="works", finished=False, paused_until=None):
    tid = conn.execute(
        "INSERT INTO tasks (project, title, created_by, state, budget, created_at, updated_at) "
        "VALUES ('proj', 't', ?, ?, '{}', 'x', 'x')", (OPERATOR, state)).lastrowid
    conn.execute(
        "INSERT INTO task_agents (task_id, agent_id, stage, budget, finished_at, paused_until, added_by, added_at) "
        "VALUES (?, ?, ?, '{}', ?, ?, ?, 'x')",
        (tid, agent_id, stage, "x" if finished else None, paused_until, OPERATOR))
    return tid


def _run(conn, tid, agent_id, *, outcome=None):
    conn.execute("INSERT INTO task_runs (task_id, agent_id, started_at, ended_at, outcome) VALUES (?, ?, 'x', ?, ?)",
                 (tid, agent_id, None if outcome is None else "y", outcome))


def _pending_approval(conn, agent_id):
    conn.execute("INSERT INTO approvals (kind, requester, details, status, created_at) "
                 "VALUES ('command', ?, '{}', 'pending', 'x')", (agent_id,))


def _ask(conn, from_id, to_id, *, expires=LATER):
    conn.execute("INSERT INTO asks (project, from_agent, to_agent, question, status, created_at, expires_at) "
                 "VALUES ('proj', ?, ?, 'q', 'pending', 'x', ?)", (from_id, to_id, expires))


def test_idle_when_nothing_is_going_on(setup):
    _, a, conn = setup
    assert avatars.poses(conn) == {a.id: "idle"}


def test_offline_beats_everything(setup):
    _, a, conn = setup
    _pending_approval(conn, a.id)
    _run(conn, _task(conn, a.id), a.id)
    conn.execute("UPDATE agents SET connected = 0")
    assert avatars.poses(conn)[a.id] == "offline"


def test_needs_you_beats_a_running_session(setup):
    _, a, conn = setup
    _run(conn, _task(conn, a.id), a.id)
    _pending_approval(conn, a.id)
    assert avatars.poses(conn)[a.id] == "needs_you"


def test_recharging_while_paused_at_the_usage_limit(setup):
    _, a, conn = setup
    tid = _task(conn, a.id, paused_until=LATER)
    assert avatars.poses(conn)[a.id] == "recharging"
    conn.execute("UPDATE task_agents SET paused_until = ?", (EARLIER,))
    assert avatars.poses(conn)[a.id] == "idle"
    conn.execute("UPDATE task_agents SET paused_until = ?", (LATER,))
    conn.execute("UPDATE tasks SET state = 'done' WHERE id = ?", (tid,))
    _run(conn, _task(conn, a.id), a.id)  # a leftover pause on a finished task doesn't hide new work
    assert avatars.poses(conn)[a.id] == "working"


def test_any_active_review_run_shows_reviewing(setup):
    _, a, conn = setup
    _run(conn, _task(conn, a.id, stage="reviews", state="review"), a.id)
    _run(conn, _task(conn, a.id), a.id)
    assert avatars.poses(conn)[a.id] == "reviewing"


def test_working_and_reviewing_follow_the_stage(setup):
    _, a, conn = setup
    tid = _task(conn, a.id)
    _run(conn, tid, a.id)
    assert avatars.poses(conn)[a.id] == "working"
    conn.execute("UPDATE task_agents SET stage = 'reviews'")
    assert avatars.poses(conn)[a.id] == "reviewing"


def test_waiting_on_an_unanswered_ask(hub, setup):
    _, a, conn = setup
    b = hub.enroll("helper")
    _ask(conn, a.id, b.id)
    assert avatars.poses(conn)[a.id] == "waiting"
    conn.execute("UPDATE asks SET expires_at = ?", (EARLIER,))
    assert avatars.poses(conn)[a.id] == "idle"


def test_error_when_the_latest_run_errored_on_a_live_task(setup):
    _, a, conn = setup
    tid = _task(conn, a.id)
    _run(conn, tid, a.id, outcome="error")
    assert avatars.poses(conn)[a.id] == "error"
    _run(conn, tid, a.id, outcome="success")  # a later run went fine
    assert avatars.poses(conn)[a.id] == "idle"


def test_no_error_once_the_task_is_done(setup):
    _, a, conn = setup
    _run(conn, _task(conn, a.id, state="done"), a.id, outcome="error")
    assert avatars.poses(conn)[a.id] == "idle"


def test_part_done_while_the_task_waits(setup):
    _, a, conn = setup
    tid = _task(conn, a.id, state="review", finished=True)
    assert avatars.poses(conn)[a.id] == "part_done"
    conn.execute("UPDATE tasks SET state = 'done' WHERE id = ?", (tid,))
    assert avatars.poses(conn)[a.id] == "idle"


def test_revoked_agents_get_no_pose(hub, setup):
    _, a, conn = setup
    hub.set_status(a, "revoked")
    assert avatars.poses(conn) == {} and avatars.board(conn) == {}


def test_board_urls_and_alt_text(setup):
    _, a, conn = setup
    v, s = conn.execute("SELECT avatar_version, avatar_seed FROM agents").fetchone()
    info = avatars.board(conn)[a.id]
    assert info == {"pose": "idle", "scene": f"/ui/avatars/{v}/{s}/scene/idle.svg",
                    "mini": f"/ui/avatars/{v}/{s}/mini/idle.svg", "alt": "worker", "label": "idle"}


def test_new_agents_get_a_look(hub):
    a, b = hub.enroll("one"), hub.enroll("two")
    rows = hub.conn().execute("SELECT avatar_version, avatar_seed FROM agents WHERE id IN (?, ?)",
                              (a.id, b.id)).fetchall()
    assert all(r[0] == avatar_art.GENERATOR_VERSION for r in rows) and rows[0][1] != rows[1][1]


def test_reroll_changes_the_seed_and_records_it(setup):
    _, a, conn = setup
    before = conn.execute("SELECT avatar_seed FROM agents").fetchone()[0]
    with db.transaction(conn):
        look = avatars.reroll(conn, a.id, actor=OPERATOR)
    assert look["seed"] != before and look["version"] == avatar_art.GENERATOR_VERSION
    assert conn.execute("SELECT avatar_seed FROM agents").fetchone()[0] == look["seed"]
    ev = conn.execute("SELECT actor, payload FROM events WHERE type = 'agent.avatar_changed'").fetchone()
    assert ev["actor"] == OPERATOR and str(look["seed"]) in ev["payload"]
    with pytest.raises(NotFound), db.transaction(conn):
        avatars.reroll(conn, "nope", actor=OPERATOR)


def test_migration_gives_existing_agents_distinct_seeds(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for version, script in enumerate(db.MIGRATIONS[:6], start=1):
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {version};COMMIT;")
    for i in range(5):
        conn.execute(
            "INSERT INTO agents (id, name, device_name, device_ip, device_node_id, token_hash, status, created_at) "
            f"VALUES ('ag{i}', 'a{i}', 'd', '1.1.1.1', 'n', 'h', 'active', 'x')")
    conn.close()
    new = db.connect(path)
    db.migrate(new)
    rows = new.execute("SELECT avatar_version, avatar_seed FROM agents").fetchall()
    seeds = {r[1] for r in rows}
    assert all(r[0] == 1 for r in rows) and len(seeds) == 5
    assert all(0 <= s < 2**31 for s in seeds)
