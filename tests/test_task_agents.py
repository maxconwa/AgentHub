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
    assert "assignee" not in _strip_comments((SRC / "task_agents.py").read_text())
    tree = ast.parse((SRC / "tasks.py").read_text())
    migrated = {"_row", "get_task", "list_tasks", "create_task", "assign"}
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
