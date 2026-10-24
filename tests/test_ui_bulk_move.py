"""The Tasks page's bulk move (0.3.12, task #55): a Stalled filter, a tick box on each row, and Mark done / Cancel
for every ticked task in one plain form post, each through the same tasks.transition as the single Move form."""

import html
import re
from urllib.parse import parse_qs, urlparse

from agenthub.hub import db, tasks

from .conftest import OP_LOGIN, OPERATOR
from .test_stalled_reviews import finish, running, team  # noqa: F401  (team is a fixture)
from .test_ui_stalled import get, stall


def move(hub, state, ids=(), headers=None, **form):
    data = {"csrf": hub.csrf(), "state": state, "id": [str(i) for i in ids], **form}
    return hub.client.post("/ui/tasks/move", headers=headers or hub.op_headers(), data=data, follow_redirects=False)


def state_of(hub, task_id):
    return tasks.get_task(hub.conn(), task_id)["state"]


def in_review(hub, b1, title="t"):
    t = running(hub, [b1], title=title)
    finish(hub, b1, t["id"])
    return t["id"]


def listed(page):
    return [int(i) for i in re.findall(r'name="id" value="(\d+)"', page)]


def result_line(page):
    """The bulk-result line as the operator reads it: tags gone, entities decoded."""
    line = re.search(r'<p class="bulk-result" role="status">(.*?)</p>', page, re.S).group(1)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", line)))


def test_mark_done_moves_every_ticked_task_and_archives_it(team):  # noqa: F811
    hub, rev, b1, _ = team
    a, b, left = in_review(hub, b1, "a"), in_review(hub, b1, "b"), in_review(hub, b1, "left alone")
    r = move(hub, "done", [a, b], note="shipped in 0.3.11")
    assert r.status_code == 303
    assert state_of(hub, a) == state_of(hub, b) == "done" and state_of(hub, left) == "review"
    conn = hub.conn()
    for task_id in (a, b):
        t = tasks.get_task(conn, task_id)
        assert t["archived_at"] and t["last_note"] == "shipped in 0.3.11" and t["last_moved_by"] == f"human:{OP_LOGIN}"
    moves = conn.execute("SELECT json_extract(payload, '$.task_id') FROM events WHERE type = 'task.transitioned' "
                         "AND json_extract(payload, '$.to') = 'done'").fetchall()
    assert sorted(m[0] for m in moves) == [a, b]  # one task.transitioned each, as from the single Move form
    page = get(hub, r.headers["location"])
    assert result_line(page) == f"2 marked done (#{a}, #{b}), 0 skipped."
    assert left in listed(page) and a not in listed(page)  # done tasks are archived: off the list


def test_a_task_whose_state_does_not_allow_it_is_skipped_not_an_error(team):  # noqa: F811
    hub, rev, b1, _ = team
    review, working = in_review(hub, b1), running(hub, [b1])["id"]
    conn = hub.conn()
    with db.transaction(conn):
        gone = tasks.create_task(conn, actor=OPERATOR, project="proj", title="already cancelled")["id"]
        tasks.transition(conn, gone, "cancelled", actor=OPERATOR, role="operator")
    r = move(hub, "done", [review, working, gone, 99999])
    assert r.status_code == 303
    assert state_of(hub, review) == "done"
    assert state_of(hub, working) == "in_progress" and state_of(hub, gone) == "cancelled"
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["moved"] == [str(review)] and query["to"] == ["done"]
    assert result_line(get(hub, r.headers["location"])) == (
        f"1 marked done (#{review}), 3 skipped (#{working}: in progress, which can't be marked done; "
        f"#{gone}: cancelled, which can't be marked done; #99999: no such task).")


def test_cancel_moves_any_open_task_and_skips_finished_ones(team):  # noqa: F811
    hub, rev, b1, _ = team
    review, working = in_review(hub, b1), running(hub, [b1])["id"]
    assert move(hub, "done", [review]).status_code == 303
    r = move(hub, "cancelled", [review, working])
    assert state_of(hub, working) == "cancelled" and state_of(hub, review) == "done"
    assert result_line(get(hub, r.headers["location"])) == (
        f"1 cancelled (#{working}), 1 skipped (#{review}: done, which can't be cancelled).")


def test_all_shown_moves_the_listed_tasks_without_javascript(team):  # noqa: F811
    hub, rev, b1, _ = team
    a, b = in_review(hub, b1), in_review(hub, b1)
    page = get(hub, "/ui/tasks?state=review")
    shown = re.search(r'name="shown" value="([\d,]*)"', page).group(1)
    assert sorted(int(i) for i in shown.split(",")) == sorted([a, b])
    r = move(hub, "cancelled", ids=[a], all="1", shown=shown, filter_state="review")  # "all" wins over the ticks
    assert state_of(hub, a) == state_of(hub, b) == "cancelled"
    assert parse_qs(urlparse(r.headers["location"]).query)["state"] == ["review"]  # back to the same filter


def test_only_done_or_cancelled_and_at_least_one_task(team):  # noqa: F811
    hub, rev, b1, _ = team
    t = in_review(hub, b1)
    assert move(hub, "in_progress", [t]).status_code == 422 and state_of(hub, t) == "review"
    assert move(hub, "done", []).status_code == 422


def test_the_bulk_move_needs_csrf(team):  # noqa: F811
    hub, rev, b1, _ = team
    t = in_review(hub, b1)
    data = {"state": "done", "id": [str(t)]}
    assert hub.client.post("/ui/tasks/move", headers=hub.op_headers(), data=data,
                           follow_redirects=False).status_code == 403
    assert hub.client.post("/ui/tasks/move", headers=hub.op_headers(), data={**data, "csrf": "0" * 64},
                           follow_redirects=False).status_code == 403
    cross = {**hub.op_headers(), "sec-fetch-site": "cross-site"}
    assert move(hub, "done", [t], headers=cross).status_code == 403
    assert state_of(hub, t) == "review"


def test_only_the_operator_can_bulk_move(team):  # noqa: F811
    hub, rev, b1, _ = team
    t = in_review(hub, b1)
    for creds in (rev, b1):
        r = move(hub, "done", [t], headers={**creds.headers, "x-csrf-token": hub.csrf()})
        assert r.status_code == 403
    assert state_of(hub, t) == "review"


def test_the_stalled_filter_and_its_no_reviewer_sub_filter(team):  # noqa: F811
    hub, rev, b1, _ = team
    idle, fresh = in_review(hub, b1, "idle"), in_review(hub, b1, "fresh")
    stall(hub, idle)
    orphan = hub.enroll("b9", projects=("solo",))  # a project with no reviewers
    lone = running(hub, [orphan], project="solo", title="no reviewer")["id"]
    finish(hub, orphan, lone)
    stall(hub, lone)
    assert sorted(listed(get(hub, "/ui/tasks?stalled=all"))) == sorted([idle, lone])
    assert listed(get(hub, "/ui/tasks?stalled=no_reviewer")) == [lone]
    assert fresh in listed(get(hub, "/ui/tasks?stalled=bogus"))  # an unknown value is no filter
    page = get(hub, "/ui/tasks")
    assert '<a href="/ui/tasks?stalled=all">2 stalled reviews</a>' in page
    assert '<option value="no_reviewer" selected>' in get(hub, "/ui/tasks?stalled=no_reviewer")
    r = move(hub, "done", [idle, lone], stalled="no_reviewer")
    assert state_of(hub, idle) == state_of(hub, lone) == "done"
    assert parse_qs(urlparse(r.headers["location"]).query)["stalled"] == ["no_reviewer"]
    assert "No tasks match" in get(hub, r.headers["location"])


def test_every_row_has_a_tick_box_kept_across_the_live_refresh(team):  # noqa: F811
    hub, rev, b1, _ = team
    t = in_review(hub, b1)
    page = get(hub, "/ui/tasks")
    box = re.search(rf'<input type="checkbox" form="bulk-move" name="id" value="{t}"[^>]*>', page).group(0)
    assert f'id="pick-{t}"' in box and "hx-preserve" in box and "data-pick" in box
    form = re.search(r'<form id="bulk-move".*?</form>', page, re.S).group(0)
    assert 'action="/ui/tasks/move"' in form and f'value="{hub.csrf()}"' in form and "data-pick-all" in form
    assert 'value="done"' in form and 'value="cancelled"' in form
    live = hub.client.get("/ui/tasks", headers=hub.op_headers(**{"hx-request": "true", "hx-target": "live"})).text
    assert f'id="pick-{t}"' in live and 'name="shown"' in live and 'id="bulk-move"' not in live
