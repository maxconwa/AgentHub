"""0.3.9.1 review flow: a reviewer's changes verdict sends the task back and wakes its builders; approve doesn't move
it; a task entering review with no reviewer gets the project's reviewers, which only the operator sets."""

import json
import sqlite3

import pytest

from agenthub.hub import db, reviewers, store, task_agents, tasks

from .conftest import OPERATOR


def mk_task(hub, project="proj"):
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, project, actor=OPERATOR)
        return tasks.create_task(conn, actor=OPERATOR, project=project, title="t")


def tx(hub, fn, *a, **kw):
    conn = hub.conn()
    with db.transaction(conn):
        return fn(conn, *a, **kw)


def agent(hub, name):
    return store.find_agent(hub.conn(), name)


def running(hub, works, reviews=(), project="proj"):
    t = mk_task(hub, project)
    for n in works:
        tx(hub, task_agents.add_agent, t["id"], n, role="Builder", stage="works", actor=OPERATOR)
    for n in reviews:
        tx(hub, task_agents.add_agent, t["id"], n, role="Reviewer", stage="reviews", actor=OPERATOR)
    tx(hub, tasks.transition, t["id"], "claimed", actor=OPERATOR, role="operator")
    tx(hub, tasks.transition, t["id"], "in_progress", actor=OPERATOR, role="operator")
    return t


def finish_all(hub, t, *names):
    for n in names:
        tx(hub, task_agents.finish, t["id"], agent(hub, n), note=f"{n} done")
    return tasks.get_task(hub.conn(), t["id"])


def verdict_api(hub, creds, task_id, verdict, notes=""):
    return hub.client.post(f"/api/v1/tasks/{task_id}/verdict", json={"verdict": verdict, "notes": notes},
                           headers=creds.headers)


def task_chat(hub, task_id):
    conn = hub.conn()
    return [dict(r) for r in conn.execute(
        "SELECT m.* FROM chat_messages m JOIN chats c ON c.id = m.chat_id WHERE c.kind = 'task' AND c.ref = ? "
        "ORDER BY m.id", (str(task_id),))]


# --- the changes verdict --------------------------------------------------------------------------------------------


def test_changes_verdict_sends_the_task_back_with_the_notes(hub):
    b1, b2, r = hub.enroll("b1"), hub.enroll("b2"), hub.enroll("rev")
    t = running(hub, ["b1", "b2"], ["rev"])
    assert finish_all(hub, t, "b1", "b2")["state"] == "review"
    resp = verdict_api(hub, r, t["id"], "changes", "the migration drops a column")
    assert resp.status_code == 200, resp.text
    got = resp.json()
    assert got["state"] == "in_progress"
    assert got["last_note"] == "the migration drops a column" and got["last_moved_by"] == r.id
    for m in got["agents"]:  # a new round: builders unfinished, verdicts cleared
        assert m["finished_at"] is None and m["verdict"] is None
    moved = [json.loads(e["payload"]) for e in hub.conn().execute(
        "SELECT payload, actor FROM events WHERE type = 'task.transitioned' AND actor = ?", (r.id,))]
    assert moved == [{"task_id": t["id"], "from": "review", "to": "in_progress",
                      "note": "the migration drops a column", "agents": [b1.id, b2.id, r.id]}]


def test_changes_verdict_wakes_each_builder_in_the_task_chat(hub):
    b1, b2, r = hub.enroll("b1"), hub.enroll("b2"), hub.enroll("rev")
    t = running(hub, ["b1", "b2"], ["rev"])
    finish_all(hub, t, "b1", "b2")
    verdict_api(hub, r, t["id"], "changes", "rename the flag")
    sent = [m for m in task_chat(hub, t["id"]) if m["kind"] == "text" and m["author"] == r.id]
    assert [json.loads(m["meta"])["to"] for m in sent] == [b1.id, b2.id]
    for m in sent:
        assert "rename the flag" in m["body"] and f"task #{t['id']}" in m["body"]
        assert json.loads(m["meta"])["send_back"] is True


def test_a_revoked_builder_is_not_woken(hub):
    hub.enroll("b1")
    gone, r = hub.enroll("b2"), hub.enroll("rev")
    t = running(hub, ["b1", "b2"], ["rev"])
    finish_all(hub, t, "b1", "b2")
    hub.set_status(gone, "revoked")
    verdict_api(hub, r, t["id"], "changes", "again")
    to = [json.loads(m["meta"]).get("to") for m in task_chat(hub, t["id"]) if m["author"] == r.id]
    assert to == [agent(hub, "b1").id]


def test_approve_does_not_move_the_task(hub):
    hub.enroll("b1")
    r = hub.enroll("rev")
    t = running(hub, ["b1"], ["rev"])
    finish_all(hub, t, "b1")
    resp = verdict_api(hub, r, t["id"], "approve", "ship it")
    assert resp.status_code == 200 and resp.json()["state"] == "review"
    me = task_agents.member(resp.json(), r.id)
    assert me["verdict"] == "approve" and me["verdict_notes"] == "ship it"
    assert not [m for m in task_chat(hub, t["id"]) if m["author"] == r.id]  # nobody is woken


def test_only_a_reviewer_on_the_task_sends_it_back(hub):
    b, r = hub.enroll("b1"), hub.enroll("rev")
    t = running(hub, ["b1"], ["rev"])
    finish_all(hub, t, "b1")
    assert verdict_api(hub, b, t["id"], "changes", "x").status_code == 403  # a builder can't
    outsider = hub.enroll("out")
    assert verdict_api(hub, outsider, t["id"], "changes", "x").status_code == 403
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "review"
    # The reviewer role makes only that one move, and only as a reviewer of the task.
    with pytest.raises(store.Forbidden):
        tasks.check_transition("review", "done", "reviewer")
    with pytest.raises(store.Forbidden):
        tasks.check_transition("in_progress", "review", "reviewer")
    with pytest.raises(store.Forbidden):
        tx(hub, tasks.transition, t["id"], "in_progress", actor=b.id, role="reviewer", agent=agent(hub, "b1"))
    # And an agent's own update_task can't use it: review → in_progress stays the operator's and a reviewer's.
    resp = hub.client.patch(f"/api/v1/tasks/{t['id']}", json={"state": "in_progress"}, headers=b.headers)
    assert resp.status_code in (403, 404, 405, 409)
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "review"


def test_a_changes_verdict_outside_review_is_refused(hub):
    hub.enroll("b1")
    r = hub.enroll("rev")
    t = running(hub, ["b1"], ["rev"])
    assert verdict_api(hub, r, t["id"], "changes", "too early").status_code == 409
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "in_progress"


# --- project reviewers ----------------------------------------------------------------------------------------------


def set_reviewers(hub, project, names):
    return tx(hub, reviewers.set_reviewers, project, names, actor=OPERATOR)


def test_a_task_entering_review_with_no_reviewer_gets_the_projects_reviewers(hub):
    hub.enroll("b1")
    r1, r2 = hub.enroll("rev1"), hub.enroll("rev2")
    t = running(hub, ["b1"])
    set_reviewers(hub, "proj", ["rev1", "rev2"])
    got = finish_all(hub, t, "b1")
    assert got["state"] == "review"
    added = [m for m in got["agents"] if m["stage"] == "reviews"]
    assert [(m["agent_id"], m["role"], m["added_by"]) for m in added] == [
        (r1.id, reviewers.ROLE, "system:hub"), (r2.id, reviewers.ROLE, "system:hub")]
    events = [e["type"] for e in hub.conn().execute("SELECT type FROM events WHERE actor = 'system:hub'")]
    assert events.count("task.agent_added") == 2


def test_a_task_that_has_a_reviewer_keeps_its_own(hub):
    hub.enroll("b1")
    mine = hub.enroll("mine")
    hub.enroll("rev1")
    t = running(hub, ["b1"], ["mine"])
    set_reviewers(hub, "proj", ["rev1"])
    got = finish_all(hub, t, "b1")
    assert [m["agent_id"] for m in got["agents"] if m["stage"] == "reviews"] == [mine.id]


def test_auto_reviewers_skip_builders_and_agents_that_lost_access(hub):
    b1 = hub.enroll("b1")
    hub.enroll("rev1")
    paused = hub.enroll("rev2")
    t = running(hub, ["b1"])
    set_reviewers(hub, "proj", ["b1", "rev1", "rev2"])
    hub.set_status(paused, "revoked")
    conn = hub.conn()  # rev1 loses the project
    with db.transaction(conn):
        conn.execute("UPDATE agents SET allowed_projects = ? WHERE name = 'rev1'", (json.dumps(["other"]),))
    got = finish_all(hub, t, "b1")
    assert got["state"] == "review"
    assert [m["agent_id"] for m in got["agents"]] == [b1.id]  # nobody to add; it still enters review


def test_a_dead_reviewer_does_not_count_as_one(hub):
    hub.enroll("b1")
    old, new = hub.enroll("old"), hub.enroll("new")
    t = running(hub, ["b1"], ["old"])
    hub.set_status(old, "revoked")
    set_reviewers(hub, "proj", ["new"])
    got = finish_all(hub, t, "b1")
    assert new.id in [m["agent_id"] for m in got["agents"] if m["stage"] == "reviews"]


def test_a_sent_back_task_reenters_review_with_the_same_reviewers(hub):
    hub.enroll("b1")
    r = hub.enroll("rev1")
    t = running(hub, ["b1"])
    set_reviewers(hub, "proj", ["rev1"])
    finish_all(hub, t, "b1")
    verdict_api(hub, r, t["id"], "changes", "one more thing")
    got = finish_all(hub, t, "b1")
    assert got["state"] == "review"
    assert [m["agent_id"] for m in got["agents"] if m["stage"] == "reviews"] == [r.id]


def test_set_reviewers_checks_its_agents(hub):
    hub.enroll("rev1")
    hub.enroll("out", projects=("other",))
    mk_task(hub)
    with pytest.raises(store.Invalid):
        set_reviewers(hub, "proj", ["out"])
    with pytest.raises(store.NotFound):
        set_reviewers(hub, "proj", ["nobody"])
    with pytest.raises(store.NotFound):
        set_reviewers(hub, "nope", ["rev1"])
    assert [r["name"] for r in set_reviewers(hub, "PROJ", ["rev1", "rev1"])] == ["rev1"]  # names ignore case
    assert set_reviewers(hub, "proj", []) == []


# --- the operator-only routes ---------------------------------------------------------------------------------------


def url(project="proj"):
    return f"/ui/api/projects/{project}/reviewers"


def test_the_operator_reads_and_sets_a_projects_reviewers(hub):
    r1, r2 = hub.enroll("rev1"), hub.enroll("rev2")
    mk_task(hub)
    resp = hub.client.get(url(), headers=hub.op_headers())
    assert resp.status_code == 200 and resp.json() == {"project": "proj", "reviewers": []}
    resp = hub.client.put(url(), json={"reviewers": ["rev2", r1.id]},
                          headers=hub.op_headers(**{"x-csrf-token": hub.csrf()}))
    assert resp.status_code == 200, resp.text
    assert [(r["agent_id"], r["name"], r["status"], r["allowed"]) for r in resp.json()["reviewers"]] == [
        (r2.id, "rev2", "active", True), (r1.id, "rev1", "active", True)]
    assert [r["agent_id"] for r in hub.client.get(url(), headers=hub.op_headers()).json()["reviewers"]] == [
        r2.id, r1.id]
    ev = hub.conn().execute("SELECT actor, payload FROM events WHERE type = 'project.reviewers'").fetchone()
    assert ev["actor"].startswith("human:") and json.loads(ev["payload"])["agents"] == [r2.id, r1.id]


def test_reviewer_route_errors_are_json(hub):
    hub.enroll("out", projects=("other",))
    mk_task(hub)
    resp = hub.client.put(url(), json={"reviewers": ["out"]}, headers=hub.op_headers(**{"x-csrf-token": hub.csrf()}))
    assert resp.status_code == 422 and "out" in resp.json()["detail"]
    resp = hub.client.get(url("nope"), headers=hub.op_headers())
    assert resp.status_code == 404 and resp.json()["detail"]


def test_the_reviewer_routes_are_operator_only(hub):
    a = hub.enroll("a")
    mk_task(hub)
    put = {"reviewers": ["a"]}
    # an agent token, with or without the operator's identity headers alongside it
    assert hub.client.get(url(), headers=a.headers).status_code in (401, 403)
    assert hub.client.put(url(), json=put, headers=a.headers).status_code in (401, 403)
    forged = {**hub.op_headers(**{"x-csrf-token": hub.csrf()}), "authorization": a.headers["authorization"]}
    assert hub.client.put(url(), json=put, headers=forged).status_code in (401, 403)
    # no identity at all
    assert hub.client.get(url()).status_code in (401, 403)
    # the operator without the CSRF token, or cross-site
    assert hub.client.put(url(), json=put, headers=hub.op_headers()).status_code == 403
    cross = hub.op_headers(**{"x-csrf-token": hub.csrf(), "sec-fetch-site": "cross-site"})
    assert hub.client.put(url(), json=put, headers=cross).status_code == 403
    # no agent API route sets them either
    assert hub.client.put("/api/v1/projects/proj/reviewers", json=put, headers=a.headers).status_code in (404, 405)
    assert hub.conn().execute("SELECT COUNT(*) FROM project_reviewers").fetchone()[0] == 0


def test_the_reviewers_migration_only_adds(tmp_path):
    path = tmp_path / "hub.sqlite3"
    db.init_db(path).close()
    conn = sqlite3.connect(path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(project_reviewers)")]
    assert cols == ["project", "agent_id", "added_by", "added_at"]
    script = next(m for m in db.MIGRATIONS if "project_reviewers" in m)
    for word in ("DROP", "ALTER", "DELETE", "UPDATE", "RENAME"):
        assert word not in script.upper()
    conn.executescript(script)  # IF NOT EXISTS: running it again (a moved list position) is harmless
