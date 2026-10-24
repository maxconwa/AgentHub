"""Archived tasks (0.3.11, task #46): marking a task done archives it; the at-a-glance lists leave it out, an explicit
ask (include_archived, state='done', the agent's archived list) shows it, and its page and chat stay reachable."""

import httpx
from mcp import Client
from typer.testing import CliRunner

from agenthub.cli import app as cli_app
from agenthub.client import HubClient
from agenthub.hub import chats, dashboard, db, store, task_agents, tasks
from agenthub.mcp_server import build_server

from .conftest import OPERATOR
from .test_restart import restart


def text(result) -> str:
    return "\n".join(c.text for c in result.content)


def new_task(hub, title="t", works=(), reviews=(), state="in_progress"):
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "proj", actor=OPERATOR)
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title=title)
        for name in works:
            task_agents.add_agent(conn, t["id"], name, role="", stage="works", actor=OPERATOR)
        for name in reviews:
            task_agents.add_agent(conn, t["id"], name, role="", stage="reviews", actor=OPERATOR)
        path = {"open": [], "claimed": ["claimed"], "in_progress": ["claimed", "in_progress"],
                "review": ["claimed", "in_progress", "review"], "done": ["claimed", "in_progress", "review", "done"]}
        for s in path[state]:
            tasks.transition(conn, t["id"], s, actor=OPERATOR, role="operator")
    return tasks.get_task(conn, t["id"])


def op_move(hub, task_id, state):
    conn = hub.conn()
    with db.transaction(conn):
        return tasks.transition(conn, task_id, state, actor=OPERATOR, role="operator")


def archived_at(hub, task_id):
    return hub.conn().execute("SELECT archived_at FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def ids(rows):
    return [t["id"] for t in rows]


# --- migration 28 ---------------------------------------------------------------------------------------------------


def test_migration_28_adds_the_column_and_archives_tasks_already_done(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    newest = db.MIGRATIONS[:28]  # only 28: later migrations may change other tables
    monkeypatch.setattr(db, "MIGRATIONS", newest[:27])
    conn = db.init_db(path)
    with db.transaction(conn):
        store.ensure_project(conn, "proj", actor=OPERATOR)
        for i, state in enumerate(("done", "cancelled", "rejected", "review", "open"), start=1):
            conn.execute("INSERT INTO tasks (id, project, title, created_by, state, budget, created_at, updated_at) "
                         "VALUES (?, 'proj', ?, ?, ?, '{}', 'c', ?)", (i, state, OPERATOR, state, f"u{i}"))
    before = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE name != 'tasks'")}
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS", newest)
    conn = db.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 28
    after = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE name != 'tasks'")}
    assert {k: v for k, v in after.items() if k in before} == before  # nothing else changed
    assert set(after) - set(before) == {"tasks_archived"}
    got = {r["state"]: r["archived_at"] for r in conn.execute("SELECT state, archived_at FROM tasks")}
    # done only, as of its last move; cancelled and rejected stay where they are
    assert got == {"done": "u1", "cancelled": None, "rejected": None, "review": None, "open": None}


def test_the_hub_archives_done_tasks_an_older_hub_left_unarchived_at_start(hub):
    """Roll back to 0.3.10 (which ignores archived_at), mark a task done, roll forward: migration 28 doesn't run
    again, so the start does it."""
    t = new_task(hub, state="review")
    conn = hub.conn()
    with db.transaction(conn):  # what 0.3.10's transition writes
        conn.execute("UPDATE tasks SET state = 'done', updated_at = 'then' WHERE id = ?", (t["id"],))
    assert archived_at(hub, t["id"]) is None
    again = restart(hub)
    with again.client:  # the lifespan runs
        assert archived_at(again, t["id"]) == "then"
        assert tasks.list_tasks(again.conn(), projects=None) == []
    conn = again.conn()
    with db.transaction(conn):
        assert tasks.archive_done(conn) == 0  # nothing left: it's idempotent


# --- every path to done archives ------------------------------------------------------------------------------------


def test_finish_then_the_operator_marks_done_archives_and_nothing_before_does(hub):
    a, b = hub.enroll("alice"), hub.enroll("bob")
    t = new_task(hub, works=["alice"], reviews=["bob"])
    r = hub.client.post(f"/api/v1/tasks/{t['id']}/finish", json={"note": "did it"}, headers=a.headers)
    assert r.status_code == 200 and r.json()["state"] == "review"
    assert archived_at(hub, t["id"]) is None  # finishing isn't done: it goes to review
    r = hub.client.post(f"/api/v1/tasks/{t['id']}/verdict", json={"verdict": "approve", "notes": "ok"},
                        headers=b.headers)
    assert r.status_code == 200
    assert archived_at(hub, t["id"]) is None  # an approval doesn't move the task; the operator does
    r = hub.client.post(f"/ui/tasks/{t['id']}/move", headers=hub.op_headers(),
                        data={"csrf": hub.csrf(), "state": "done"}, follow_redirects=False)
    assert r.status_code == 303
    fresh = tasks.get_task(hub.conn(), t["id"])
    assert fresh["state"] == "done" and fresh["archived_at"] == fresh["updated_at"]


def test_the_cli_marking_done_archives(tmp_path):
    from agenthub.cli import _hub_conn

    runner = CliRunner()
    assert runner.invoke(cli_app, ["hub", "init", "--no-systemd"]).exit_code == 0
    runner.invoke(cli_app, ["hub", "project", "add", "alpha"])
    conn = _hub_conn()
    with db.transaction(conn):
        store.ensure_project(conn, "alpha", actor=OPERATOR)
        t = tasks.create_task(conn, actor=OPERATOR, project="alpha", title="T")
        for s in ("claimed", "in_progress", "review"):
            tasks.transition(conn, t["id"], s, actor=OPERATOR, role="operator")
    conn.close()
    assert runner.invoke(cli_app, ["hub", "tasks", "move", str(t["id"]), "done"]).exit_code == 0
    assert f"#{t['id']}" not in runner.invoke(cli_app, ["hub", "tasks", "list"]).output
    listed = runner.invoke(cli_app, ["hub", "tasks", "list", "--archived"]).output
    assert f"#{t['id']}" in listed and "done" in listed
    conn = _hub_conn()
    assert tasks.get_task(conn, t["id"])["archived_at"] is not None


def test_cancelled_and_rejected_tasks_are_not_archived(hub):
    cancelled = new_task(hub, state="in_progress")
    op_move(hub, cancelled["id"], "cancelled")
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "proj", actor=OPERATOR)
        rejected = tasks.create_task(conn, actor=OPERATOR, project="proj", title="r", state="awaiting_approval")
        tasks.transition(conn, rejected["id"], "rejected", actor=OPERATOR, role="approval")
    assert archived_at(hub, cancelled["id"]) is None and archived_at(hub, rejected["id"]) is None
    assert set(ids(tasks.list_tasks(hub.conn(), projects=None))) == {cancelled["id"], rejected["id"]}


def test_any_move_but_done_clears_archived_at(hub):
    """done has no exits today, so a task can't leave it; should one be added, transition() un-archives on the way out
    as it does for any move but to done."""
    t = new_task(hub, state="review")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET archived_at = 'stale' WHERE id = ?", (t["id"],))
    op_move(hub, t["id"], "in_progress")  # sent back
    assert archived_at(hub, t["id"]) is None


# --- the lists ------------------------------------------------------------------------------------------------------


def test_list_tasks_hides_archived_unless_asked_or_filtered_to_done(hub):
    live = new_task(hub, "live", state="open")
    gone = new_task(hub, "gone", state="done")
    conn = hub.conn()
    assert ids(tasks.list_tasks(conn, projects=["proj"])) == [live["id"]]
    assert ids(tasks.list_tasks(conn, projects=None, include_archived=True)) == [live["id"], gone["id"]]
    assert ids(tasks.list_tasks(conn, projects=None, state="done")) == [gone["id"]]  # an explicit state='done'
    assert ids(tasks.list_tasks(conn, projects=None, state="open", include_archived=True)) == [live["id"]]


def test_archived_tasks_lists_an_agents_archived_tasks_in_either_stage_newest_first(hub):
    alice, _ = hub.enroll("alice"), hub.enroll("bob")
    first = new_task(hub, "first", works=["alice"], state="done")
    reviewed = new_task(hub, "reviewed", works=["bob"], reviews=["alice"], state="done")
    new_task(hub, "live", works=["alice"], state="in_progress")
    new_task(hub, "bobs", works=["bob"], state="done")
    conn = hub.conn()
    with db.transaction(conn):  # first was archived later than reviewed
        conn.execute("UPDATE tasks SET archived_at = '9999' WHERE id = ?", (first["id"],))
    got = tasks.archived_tasks(conn, alice.id)
    assert ids(got) == [first["id"], reviewed["id"]]
    assert all(t["archived_at"] and t["state"] == "done" for t in got)
    assert [m["name"] for m in got[1]["agents"]] == ["bob", "alice"]  # task dicts, as list_tasks returns
    assert ids(tasks.archived_tasks(conn, alice.id, limit=1)) == [first["id"]]
    assert tasks.archived_tasks(conn, "nobody") == []


def test_the_rest_api_hides_archived_tasks_by_default(hub):
    a = hub.enroll("alice")
    live = new_task(hub, "live", works=["alice"], state="open")
    gone = new_task(hub, "gone", works=["alice"], state="done")

    def listed(**params):
        r = hub.client.get("/api/v1/projects/proj/tasks", params=params, headers=a.headers)
        assert r.status_code == 200, r.text
        return ids(r.json()["tasks"])

    assert listed() == [live["id"]]
    assert listed(mine="true") == [live["id"]]
    assert listed(include_archived="true") == [live["id"], gone["id"]]
    assert listed(state="done") == [gone["id"]]
    [row] = hub.client.get("/api/v1/projects/proj/tasks", params={"state": "done"}, headers=a.headers).json()["tasks"]
    assert row["archived_at"]


async def test_mcp_list_tasks_hides_archived_and_include_archived_shows_them(dev_hub):
    a = dev_hub.enroll("alice")
    gone = new_task(dev_hub, "gone", works=["alice"], state="done")
    client = HubClient("http://hub", a.token, transport=httpx.ASGITransport(app=dev_hub.client.app))
    async with Client(build_server(lambda: client)) as c:
        hidden = text(await c.call_tool("list_tasks", {"project": "proj"}))
        assert f"task #{gone['id']}" not in hidden and "include_archived=true" in hidden
        shown = text(await c.call_tool("list_tasks", {"project": "proj", "include_archived": True}))
        assert f"task #{gone['id']} [done, archived]" in shown
        assert f"task #{gone['id']} [done, archived]" in text(
            await c.call_tool("list_tasks", {"project": "proj", "state": "done"}))
        mine = text(await c.call_tool("list_tasks", {"project": "proj", "state": "done", "mine": True}))
        assert f"task #{gone['id']}" in mine
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "include_archived" in tools["list_tasks"].input_schema["properties"]
        assert "archived" in tools["list_tasks"].description


async def test_the_client_sends_include_archived_only_when_asked(dev_hub):
    a = dev_hub.enroll("alice")
    seen = []

    async def spy(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"tasks": []})

    client = HubClient("http://hub", a.token, transport=httpx.MockTransport(spy))
    await client.list_tasks("proj", state="open", mine=True)  # the runner's poll, unchanged for an older hub
    await client.list_tasks("proj", include_archived=True)
    assert seen == [{"mine": "true", "state": "open"}, {"mine": "false", "include_archived": "true"}]


# --- chats ----------------------------------------------------------------------------------------------------------


def test_chat_overview_and_the_badge_leave_archived_task_chats_out(hub):
    a, b = hub.enroll("alice"), hub.enroll("bob")
    t = new_task(hub, works=["alice", "bob"], state="review")
    r = hub.client.post(f"/api/v1/chats/task/{t['id']}/messages", json={"body": "unread"}, headers=a.headers)
    assert r.status_code == 201
    conn = hub.conn()
    assert (["task", str(t["id"])] in [[o["kind"], o["ref"]] for o in chats.overview(conn)])
    before = dashboard.unread_total(conn)
    op_move(hub, t["id"], "done")
    assert all(not (o["kind"] == "task" and o["ref"] == str(t["id"])) for o in chats.overview(conn))
    assert dashboard.unread_total(conn) == before - 1
    kept = [o for o in chats.overview(conn, include_archived=True) if o["kind"] == "task"]
    assert [o["ref"] for o in kept] == [str(t["id"])] and kept[0]["unread"] == 1


def test_an_archived_tasks_page_and_chat_stay_reachable_and_agents_still_use_its_chat(hub):
    a, b = hub.enroll("alice"), hub.enroll("bob")
    t = new_task(hub, "Old work", works=["alice"], reviews=["bob"], state="done")
    assert archived_at(hub, t["id"])
    for path in (f"/ui/tasks/{t['id']}", f"/ui/chat/task/{t['id']}"):
        r = hub.client.get(path, headers=hub.op_headers())
        assert r.status_code == 200 and f"#{t['id']}" in r.text, path
    # archiving changes no delivery: the chat's members still read and write it as for any done task
    r = hub.client.post(f"/api/v1/chats/task/{t['id']}/messages", json={"body": "follow-up", "to": "bob"},
                        headers=a.headers)
    assert r.status_code == 201, r.text
    history = hub.client.get(f"/api/v1/chats/task/{t['id']}/messages", headers=b.headers)
    assert history.status_code == 200 and "follow-up" in history.text
