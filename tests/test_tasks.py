import pytest

from agenthub.hub import approvals, db, store, task_agents, tasks

from .conftest import OPERATOR


def op_task(hub, title="t", assignee=None, project="proj"):
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, project, actor=OPERATOR)
        return tasks.create_task(conn, actor=OPERATOR, project=project, title=title, assignee=assignee)


def move(hub, agent, task_id, state, **kw):
    return hub.client.post(f"/api/v1/tasks/{task_id}/update", json={"state": state, **kw}, headers=agent.headers)


def op_move(hub, task_id, state):
    conn = hub.conn()
    with db.transaction(conn):
        return tasks.transition(conn, task_id, state, actor=OPERATOR, role="operator")


def finish(hub, task_id, agent):
    conn = hub.conn()
    with db.transaction(conn):
        return task_agents.finish(conn, task_id, store.find_agent(conn, agent))


def decide(hub, approval_id, approve=True):
    conn = hub.conn()
    with db.transaction(conn):
        return approvals.decide(conn, approval_id, approve=approve, actor=OPERATOR)


# --- the state machine table ------------------------------------------------------

def test_every_agent_move_is_a_legal_transition():
    for current, new in tasks.AGENT_MOVES | tasks.APPROVAL_MOVES:
        assert new in tasks.TRANSITIONS[current]


def test_terminal_states_have_no_exits():
    for state in tasks.TERMINAL:
        assert state not in tasks.TRANSITIONS


@pytest.mark.parametrize("current,new", [
    ("review", "done"), ("awaiting_approval", "open"), ("open", "in_progress"),
    ("in_progress", "cancelled"), ("review", "in_progress"), ("proposed", "awaiting_approval"),
])
def test_agent_forbidden_moves(current, new):
    with pytest.raises((store.Forbidden, store.Conflict)):
        tasks.check_transition(current, new, "agent")


@pytest.mark.parametrize("current,new", [("done", "open"), ("open", "done"), ("rejected", "open"),
                                         ("open", "review"), ("cancelled", "open")])
def test_illegal_transitions_rejected_for_everyone(current, new):
    for role in ("agent", "operator", "approval"):
        with pytest.raises(store.Conflict):
            tasks.check_transition(current, new, role)


def test_unknown_state_and_role():
    with pytest.raises(store.Invalid):
        tasks.check_transition("open", "finished", "operator")
    with pytest.raises(store.Invalid):
        tasks.check_transition("open", "claimed", "root")


# --- lifecycle over the API ------------------------------------------------------

def test_agent_created_task_waits_for_approval(hub):
    a = hub.enroll("a")
    r = hub.client.post("/api/v1/projects/proj/tasks", json={"title": "refactor", "body": "do it"},
                        headers=a.headers)
    assert r.status_code == 201
    task, approval = r.json()["task"], r.json()["approval"]
    assert task["state"] == "awaiting_approval" and approval["kind"] == "task_create"
    assert task["approval_id"] == approval["id"]
    # cannot be claimed until approved
    assert hub.client.post(f"/api/v1/tasks/{task['id']}/claim", headers=a.headers).status_code == 409
    decide(hub, approval["id"])
    assert hub.client.get(f"/api/v1/tasks/{task['id']}", headers=a.headers).json()["state"] == "open"


def test_rejected_task_creation(hub):
    a = hub.enroll("a")
    r = hub.client.post("/api/v1/projects/proj/tasks", json={"title": "x"}, headers=a.headers).json()
    decide(hub, r["approval"]["id"], approve=False)
    assert hub.client.get(f"/api/v1/tasks/{r['task']['id']}", headers=a.headers).json()["state"] == "rejected"


def test_operator_created_task_skips_approval(hub):
    t = op_task(hub)
    assert t["state"] == "open" and t["approval_id"] is None


def test_full_agent_lifecycle_stops_at_review(hub):
    a = hub.enroll("a")
    t = op_task(hub)
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers).json()["agents"][0]["agent_id"] == a.id
    assert move(hub, a, t["id"], "in_progress").json()["state"] == "in_progress"
    assert move(hub, a, t["id"], "blocked").json()["state"] == "blocked"
    assert move(hub, a, t["id"], "in_progress").json()["state"] == "in_progress"
    assert move(hub, a, t["id"], "review").status_code == 403  # agents finish; the hub moves to review
    finish(hub, t["id"], "a")
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "review"
    assert move(hub, a, t["id"], "done").status_code == 403
    assert op_move(hub, t["id"], "done")["state"] == "done"


def test_non_member_cannot_claim_or_move(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    t = op_task(hub, assignee="a")
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=b.headers).status_code == 403
    hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers)
    assert move(hub, b, t["id"], "in_progress").status_code == 403
    assert move(hub, b, t["id"], None, note="hi").status_code == 403


def test_unclaim_returns_task_to_pool(hub):
    a = hub.enroll("a")
    t = op_task(hub)
    hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers)
    r = move(hub, a, t["id"], "open").json()
    assert r["state"] == "open" and r["agents"] == []


def test_reassign_puts_claimed_task_back_to_open(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    t = op_task(hub, assignee="a")
    hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers)
    conn = hub.conn()
    with db.transaction(conn):
        r = tasks.assign(conn, t["id"], "b", actor=OPERATOR)
    assert r["state"] == "open" and [m["agent_id"] for m in r["agents"]] == [b.id]


def test_agent_reassign_requires_approval(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    t = op_task(hub, assignee="a")
    r = hub.client.post("/api/v1/approvals", headers=a.headers, json={
        "kind": "task_reassign", "reason": "b knows this code", "details": {"task_id": t["id"], "assignee": "b"}})
    assert r.status_code == 201
    assert hub.client.get(f"/api/v1/tasks/{t['id']}", headers=a.headers).json()["agents"][0]["agent_id"] == a.id
    decide(hub, r.json()["id"])
    assert hub.client.get(f"/api/v1/tasks/{t['id']}", headers=a.headers).json()["agents"][0]["agent_id"] == b.id


def test_tasks_in_other_projects_are_invisible(hub):
    a = hub.enroll("a", projects=("proj",))
    t = op_task(hub, project="secret")
    assert hub.client.get(f"/api/v1/tasks/{t['id']}", headers=a.headers).status_code == 404
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers).status_code == 404
    assert hub.client.get("/api/v1/projects/secret/tasks", headers=a.headers).status_code == 403


def test_list_tasks_filters(hub):
    a = hub.enroll("a")
    op_task(hub, "one")
    t2 = op_task(hub, "two")
    hub.client.post(f"/api/v1/tasks/{t2['id']}/claim", headers=a.headers)
    mine = hub.client.get("/api/v1/projects/proj/tasks", params={"mine": True}, headers=a.headers).json()
    assert [t["title"] for t in mine["tasks"]] == ["two"]
    opened = hub.client.get("/api/v1/projects/proj/tasks", params={"state": "open"}, headers=a.headers).json()
    assert [t["title"] for t in opened["tasks"]] == ["one"]


@pytest.mark.parametrize("budget", [{"max_turns": 0}, {"max_turns": 10_000}, {"bogus": 1},
                                    {"wall_clock_minutes": -1}])
def test_budget_validation(hub, budget):
    a = hub.enroll("a")
    r = hub.client.post("/api/v1/projects/proj/tasks", json={"title": "x", "budget": budget}, headers=a.headers)
    assert r.status_code == 422


def test_task_in_missing_project(hub):
    conn = hub.conn()
    with pytest.raises(store.NotFound), db.transaction(conn):
        tasks.create_task(conn, actor=OPERATOR, project="nope", title="x")


def test_default_budget_applied(hub):
    t = op_task(hub)
    assert t["budget"] == tasks.DEFAULT_BUDGET


def test_transitions_emit_events(hub):
    a = hub.enroll("a")
    t = op_task(hub)
    hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers)
    move(hub, a, t["id"], "in_progress", note="starting")
    import json

    rows = hub.conn().execute("SELECT payload FROM events WHERE type = 'task.transitioned' ORDER BY id")
    moves = [(p["from"], p["to"], p["note"]) for p in (json.loads(r["payload"]) for r in rows)]
    assert moves == [("open", "claimed", ""), ("claimed", "in_progress", "starting")]


def test_compare_and_set_detects_concurrent_change(hub):
    a = hub.enroll("a")
    t = op_task(hub)
    conn = hub.conn()
    agent = store.get_agent(conn, a.id)
    original = tasks.get_task
    # Simulate another writer changing the state between read and write.
    def stale_get(c, task_id):
        row = original(c, task_id)
        c.execute("UPDATE tasks SET state = 'cancelled' WHERE id = ?", (task_id,))
        return row
    tasks.get_task = stale_get
    try:
        with pytest.raises(store.Conflict), db.transaction(conn):
            tasks.transition(conn, t["id"], "claimed", actor=a.id, role="agent", agent=agent)
    finally:
        tasks.get_task = original


def test_only_assignee_can_record_runs_and_progress(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    t = op_task(hub, assignee="a")
    hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers)
    move(hub, a, t["id"], "in_progress")
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/runs", headers=b.headers).status_code == 403
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/progress", json={"items": [{"kind": "text", "text": "x"}]},
                           headers=b.headers).status_code == 403
    run = hub.client.post(f"/api/v1/tasks/{t['id']}/runs", headers=a.headers).json()
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/runs", headers=a.headers).status_code == 409  # one at a time
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/runs/{run['id']}/end", json={"outcome": "success"},
                           headers=b.headers).status_code == 403
    end = hub.client.post(f"/api/v1/tasks/{t['id']}/runs/{run['id']}/end",
                          json={"outcome": "success", "num_turns": 4}, headers=a.headers)
    assert end.json()["runs"] == 1 and end.json()["turns"] == 4


def test_runs_require_in_progress(hub):
    a = hub.enroll("a")
    t = op_task(hub, assignee="a")
    hub.client.post(f"/api/v1/tasks/{t['id']}/claim", headers=a.headers)
    assert hub.client.post(f"/api/v1/tasks/{t['id']}/runs", headers=a.headers).status_code == 409


def test_budget_extension_is_decided_by_hub_not_agent(hub):
    a = hub.enroll("a")
    t = op_task(hub, assignee="a")
    r = hub.client.post("/api/v1/approvals", headers=a.headers, json={
        "kind": "budget_exceeded", "reason": "need more",
        "details": {"task_id": t["id"], "limit": "max_turns", "extend_to": 100000}})
    assert r.json()["details"]["extend_to"] == tasks.DEFAULT_BUDGET["max_turns"] * 2
    decide(hub, r.json()["id"])
    assert tasks.get_task(hub.conn(), t["id"])["budget"]["max_turns"] == tasks.DEFAULT_BUDGET["max_turns"] * 2
    bad = hub.client.post("/api/v1/approvals", headers=a.headers, json={
        "kind": "budget_exceeded", "details": {"task_id": t["id"], "limit": "infinite"}})
    assert bad.status_code == 422


# --- membership-based state machine ----------------------------------------------

def claim(hub, agent, task_id):
    return hub.client.post(f"/api/v1/tasks/{task_id}/claim", headers=agent.headers)


def members(hub, task_id):
    return {m["agent_id"]: m for m in tasks.get_task(hub.conn(), task_id)["agents"]}


def add_member(hub, task_id, agent, stage="works", role="r"):
    conn = hub.conn()
    with db.transaction(conn):
        return task_agents.add_agent(conn, task_id, agent, role=role, stage=stage, actor=OPERATOR)


def test_claim_unassigned_task_adds_claimant_as_implementer(hub):
    a = hub.enroll("a")
    t = op_task(hub)
    r = claim(hub, a, t["id"])
    assert r.status_code == 200 and r.json()["state"] == "claimed"
    m = members(hub, t["id"])[a.id]
    assert m["stage"] == "works" and m["role"] == ""


def test_non_member_cannot_claim_or_move_403(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    t = op_task(hub, assignee="a")
    assert claim(hub, b, t["id"]).status_code == 403
    claim(hub, a, t["id"])
    assert move(hub, b, t["id"], "in_progress").status_code == 403
    assert set(members(hub, t["id"])) == {a.id}


def test_reviewer_cannot_move_state(hub):
    a, r = hub.enroll("a"), hub.enroll("r")
    t = op_task(hub, assignee="a")
    add_member(hub, t["id"], "r", stage="reviews")
    claim(hub, a, t["id"])
    move(hub, a, t["id"], "in_progress")
    assert move(hub, r, t["id"], "blocked").status_code == 403
    assert claim(hub, r, t["id"]).status_code in (403, 409)
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "in_progress"
    add_note = hub.client.post(f"/api/v1/tasks/{t['id']}/update", json={"note": "x"}, headers=r.headers)
    assert add_note.status_code == 200  # notes need membership only


def test_agents_cannot_move_to_review_directly(hub):
    a = hub.enroll("a")
    t = op_task(hub, assignee="a")
    claim(hub, a, t["id"])
    move(hub, a, t["id"], "in_progress")
    assert move(hub, a, t["id"], "review").status_code == 403
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "in_progress"


def test_system_role_may_only_move_to_review():
    tasks.check_transition("in_progress", "review", "system")
    with pytest.raises(store.Forbidden):
        tasks.check_transition("review", "done", "system")
    with pytest.raises(store.Forbidden):
        tasks.check_transition("open", "claimed", "system")


def in_progress_task(hub, *names, project="proj"):
    t = op_task(hub, project=project)
    for n in names:
        add_member(hub, t["id"], n)
    conn = hub.conn()
    with db.transaction(conn):
        tasks.transition(conn, t["id"], "claimed", actor=OPERATOR, role="operator")
        tasks.transition(conn, t["id"], "in_progress", actor=OPERATOR, role="operator")
    return t


def test_task_moves_to_review_when_all_implementers_finish(hub):
    hub.enroll("a"), hub.enroll("b")
    t = in_progress_task(hub, "a", "b")
    finish(hub, t["id"], "a")
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "in_progress"
    finish(hub, t["id"], "b")
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "review"
    row = hub.conn().execute(
        "SELECT actor, payload FROM events WHERE type = 'task.transitioned' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["actor"] == "system:hub"
