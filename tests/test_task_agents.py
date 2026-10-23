import ast
import json
import sqlite3
from pathlib import Path

import pytest

from agenthub.hub import db, store, task_agents, tasks

from .conftest import OPERATOR

SRC = Path(__file__).resolve().parent.parent / "src" / "agenthub" / "hub"


def mk_task(hub, project="proj", **kw):
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, project, actor=OPERATOR)
        return tasks.create_task(conn, actor=OPERATOR, project=project, title="t", **kw)


def add(hub, task_id, agent, role="r", stage="works"):
    conn = hub.conn()
    with db.transaction(conn):
        return task_agents.add_agent(conn, task_id, agent, role=role, stage=stage, actor=OPERATOR)


def test_add_two_agents_with_roles_and_stages(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    t = mk_task(hub)
    add(hub, t["id"], a.name, "Implementer", "works")
    add(hub, t["id"], b.id, "Reviewer", "reviews")
    got = tasks.get_task(hub.conn(), t["id"])
    assert [(m["name"], m["role"], m["stage"]) for m in got["agents"]] == [
        ("a", "Implementer", "works"), ("b", "Reviewer", "reviews")]
    assert "assignee" not in got and "assignee_name" not in got


def test_agent_budget_copied_from_task(hub):
    a = hub.enroll("a")
    t = mk_task(hub, budget={"max_turns": 7})
    add(hub, t["id"], a.id)
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET budget = ? WHERE id = ?",
                     (json.dumps({**t["budget"], "max_turns": 9}), t["id"]))
    got = tasks.get_task(conn, t["id"])
    assert got["budget"]["max_turns"] == 9
    assert got["agents"][0]["budget"]["max_turns"] == 7


def test_add_rejects_duplicates_bad_stage_long_role_and_outsiders(hub):
    a = hub.enroll("a")
    out = hub.enroll("out", projects=("other",))
    inactive = hub.enroll("idle", approve=False)
    t = mk_task(hub)
    add(hub, t["id"], a.id)
    with pytest.raises(store.Conflict):
        add(hub, t["id"], a.id)
    b = hub.enroll("b")
    with pytest.raises(store.Invalid):
        add(hub, t["id"], b.id, stage="lead")
    with pytest.raises(store.Invalid):
        add(hub, t["id"], b.id, role="x" * 201)
    add(hub, t["id"], b.id, role="x" * 200)  # boundary is allowed
    for bad in (out, inactive):
        with pytest.raises(store.Invalid):
            add(hub, t["id"], bad.id)


def test_update_and_remove_validate(hub):
    a = hub.enroll("a")
    t = mk_task(hub)
    add(hub, t["id"], a.id)
    conn = hub.conn()
    with db.transaction(conn):
        with pytest.raises(store.Invalid):
            task_agents.update_agent(conn, t["id"], a.id, actor=OPERATOR, stage="lead")
        with pytest.raises(store.Invalid):
            task_agents.update_agent(conn, t["id"], a.id, actor=OPERATOR, role="x" * 201)
        got = task_agents.update_agent(conn, t["id"], a.id, actor=OPERATOR, role="New", stage="reviews")
        assert (got["agents"][0]["role"], got["agents"][0]["stage"]) == ("New", "reviews")
        got = task_agents.remove_agent(conn, t["id"], a.id, actor=OPERATOR)
        assert got["agents"] == []
        with pytest.raises(store.NotFound):
            task_agents.remove_agent(conn, t["id"], a.id, actor=OPERATOR)
        with pytest.raises(store.NotFound):
            task_agents.update_agent(conn, t["id"], a.id, actor=OPERATOR, role="x")


def test_membership_changes_emit_events(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    t = mk_task(hub)
    add(hub, t["id"], a.id)
    add(hub, t["id"], b.id)
    conn = hub.conn()
    with db.transaction(conn):
        task_agents.update_agent(conn, t["id"], b.id, actor=OPERATOR, role="Z")
        task_agents.remove_agent(conn, t["id"], a.id, actor=OPERATOR)
    rows = conn.execute(
        "SELECT type, project, payload FROM events WHERE type LIKE 'task.agent_%' ORDER BY id").fetchall()
    assert [r["type"] for r in rows] == [
        "task.agent_added", "task.agent_added", "task.agent_updated", "task.agent_removed"]
    for r in rows:
        assert r["project"] == "proj"
        p = json.loads(r["payload"])
        assert set(p) >= {"task_id", "agent_id", "agents"}
    assert json.loads(rows[-1]["payload"])["agents"] == [b.id]
    assert json.loads(rows[0]["payload"])["agents"] == [a.id]


def test_migration_backfills_assignee(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for version, script in enumerate(db.MIGRATIONS[:4], start=1):
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {version};COMMIT;")
    budget = json.dumps({"max_turns": 3, "wall_clock_minutes": 4, "max_subtasks": 1})
    conn.execute(
        "INSERT INTO agents (id, name, device_name, device_ip, device_node_id, token_hash, status, created_at) "
        "VALUES ('ag1', 'one', 'd', '1.1.1.1', 'n', 'h', 'active', 'x')")
    conn.execute("INSERT INTO projects VALUES ('p', '', 'x')")
    conn.execute(
        "INSERT INTO tasks (project, title, created_by, assignee, state, budget, created_at, updated_at) "
        "VALUES ('p', 't', 'ag1', 'ag1', 'open', ?, 'x', 'x')", (budget,))
    conn.execute(
        "INSERT INTO tasks (project, title, created_by, state, budget, created_at, updated_at) "
        "VALUES ('p', 'u', 'ag1', 'open', ?, 'x', 'x')", (budget,))
    conn.close()
    new = db.connect(path)
    db.migrate(new)
    rows = new.execute("SELECT * FROM task_agents").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert (r["task_id"], r["agent_id"], r["role"], r["stage"]) == (1, "ag1", "", "works")
    assert json.loads(r["budget"]) == json.loads(budget)
    assert new.execute("SELECT assignee FROM tasks WHERE id = 1").fetchone()[0] is None


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_assignee_column_is_dead():
    """The ``tasks.assignee`` column is no longer read or written by migrated code.

    Precise check: ``task_agents.py`` must not contain the word ``assignee`` at all
    (outside ``#`` comments). In ``tasks.py`` the keyword *parameter* ``assignee`` of
    ``create_task`` is part of the interface, so there we only forbid string literals
    mentioning it (SQL text, dict keys, event payload keys) inside the functions and
    constants migrated in this task: ``_SELECT``, ``_row``, ``get_task``, ``list_tasks``,
    ``create_task`` and ``assign``. Later tasks extend this to the remaining functions
    and modules.
    """
    for name in ("task_agents.py", "bus.py", "../runner/daemon.py"):
        assert "assignee" not in _strip_comments((SRC / name).read_text()), name
    # app.py: the request model field ``assignee`` of CreateTaskRequest is the one wire-format use.
    assert "assignee" not in _strip_comments((SRC / "app.py").read_text()).replace(
        "assignee: str | None", "").replace("assignee=body.assignee", ""), "app.py"
    # approvals.py: the task_reassign details key "assignee" is wire format; no error text may use it.
    for sub in ast.walk(ast.parse((SRC / "approvals.py").read_text())):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            assert "assignee" not in sub.value.replace('"assignee"', "") or sub.value == "assignee", sub.value
    tree = ast.parse((SRC / "tasks.py").read_text())
    migrated = {"_row", "get_task", "list_tasks", "create_task", "assign", "transition", "add_note",
                "_require_member", "start_run", "end_run", "record_progress"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in migrated]
    nodes += [n for n in tree.body if isinstance(n, ast.Assign)
              and any(getattr(t, "id", "") == "_SELECT" for t in n.targets)]
    assert len(nodes) == len(migrated) + 1
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                assert "assignee" not in sub.value, (node, sub.value)


def test_list_tasks_agent_filter(hub):
    a = hub.enroll("a")
    t1 = mk_task(hub)
    mk_task(hub)
    add(hub, t1["id"], a.id)
    got = tasks.list_tasks(hub.conn(), projects=None, agent=a.id)
    assert [t["id"] for t in got] == [t1["id"]]


def test_assign_makes_agent_only_works_and_keeps_reviewers(hub):
    a, b, c = hub.enroll("a"), hub.enroll("b"), hub.enroll("c")
    t = mk_task(hub, assignee=a.id, role="Impl")
    add(hub, t["id"], c.id, "Rev", "reviews")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET state = 'in_progress' WHERE id = ?", (t["id"],))
        got = tasks.assign(conn, t["id"], b.id, actor=OPERATOR, role="New")
    assert got["state"] == "open"
    assert sorted((m["name"], m["stage"]) for m in got["agents"]) == [("b", "works"), ("c", "reviews")]
    assert task_agents.member(got, b.id)["role"] == "New"
    assert task_agents.member(got, a.id) is None


def test_assign_same_agent_keeps_role(hub):
    a = hub.enroll("a")
    t = mk_task(hub)
    add(hub, t["id"], a.id, "Implementer")
    conn = hub.conn()
    with db.transaction(conn):
        got = tasks.assign(conn, t["id"], a.id, actor=OPERATOR)
        assert task_agents.member(got, a.id)["role"] == "Implementer"
        got = tasks.assign(conn, t["id"], a.id, actor=OPERATOR, role="Lead")
        assert task_agents.member(got, a.id)["role"] == "Lead"


# --- finish, verdicts, send-back ---------------------------------------------------

def _tx(hub, fn, *a, **kw):
    conn = hub.conn()
    with db.transaction(conn):
        return fn(conn, *a, **kw)


def _agent(hub, name):
    return store.find_agent(hub.conn(), name)


def _state(hub, task_id):
    return tasks.get_task(hub.conn(), task_id)["state"]


def running(hub, *works, reviews=()):
    """A task in_progress with the given implementers and reviewers (agents enrolled here)."""
    t = mk_task(hub)
    for n in works:
        hub.enroll(n)
        add(hub, t["id"], n)
    for n in reviews:
        hub.enroll(n)
        add(hub, t["id"], n, stage="reviews")
    _tx(hub, tasks.transition, t["id"], "claimed", actor=OPERATOR, role="operator")
    _tx(hub, tasks.transition, t["id"], "in_progress", actor=OPERATOR, role="operator")
    return t


def fin(hub, t, name):
    return _tx(hub, task_agents.finish, t["id"], _agent(hub, name))


def verdict(hub, t, name, v, notes=""):
    return _tx(hub, task_agents.record_verdict, t["id"], _agent(hub, name), verdict=v, notes=notes)


def test_revoked_implementer_does_not_block_review(hub):
    t = running(hub, "a", "b")
    b = _agent(hub, "b")
    hub.set_status(type("C", (), {"id": b.id})(), "revoked")
    fin(hub, t, "a")
    assert _state(hub, t["id"]) == "review"


def test_removed_implementer_does_not_block_review(hub):
    t = running(hub, "a", "b")
    _tx(hub, task_agents.remove_agent, t["id"], "b", actor=OPERATOR)
    assert _state(hub, t["id"]) == "in_progress"
    fin(hub, t, "a")
    assert _state(hub, t["id"]) == "review"


def test_finish_rules(hub):
    t = running(hub, "a", reviews=("r",))
    hub.enroll("x")
    for who in ("r", "x"):
        with pytest.raises(store.Forbidden):
            fin(hub, t, who)
    assert all(m["finished_at"] is None for m in tasks.get_task(hub.conn(), t["id"])["agents"])
    fin(hub, t, "a")
    with pytest.raises(store.Conflict):  # already in review
        fin(hub, t, "a")
    ev = hub.conn().execute("SELECT payload FROM events WHERE type = 'task.agent_finished'").fetchall()
    assert len(ev) == 1 and json.loads(ev[0]["payload"])["agents"]


def test_finish_requires_in_progress(hub):
    t = mk_task(hub)
    hub.enroll("a")
    add(hub, t["id"], "a")
    with pytest.raises(store.Conflict):
        fin(hub, t, "a")


def _verdicts(hub, t):
    return {m["agent_id"]: (m["verdict"], m["verdict_notes"]) for m in tasks.get_task(hub.conn(), t["id"])["agents"]}


def test_verdict_rules(hub):
    t = running(hub, "a", reviews=("r",))
    hub.enroll("x")
    before = _verdicts(hub, t)
    with pytest.raises(store.Conflict):  # still in_progress
        verdict(hub, t, "r", "approve")
    fin(hub, t, "a")
    assert _state(hub, t["id"]) == "review"
    with pytest.raises(store.Forbidden):
        verdict(hub, t, "a", "approve")
    with pytest.raises(store.Forbidden):
        verdict(hub, t, "x", "approve")
    with pytest.raises(store.Invalid):
        verdict(hub, t, "r", "lgtm")
    assert _verdicts(hub, t) == before


def test_verdict_recorded_and_state_unchanged(hub):
    t = running(hub, "a", reviews=("r",))
    fin(hub, t, "a")
    verdict(hub, t, "r", "approve", "looks good")
    r = _agent(hub, "r")
    assert _verdicts(hub, t)[r.id] == ("approve", "looks good")
    assert _state(hub, t["id"]) == "review"
    row = hub.conn().execute("SELECT payload FROM events WHERE type = 'task.verdict'").fetchone()
    p = json.loads(row["payload"])
    assert p["verdict"] == "approve" and p["notes"] == "looks good" and p["agent_id"] == r.id
    assert r.id in p["agents"]


def test_operator_send_back_resets_finish_and_verdicts(hub):
    t = running(hub, "a", "b", reviews=("r",))
    fin(hub, t, "a")
    fin(hub, t, "b")
    verdict(hub, t, "r", "approve", "fine by me")
    _tx(hub, tasks.transition, t["id"], "in_progress", actor=OPERATOR, role="operator")
    for m in tasks.get_task(hub.conn(), t["id"])["agents"]:
        assert m["finished_at"] is None and m["verdict"] is None and m["verdict_notes"] == ""


def test_release_removes_member_and_reopens_when_last(hub):
    t = running(hub, "a", "b")
    a, b = _agent(hub, "a"), _agent(hub, "b")
    _tx(hub, tasks.transition, t["id"], "open", actor=a.id, role="agent", agent=a)
    assert _state(hub, t["id"]) == "in_progress"
    assert [m["agent_id"] for m in tasks.get_task(hub.conn(), t["id"])["agents"]] == [b.id]
    _tx(hub, tasks.transition, t["id"], "open", actor=b.id, role="agent", agent=b)
    got = tasks.get_task(hub.conn(), t["id"])
    assert got["state"] == "open" and got["agents"] == []


def test_task_events_carry_member_ids(hub):
    t = running(hub, "a", reviews=("r",))
    fin(hub, t, "a")
    ids = {_agent(hub, "a").id, _agent(hub, "r").id}
    for row in hub.conn().execute("SELECT type, payload FROM events WHERE type LIKE 'task.%' AND id > "
                                  "(SELECT MAX(id) FROM events WHERE type = 'task.agent_added')"):
        assert set(json.loads(row["payload"])["agents"]) == ids, row["type"]


# --- review round: advance on removal / revocation, release and unclaim with reviewers ---

def test_finish_then_remove_moves_to_review(hub):
    t = running(hub, "a", "b")
    fin(hub, t, "a")
    assert _state(hub, t["id"]) == "in_progress"
    _tx(hub, task_agents.remove_agent, t["id"], "b", actor=OPERATOR)
    assert _state(hub, t["id"]) == "review"


def test_finish_then_revoke_moves_to_review(hub):
    t = running(hub, "a", "b")
    fin(hub, t, "a")
    b = _agent(hub, "b")
    _tx(hub, store.set_agent_status, b.id, "revoked", actor=OPERATOR)
    assert _state(hub, t["id"]) == "review"


def test_removing_last_implementer_does_not_advance(hub):
    t = running(hub, "a", reviews=("r",))
    _tx(hub, task_agents.remove_agent, t["id"], "a", actor=OPERATOR)
    assert _state(hub, t["id"]) == "in_progress"


def test_last_implementer_release_reopens_despite_reviewers(hub):
    t = running(hub, "a", reviews=("r",))
    a = _agent(hub, "a")
    _tx(hub, tasks.transition, t["id"], "open", actor=a.id, role="agent", agent=a)
    got = tasks.get_task(hub.conn(), t["id"])
    assert got["state"] == "open"
    assert [(m["agent_id"], m["stage"]) for m in got["agents"]] == [(_agent(hub, "r").id, "reviews")]


def test_release_by_unfinished_completes_others_part(hub):
    t = running(hub, "a", "b")
    fin(hub, t, "a")
    b = _agent(hub, "b")
    _tx(hub, tasks.transition, t["id"], "open", actor=b.id, role="agent", agent=b)
    assert _state(hub, t["id"]) == "review"


def test_operator_unclaim_keeps_reviewers(hub):
    t = running(hub, "a", "b", reviews=("r",))
    _tx(hub, tasks.transition, t["id"], "open", actor=OPERATOR, role="operator")
    got = tasks.get_task(hub.conn(), t["id"])
    assert got["state"] == "open"
    assert [m["stage"] for m in got["agents"]] == ["reviews"]


def test_stage_change_can_complete_the_implementers_part(hub):
    t = running(hub, "a", "b")
    fin(hub, t, "a")
    _tx(hub, task_agents.update_agent, t["id"], "b", actor=OPERATOR, stage="reviews")
    assert _state(hub, t["id"]) == "review"


def test_stage_change_clears_the_old_stages_fields(hub):
    t = running(hub, "a", "b", "c")
    fin(hub, t, "a")
    _tx(hub, task_agents.update_agent, t["id"], "a", actor=OPERATOR, stage="reviews")
    a = [m for m in tasks.get_task(hub.conn(), t["id"])["agents"] if m["name"] == "a"][0]
    assert a["finished_at"] is None and a["finish_note"] == ""
    _tx(hub, tasks.transition, t["id"], "review", actor=OPERATOR, role="operator")
    verdict(hub, t, "a", "approve", "ok")
    _tx(hub, task_agents.update_agent, t["id"], "a", actor=OPERATOR, stage="works")
    a = [m for m in tasks.get_task(hub.conn(), t["id"])["agents"] if m["name"] == "a"][0]
    assert a["verdict"] is None and a["verdict_notes"] == ""


def test_operator_unblock_advances_when_remaining_implementers_finished(hub):
    t = running(hub, "a", "b")
    fin(hub, t, "a")
    _tx(hub, tasks.transition, t["id"], "blocked", actor=OPERATOR, role="operator")
    _tx(hub, task_agents.remove_agent, t["id"], "b", actor=OPERATOR)
    assert _state(hub, t["id"]) == "blocked"
    _tx(hub, tasks.transition, t["id"], "in_progress", actor=OPERATOR, role="operator")
    assert _state(hub, t["id"]) == "review"


def test_release_with_only_revoked_implementers_left_reopens(hub):
    t = running(hub, "a", "b", reviews=("r",))
    b = _agent(hub, "b")
    hub.set_status(type("C", (), {"id": b.id})(), "revoked")
    a = _agent(hub, "a")
    _tx(hub, tasks.transition, t["id"], "open", actor=a.id, role="agent", agent=a)
    assert _state(hub, t["id"]) == "open"


def test_claim_allowed_when_only_reviewers_remain(hub):
    t = running(hub, "a", reviews=("r",))
    a = _agent(hub, "a")
    _tx(hub, tasks.transition, t["id"], "open", actor=a.id, role="agent", agent=a)
    hub.enroll("c")
    c = _agent(hub, "c")
    _tx(hub, tasks.transition, t["id"], "claimed", actor=c.id, role="agent", agent=c)
    got = tasks.get_task(hub.conn(), t["id"])
    assert got["state"] == "claimed"
    assert sorted((m["name"], m["stage"]) for m in got["agents"]) == [("c", "works"), ("r", "reviews")]
