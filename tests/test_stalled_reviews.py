"""No more stalled reviews (0.3.11, task #51): what stalled review tasks #39 and #44, replayed, and each fix.

How #39 stalled: hub-reviewer's review task was its own ``works`` task. Its session finished before any branch was
ready, so the task went to review with nobody to review it. The builders then handed off in their task chats without
addressing anyone, which woke nobody. hub-reviewer couldn't move its task back out of review (403), and no approval
kind could reopen it. The fixes:

(a) any agent on a task may reopen it, review → in_progress, and it has to finish again to get back to review;
(b) an implementer's unaddressed message in a task chat while the task is in review wakes the task's reviewers, and
    the finish that moves a task to review wakes them too, all through wake chains, loop protection and pauses;
(c) a ``task_reopen`` approval that really reopens the task;
(d) a ``stalled_review`` flag (views, doctor) on a task left in review with no reviewer session and no verdict.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from agenthub import doctor, paths
from agenthub.config import HubConfig, check_reviews
from agenthub.hub import approvals, chats, db, reviewers, stalls, store, task_agents, tasks, views, wakes

from .conftest import OPERATOR, make_hub


def api(hub, agent, method, path, **kw):
    return hub.client.request(method, f"/api/v1{path}", headers=agent.headers, **kw)


def tx(hub, fn, *a, **kw):
    conn = hub.conn()
    with db.transaction(conn):
        return fn(conn, *a, **kw)


def agent(hub, creds):
    return store.find_agent(hub.conn(), creds.id)


def running(hub, works, reviews=(), project="proj", title="t"):
    """A task in progress with these implementers and reviewers (AgentCreds)."""
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, project, actor=OPERATOR)
        t = tasks.create_task(conn, actor=OPERATOR, project=project, title=title)
        for a in works:
            task_agents.add_agent(conn, t["id"], a.id, role="Builder", stage="works", actor=OPERATOR)
        for a in reviews:
            task_agents.add_agent(conn, t["id"], a.id, role="Reviewer", stage="reviews", actor=OPERATOR)
        tasks.transition(conn, t["id"], "claimed", actor=OPERATOR, role="operator")
        tasks.transition(conn, t["id"], "in_progress", actor=OPERATOR, role="operator")
    return tasks.get_task(hub.conn(), t["id"])


def finish(hub, creds, task_id, note="done"):
    r = api(hub, creds, "POST", f"/tasks/{task_id}/finish", json={"note": note})
    assert r.status_code == 200, r.text
    return r.json()


def reopen(hub, creds, task_id, note="not ready yet"):
    return api(hub, creds, "POST", f"/tasks/{task_id}/update", json={"state": "in_progress", "note": note})


def post(hub, creds, task_id, body, to=None):
    r = api(hub, creds, "POST", f"/chats/task/{task_id}/messages", json={"body": body, "to": to})
    assert r.status_code == 201, r.text
    return r.json()


def deliveries(hub, creds):
    r = api(hub, creds, "GET", "/chats/deliveries")
    assert r.status_code == 200, r.text
    return r.json()["deliveries"]


def claim(hub, creds, delivery_id, mode="woke"):
    return api(hub, creds, "POST", f"/chats/deliveries/{delivery_id}", json={"mode": mode})


def status_lines(hub, task_id, state):
    return [m["meta"] for m in chats.messages(hub.conn(), "task", str(task_id))
            if m["kind"] == "status" and m["author"] == chats.SYSTEM and m["meta"].get("state") == state]


def later(minutes):
    return datetime.now(UTC) + timedelta(minutes=minutes)


@pytest.fixture
def team(hub):
    """rev reviews the project (and, as on the real hub, also owns its review tasks as their implementer); b1 and
    b2 build."""
    rev, b1, b2 = hub.enroll("rev"), hub.enroll("b1"), hub.enroll("b2")
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "proj", actor=OPERATOR)
        reviewers.set_reviewers(conn, "proj", [rev.id], actor=OPERATOR)
    return hub, rev, b1, b2


# --- the #39 case, replayed end to end ---------------------------------------------------------------------------


def test_the_39_stall_no_longer_blocks(team):
    hub, rev, b1, _ = team
    review_task = running(hub, [rev], title="Review: device broadcast + device stats")  # #39
    build_task = running(hub, [b1], title="Message all the agents on a device")  # #41

    # 1. The reviewer's session finishes before any branch is ready: #39 goes to review with nobody to review it.
    # (c) It still finishes, but the hub warns: the project's reviewer recorded no verdict since #39 started.
    t39 = finish(hub, rev, review_task["id"], note="nothing to review yet")
    assert t39["state"] == "review" and not views.task_reviewers(t39)
    assert t39["warning"] == "no_verdict"
    assert status_lines(hub, review_task["id"], "no_verdict") == [
        {"state": "no_verdict", "warning": "no_verdict", "task_id": review_task["id"], "agent_id": rev.id,
         "agent_name": "rev"}]
    # (d) Left like that, it shows as a stalled review once stall_minutes pass.
    assert review_task["id"] not in stalls.stalled_reviews(hub.conn())
    flag = stalls.stalled_reviews(hub.conn(), now=later(31))[review_task["id"]]
    assert flag["reason"] == "no_reviewer" and flag["minutes"] >= 30

    # 2. The builder finishes: its task goes to review, gets the project's reviewer, and (b) that reviewer is woken.
    t41 = finish(hub, b1, build_task["id"], note="branch agent/b1-broadcast @ abc123")
    assert t41["state"] == "review" and [m["agent_id"] for m in views.task_reviewers(t41)] == [rev.id]
    handoff = [d for d in deliveries(hub, rev) if d["chat"]["kind"] == "task"
               and d["chat"]["ref"] == str(build_task["id"])]
    assert len(handoff) == 1 and handoff[0]["wake"] is True
    assert "agent/b1-broadcast @ abc123" in handoff[0]["body"]
    assert claim(hub, rev, handoff[0]["id"]).status_code == 200  # the runner resumes rev's session on the task

    # 3. The builder's later hand-off is addressed to nobody, as on the real hub; it still wakes the reviewer.
    msg = post(hub, b1, build_task["id"], "Rebased: agent/b1-broadcast @ def456, ready for review")
    assert [(w["agent_id"], w["wake"]) for w in msg["wakes"]] == [(rev.id, True)]
    later_d = next(d for d in deliveries(hub, rev) if d["message_id"] == msg["id"])
    assert claim(hub, rev, later_d["id"]).status_code == 200

    # 4. (a) The reviewer reopens its own review task, without the operator.
    r = reopen(hub, rev, review_task["id"], note="the branches are ready now")
    assert r.status_code == 200, r.text
    t39 = r.json()
    assert t39["state"] == "in_progress" and t39["last_moved_by"] == rev.id and t39["reopened"] is True
    assert t39["last_note"] == "the branches are ready now"
    assert task_agents.member(t39, rev.id)["finished_at"] is None  # it has to finish again: no check is skipped
    assert status_lines(hub, review_task["id"], "reopened") == [
        {"state": "reopened", "task_id": review_task["id"], "by": rev.id, "agent_id": rev.id,
         "agent_name": "rev", "detail": "the branches are ready now"}]
    assert review_task["id"] not in stalls.stalled_reviews(hub.conn(), now=later(120))
    page = hub.client.get(f"/ui/tasks/{review_task['id']}", headers=hub.op_headers()).text
    assert f"rev reopened task #{review_task['id']}: back in progress" in page
    assert "the branches are ready now" in page
    assert "rev finished task #%d without recording a review verdict" % review_task["id"] in page

    # 5. rev reviews #41 for real, then finishes #39 again: no warning now.
    r = api(hub, rev, "POST", f"/tasks/{build_task['id']}/verdict", json={"verdict": "approve", "notes": "ok"})
    assert r.status_code == 200, r.text
    t39 = finish(hub, rev, review_task["id"], note="reviewed #41: approve")
    assert t39["state"] == "review" and "warning" not in t39


def test_the_39_stall_with_an_operator_reopen(team):
    """(c) Before (a), hub-reviewer asked for task_reassign as a stand-in; task_reopen now reopens the task itself."""
    hub, rev, _, _ = team
    t = running(hub, [rev])
    finish(hub, rev, t["id"])
    r = api(hub, rev, "POST", "/approvals", json={"kind": "task_reopen", "reason": "finished before the branches",
                                                  "details": {"task_id": t["id"]}})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["kind"] == "task_reopen" and a["details"] == {"task_id": t["id"]} and a["project"] == "proj"
    card = hub.client.get(f"/ui/approvals/{a['id']}", headers=hub.op_headers())
    assert card.status_code == 200 and f"Reopen task #{t['id']}: review → in progress" in card.text
    tx(hub, approvals.decide, a["id"], approve=True, actor=OPERATOR)
    got = tasks.get_task(hub.conn(), t["id"])
    assert got["state"] == "in_progress" and got["last_moved_by"] == OPERATOR
    assert task_agents.member(got, rev.id)["finished_at"] is None
    # The note is the hub's, never the agent's reason: a resumed session reads it as the operator's.
    assert got["last_note"] == f"reopened by the operator (approval #{a['id']})"
    assert status_lines(hub, t["id"], "reopened")[0]["by"] == OPERATOR


# --- (a) reopen ------------------------------------------------------------------------------------------------------


def test_an_implementer_reopens_its_task(team):
    hub, rev, b1, b2 = team
    t = running(hub, [b1, b2])
    finish(hub, b1, t["id"])
    finish(hub, b2, t["id"])
    r = reopen(hub, b2, t["id"], note="forgot the README")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["state"] == "in_progress"
    # A new round: every implementer is unfinished, the reviewer's verdict (none yet) is cleared.
    assert all(m["finished_at"] is None for m in got["agents"] if m["stage"] == "works")
    moved = [json.loads(e["payload"]) for e in hub.conn().execute(
        "SELECT payload FROM events WHERE type = 'task.transitioned' AND actor = ?", (b2.id,))]
    assert moved[-1]["from"] == "review" and moved[-1]["to"] == "in_progress"
    # Back to review only by finishing again, every implementer.
    assert finish(hub, b1, t["id"])["state"] == "in_progress"
    assert finish(hub, b2, t["id"])["state"] == "review"


def test_a_reviewer_reopens_and_clears_the_round(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    assert api(hub, rev, "POST", f"/tasks/{t['id']}/verdict",
               json={"verdict": "approve", "notes": "ok"}).status_code == 200
    r = reopen(hub, rev, t["id"], note="found one more thing")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["state"] == "in_progress"
    assert task_agents.member(got, rev.id)["verdict"] is None
    assert task_agents.member(got, b1.id)["finished_at"] is None


def test_only_agents_on_the_task_reopen_it(team):
    hub, rev, b1, b2 = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    assert reopen(hub, b2, t["id"]).status_code in (403, 404)  # b2 is in the project, not on the task
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "review"


def test_reopen_needs_an_implementer_to_reopen_it_for(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    hub.set_status(b1, "revoked")
    r = reopen(hub, rev, t["id"])
    assert r.status_code == 409 and "implementer" in r.json()["detail"]
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "review"


def test_an_agent_still_cannot_mark_done_or_skip_review(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    r = api(hub, b1, "POST", f"/tasks/{t['id']}/update", json={"state": "review"})
    assert r.status_code == 403
    finish(hub, b1, t["id"])
    for creds in (b1, rev):
        assert api(hub, creds, "POST", f"/tasks/{t['id']}/update", json={"state": "done"}).status_code == 403
    with pytest.raises(store.Forbidden):
        tasks.check_transition("review", "done", "agent")


def test_an_operator_send_back_writes_a_reopened_line_but_a_changes_verdict_does_not(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    tx(hub, tasks.transition, t["id"], "in_progress", actor=OPERATOR, role="operator", note="one more")
    assert status_lines(hub, t["id"], "reopened") == [{"state": "reopened", "task_id": t["id"], "by": OPERATOR}]
    finish(hub, b1, t["id"])
    api(hub, rev, "POST", f"/tasks/{t['id']}/verdict", json={"verdict": "changes", "notes": "x"})
    assert len(status_lines(hub, t["id"], "reopened")) == 1  # the verdict's own messages say it


# --- (b) hand-offs wake the other members ---------------------------------------------------------------------------


def test_an_unaddressed_message_in_review_wakes_every_other_member(team):
    hub, rev, b1, b2 = team
    t = running(hub, [b1, b2])
    finish(hub, b1, t["id"])
    finish(hub, b2, t["id"])
    msg = post(hub, b1, t["id"], "ready: agent/b1 @ 1234")
    assert {(w["agent_id"], w["wake"]) for w in msg["wakes"]} == {(rev.id, True), (b2.id, True)}
    assert sorted(msg["handoff"]) == sorted([rev.id, b2.id])  # never its author
    for creds in (rev, b2):
        d = next(d for d in deliveries(hub, creds) if d["message_id"] == msg["id"])
        assert claim(hub, creds, d["id"]).status_code == 200


def test_a_reviewers_unaddressed_message_wakes_the_implementers_too(team):
    """#39's shape: the assignee finished early; another member's hand-off in the review task's chat wakes it."""
    hub, rev, b1, _ = team
    t = running(hub, [b1], [rev])
    finish(hub, b1, t["id"])
    msg = post(hub, rev, t["id"], "the branch you wait for is ready, reopen")
    assert [(w["agent_id"], w["wake"]) for w in msg["wakes"]] == [(b1.id, True)]


def test_outside_review_an_unaddressed_message_still_wakes_nobody(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1], [rev])
    msg = post(hub, b1, t["id"], "still working")  # in progress: a notice, as before
    assert [w["wake"] for w in msg["wakes"]] == [False] and msg["handoff"] == []
    msg = post(hub, rev, t["id"], "fyi")
    assert [w["wake"] for w in msg["wakes"]] == [False]


def test_agents_off_the_task_still_cannot_write_in_its_chat(team):
    hub, rev, b1, b2 = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    r = api(hub, b2, "POST", f"/chats/task/{t['id']}/messages", json={"body": "#42 is ready"})
    assert r.status_code in (403, 404)


def test_handoff_wakes_among_three_members_are_capped_by_the_loop_limit(tmp_path):
    hub = make_hub(tmp_path, wakes={"loop_limit": 4})
    try:
        b1, r1, r2 = hub.enroll("b1"), hub.enroll("r1"), hub.enroll("r2")
        t = running(hub, [b1], [r1, r2])
        finish(hub, b1, t["id"])  # wakes r1 and r2, one chain each (b1 is in no chain yet)
        first = post(hub, b1, t["id"], "ready @ abc")  # a new chain: two wakes
        assert sorted(first["handoff"]) == sorted([r1.id, r2.id])
        d = next(x for x in deliveries(hub, r1) if x["message_id"] == first["id"])
        assert claim(hub, r1, d["id"]).status_code == 200
        reply = post(hub, r1, t["id"], "which tests?")  # r1 answers in that chain: b1 and r2, four
        assert reply["chain_id"] == first["chain_id"] and all(w["wake"] for w in reply["wakes"])
        d = next(x for x in deliveries(hub, b1) if x["message_id"] == reply["id"])
        assert claim(hub, b1, d["id"]).status_code == 200
        tripped = post(hub, b1, t["id"], "tests/test_x.py")  # six: over the limit, the chain pauses
        assert tripped["chain_id"] == first["chain_id"] and tripped.get("loop_paused") is True
        assert not any(w["wake"] for w in tripped["wakes"])
        assert wakes.chain_view(hub.conn(), tripped["chain_id"])["status"] == "paused"
    finally:
        wakes.configure(HubConfig().wake_loop_limit)


def test_an_addressed_message_is_unchanged(team):
    hub, rev, b1, b2 = team
    t = running(hub, [b1, b2])
    finish(hub, b1, t["id"])
    finish(hub, b2, t["id"])
    msg = post(hub, b1, t["id"], "can you look at the tests?", to="b2")
    assert [(w["agent_id"], w["wake"]) for w in msg["wakes"]] == [(b2.id, True)]


def test_the_last_finish_wakes_each_reviewer_and_an_earlier_one_does_not(team):
    hub, rev, b1, b2 = team
    other = hub.enroll("rev2")
    t = running(hub, [b1, b2], [rev, other])
    finish(hub, b1, t["id"], note="part one")
    assert not [m for m in chats.messages(hub.conn(), "task", str(t["id"])) if m["kind"] == "text"]
    finish(hub, b2, t["id"], note="part two, agent/b2 @ 99")
    sent = [m for m in chats.messages(hub.conn(), "task", str(t["id"])) if m["kind"] == "text"]
    assert [(m["author"], m["meta"]["to"]) for m in sent] == [(b2.id, rev.id), (b2.id, other.id)]
    for m in sent:
        assert m["meta"]["handoff"] is True
        assert f"Task #{t['id']} is ready for review" in m["body"] and "part two, agent/b2 @ 99" in m["body"]
    for creds in (rev, other):
        d = next(d for d in deliveries(hub, creds) if d["chat"]["ref"] == str(t["id"]))
        assert d["wake"] is True


def test_a_finish_with_no_reviewer_posts_nothing(hub):
    b1 = hub.enroll("b1")
    t = running(hub, [b1])
    assert finish(hub, b1, t["id"])["state"] == "review"
    assert not [m for m in chats.messages(hub.conn(), "task", str(t["id"])) if m["kind"] == "text"]


def test_handoff_wakes_respect_the_pause_switches(team):
    hub, rev, b1, _ = team
    tx(hub, wakes.set_agent_paused, rev.id, True, actor=OPERATOR)
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    msg = post(hub, b1, t["id"], "ready")
    assert [(w["agent_id"], w["wake"]) for w in msg["wakes"]] == [(rev.id, False)]
    assert all(d["wake"] is False for d in deliveries(hub, rev))  # it still receives them, without waking
    tx(hub, wakes.set_agent_paused, rev.id, False, actor=OPERATOR)
    assert all(d["wake"] is True for d in deliveries(hub, rev))  # lifting the pause lets them wake
    tx(hub, wakes.set_hub_paused, True, actor=OPERATOR)
    msg = post(hub, b1, t["id"], "ready, again")
    assert [w["wake"] for w in msg["wakes"]] == [False]


def test_handoff_wakes_count_toward_loop_protection(tmp_path):
    hub = make_hub(tmp_path, wakes={"loop_limit": 2})
    try:
        rev, b1 = hub.enroll("rev"), hub.enroll("b1")
        t = running(hub, [b1], [rev])
        finish(hub, b1, t["id"])  # the hand-off starts a chain: one wake
        d = deliveries(hub, rev)[0]
        assert d["wake"] is True and claim(hub, rev, d["id"]).status_code == 200
        question = post(hub, rev, t["id"], "which commit?", to="b1")  # rev answers in that chain: two
        assert question["chain_id"] == d["chain_id"] and question["wakes"][0]["wake"] is True
        d = next(x for x in deliveries(hub, b1) if x["message_id"] == question["id"])
        assert claim(hub, b1, d["id"]).status_code == 200
        tripped = post(hub, b1, t["id"], "abc123")  # b1's unaddressed hand-off: three, over the limit
        assert tripped["chain_id"] == question["chain_id"] and tripped["handoff"] == [rev.id]
        assert tripped.get("loop_paused") is True and tripped["wakes"][0]["wake"] is False
        assert wakes.chain_view(hub.conn(), tripped["chain_id"])["status"] == "paused"
    finally:
        wakes.configure(HubConfig().wake_loop_limit)


# --- (c) the task_reopen approval ------------------------------------------------------------------------------------


def test_task_reopen_needs_a_task_in_review_and_access(team):
    hub, rev, b1, _ = team
    outsider = hub.enroll("out", projects=("other",))
    t = running(hub, [b1])
    r = api(hub, b1, "POST", "/approvals", json={"kind": "task_reopen", "reason": "x", "details": {"task_id": t["id"]}})
    assert r.status_code == 409  # it is in progress
    finish(hub, b1, t["id"])
    r = api(hub, outsider, "POST", "/approvals",
            json={"kind": "task_reopen", "reason": "x", "details": {"task_id": t["id"]}})
    assert r.status_code in (403, 404)
    r = api(hub, b1, "POST", "/approvals", json={"kind": "task_reopen", "reason": "x", "details": {"task_id": "1"}})
    assert r.status_code == 422


def test_a_rejected_or_late_task_reopen_changes_nothing(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    ids = [api(hub, b1, "POST", "/approvals", json={"kind": "task_reopen", "reason": "x",
                                                    "details": {"task_id": t["id"]}}).json()["id"] for _ in range(2)]
    tx(hub, approvals.decide, ids[0], approve=False, actor=OPERATOR)
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "review"
    tx(hub, tasks.transition, t["id"], "done", actor=OPERATOR, role="operator")
    tx(hub, approvals.decide, ids[1], approve=True, actor=OPERATOR)  # done meanwhile: nothing to reopen
    assert tasks.get_task(hub.conn(), t["id"])["state"] == "done"


def test_migration_allows_task_reopen_and_keeps_every_approval(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    newest = list(db.MIGRATIONS)
    at = next(i for i, m in enumerate(newest) if "approvals_v29" in m)
    monkeypatch.setattr(db, "MIGRATIONS", newest[:at])
    conn = db.init_db(path)
    with db.transaction(conn):
        conn.execute("INSERT INTO approvals (kind, requester, details, status, created_at) "
                     "VALUES ('wake_loop', 'system:hub', '{}', 'pending', 'x')")
        with pytest.raises(Exception, match="CHECK"):
            conn.execute("INSERT INTO approvals (kind, requester, details, status, created_at) "
                         "VALUES ('task_reopen', 'a', '{}', 'pending', 'x')")
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS", newest)
    conn = db.init_db(path)
    assert [r["kind"] for r in conn.execute("SELECT kind FROM approvals")] == ["wake_loop"]
    with db.transaction(conn):
        conn.execute("INSERT INTO approvals (kind, requester, details, status, created_at) "
                     "VALUES ('task_reopen', 'a', '{}', 'pending', 'x')")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'approvals'")}
    assert {"approvals_status", "approvals_command_key"} <= names


def test_task_reopen_card_and_notification_label():
    from agenthub.hub import notify

    assert notify.KIND_LABELS["task_reopen"] == "Reopen task"
    assert "task_reopen" in approvals.KINDS and "task_reopen" in approvals.AGENT_REQUESTABLE


# --- (d) the stalled-review signal -----------------------------------------------------------------------------------


def test_a_review_with_an_idle_reviewer_stalls_and_a_verdict_or_session_clears_it(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    flag = stalls.stalled_reviews(hub.conn(), now=later(45))[t["id"]]
    assert flag["reason"] == "idle" and 44 <= flag["minutes"] <= 46
    entered = hub.conn().execute("SELECT MAX(ts) FROM events WHERE type = 'task.transitioned'").fetchone()[0]
    assert flag["since"] == entered
    # A reviewer session running: not stalled.
    r = api(hub, rev, "POST", f"/tasks/{t['id']}/runs")
    assert r.status_code == 201, r.text
    assert t["id"] not in stalls.stalled_reviews(hub.conn(), now=later(45))
    api(hub, rev, "POST", f"/tasks/{t['id']}/runs/{r.json()['id']}/end",
        json={"outcome": "success", "session_id": None, "num_turns": 1, "cost_usd": 0})
    assert t["id"] in stalls.stalled_reviews(hub.conn(), now=later(45))  # it ended without a verdict
    # A verdict: not stalled (approve leaves the task with the operator, which is not a stall).
    api(hub, rev, "POST", f"/tasks/{t['id']}/verdict", json={"verdict": "approve", "notes": ""})
    assert t["id"] not in stalls.stalled_reviews(hub.conn(), now=later(45))


def test_a_reviewer_awake_in_the_task_chat_is_not_a_stall(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    r = api(hub, rev, "POST", f"/chats/task/{t['id']}/stream",
            json={"items": [{"type": "status", "state": "working"}], "task_id": t["id"]})
    assert r.status_code in (200, 201), r.text
    assert t["id"] not in stalls.stalled_reviews(hub.conn(), now=later(45))


def test_stall_minutes_config(tmp_path):
    assert HubConfig().review_stall_minutes == 30
    check_reviews({"stall_minutes": 0})
    for bad in ({"stall_minutes": -1}, {"stall_minutes": True}, {"stall_minutes": "30"}, {"other": 1},
                {"stall_minutes": 7 * 24 * 60 + 1}, []):
        with pytest.raises(ValueError):
            check_reviews(bad)
    hub = make_hub(tmp_path, reviews={"stall_minutes": 5})
    try:
        b1 = hub.enroll("b1")
        t = running(hub, [b1])
        finish(hub, b1, t["id"])
        assert t["id"] in stalls.stalled_reviews(hub.conn(), now=later(6))
        stalls.configure(0)  # off
        assert stalls.stalled_reviews(hub.conn(), now=later(600)) == {}
    finally:
        stalls.configure(HubConfig().review_stall_minutes)


def test_views_carry_the_flag(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    other = running(hub, [b1])
    finish(hub, b1, t["id"])
    conn = hub.conn()
    with db.transaction(conn):  # make it look 40 minutes old
        old = (datetime.now(UTC) - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old, t["id"]))
    conn.execute("DROP TRIGGER events_no_update")  # this test's own copy of the database
    conn.execute("UPDATE events SET ts = ? WHERE type = 'task.transitioned' AND json_extract(payload, '$.to') = "
                 "'review'", (old,))
    conn.commit()
    conn = hub.conn()
    row = next(x for a in views.agent_rows(conn) if a["id"] == b1.id for x in a["tasks"] if x["id"] == t["id"])
    assert row["stalled_review"]["minutes"] >= 39 and row["stalled_review"]["reason"] == "idle"
    assert next(x for a in views.agent_rows(conn) if a["id"] == b1.id
                for x in a["tasks"] if x["id"] == other["id"])["stalled_review"] is None
    tree = next(p for p in views.project_tree(conn) if p["name"] == "proj")
    assert next(x for x in tree["tasks"] if x["id"] == t["id"])["stalled_review"]["since"] == old
    index = next(x for x in views.chat_index(conn)["tasks"] if x["item"] == f"task:{t['id']}")
    assert index["stalled_review"] is not None
    assert views.panel(conn, f"task:{t['id']}")["t"]["stalled_review"] is not None
    resp = hub.client.get(f"/ui/tasks/{t['id']}", headers=hub.op_headers())
    assert resp.status_code == 200


def test_doctor_warns_about_stalled_reviews(tmp_path, monkeypatch):
    hub = make_hub(tmp_path)
    b1 = hub.enroll("b1")
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    conn = hub.conn()
    check = doctor.check_stalled_reviews(conn, 30, now=later(31))
    assert check.status == doctor.WARN and f"#{t['id']}" in check.detail and "30 min" in check.detail
    assert doctor.check_stalled_reviews(conn, 30).status == doctor.OK
    assert doctor.check_stalled_reviews(conn, 0, now=later(999)).status == doctor.OK
    # hub_checks runs it against the hub's own database.
    HubConfig(db_path=hub.state.config.db_path, operators=["op@example.com"],
              reviews={"stall_minutes": 1}).save(paths.hub_config_path())
    names = [c.name for c in doctor.hub_checks(lambda argv: doctor.subprocess.CompletedProcess(argv, 1, "", ""))]
    assert "stalled reviews" in names


# --- no_verdict: a reviewer finishing without a verdict -------------------------------------------------------------


def test_a_non_reviewer_finishing_gets_no_warning(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    got = finish(hub, b1, t["id"])
    assert got["state"] == "review" and "warning" not in got
    assert status_lines(hub, t["id"], "no_verdict") == []


def test_a_verdict_before_the_task_last_entered_in_progress_does_not_count(team):
    hub, rev, b1, _ = team
    other = running(hub, [b1])
    finish(hub, b1, other["id"])
    assert api(hub, rev, "POST", f"/tasks/{other['id']}/verdict", json={"verdict": "approve"}).status_code == 200
    t = running(hub, [rev])  # it starts after that verdict
    assert finish(hub, rev, t["id"])["warning"] == "no_verdict"


def test_a_reviewers_task_session_ending_without_a_verdict_warns(team):
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])  # rev is added as the project's reviewer
    run = api(hub, rev, "POST", f"/tasks/{t['id']}/runs").json()
    end = {"outcome": "success", "session_id": "s1", "num_turns": 3, "cost_usd": 0}
    r = api(hub, rev, "POST", f"/tasks/{t['id']}/runs/{run['id']}/end", json=end)
    assert r.status_code == 200 and r.json()["warning"] == "no_verdict"
    assert [m["agent_id"] for m in status_lines(hub, t["id"], "no_verdict")] == [rev.id]
    # With a verdict recorded, its next session ends quietly.
    assert api(hub, rev, "POST", f"/tasks/{t['id']}/verdict", json={"verdict": "approve"}).status_code == 200
    run = api(hub, rev, "POST", f"/tasks/{t['id']}/runs").json()
    assert "warning" not in api(hub, rev, "POST", f"/tasks/{t['id']}/runs/{run['id']}/end", json=end).json()


def test_mcp_texts_say_what_happened():
    from agenthub import mcp_server

    line = mcp_server._hub_status({"state": "no_verdict", "warning": "no_verdict", "task_id": 39,
                                   "agent_id": "ag1", "agent_name": "rev"}, {})
    assert line == "warning no_verdict: rev finished task #39 without recording a review_task verdict"


async def test_mcp_finish_warns_and_a_reopen_says_the_sessions_resume(dev_hub):
    import httpx
    from mcp import Client

    from agenthub.client import HubClient
    from agenthub.mcp_server import build_server

    rev = dev_hub.enroll("rev")
    tx(dev_hub, store.ensure_project, "proj", actor=OPERATOR)
    tx(dev_hub, reviewers.set_reviewers, "proj", [rev.id], actor=OPERATOR)
    t = running(dev_hub, [rev])
    client = HubClient("http://hub", rev.token, transport=httpx.ASGITransport(app=dev_hub.client.app))
    async with Client(build_server(lambda: client)) as c:
        done = "\n".join(x.text for x in (await c.call_tool("finish_task", {"task_id": t["id"]})).content)
        assert "moves to review" in done and "Warning no_verdict" in done
        back = "\n".join(x.text for x in (await c.call_tool(
            "update_task", {"task_id": t["id"], "state": "in_progress", "note": "the branches are in"})).content)
        assert f"Task #{t['id']} is reopened" in back and "task sessions resume" in back
        blocked = "\n".join(x.text for x in (await c.call_tool(
            "update_task", {"task_id": t["id"], "state": "blocked"})).content)
        assert blocked == f"Task #{t['id']} is now blocked."
