"""The stalled-review badge (0.3.11, after #1120): a task in review that nobody is reviewing (#51's
``stalled_review``) says so wherever the dashboard lists it, and its page says what to do."""

import re
from datetime import UTC, datetime, timedelta

from agenthub.hub import db

from .test_stalled_reviews import finish, running, team  # noqa: F401  (team is a fixture)


def get(hub, path):
    r = hub.client.get(path, headers=hub.op_headers())
    assert r.status_code == 200, r.text
    return r.text


def stall(hub, task_id, minutes=40):
    """Make the task's move to review look ``minutes`` old (this test's own copy of the database)."""
    old = (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
    conn.execute("DROP TRIGGER IF EXISTS events_no_update")
    conn.execute("UPDATE events SET ts = ? WHERE type = 'task.transitioned' AND json_extract(payload, '$.task_id') = ? "
                 "AND json_extract(payload, '$.to') = 'review'", (old, task_id))
    conn.commit()


def badges(html):
    return re.findall(r'<span class="chip chip-stalled" title="([^"]*)">([^<]*)</span>', html)


def test_a_stalled_review_is_badged_on_every_task_list(team):  # noqa: F811
    hub, rev, b1, _ = team
    stuck, fresh = running(hub, [b1], title="stuck"), running(hub, [b1], title="fresh")
    finish(hub, b1, stuck["id"])
    finish(hub, b1, fresh["id"])
    stall(hub, stuck["id"], minutes=95)
    pages = ["/ui/tasks", "/ui/projects", "/ui/projects/proj", f"/ui/agents/{b1.id}", f"/ui/tasks/{stuck['id']}",
             "/ui/chats", f"/ui/panel?item=agent:{b1.id}", f"/ui/panel?item=task:{stuck['id']}",
             "/ui/panel?item=project:proj"]
    for path in pages:
        found = badges(get(hub, path))
        assert len(found) == 1, path  # the stuck task, never the fresh one
        title, text = found[0]
        assert text == "stalled · 1h" and "no reviewer session is running" in title and "95 min" in title, path


def test_the_task_page_says_why_and_what_to_do(team):  # noqa: F811
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    assert "Stalled review" not in get(hub, f"/ui/tasks/{t['id']}")  # in review, but not for long
    stall(hub, t["id"])
    html = get(hub, f"/ui/tasks/{t['id']}")
    note = re.search(r'<p class="stall-note" role="status">(.*?)</p>', html, re.S).group(1)
    assert "none of its reviewers has a session running" in note and "move it below" in note
    assert badges(html)[0][1] == "stalled · 40m"


def test_a_review_with_no_reviewer_says_so(hub):
    b1 = hub.enroll("b1")  # no project reviewers at all
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    stall(hub, t["id"])
    html = get(hub, f"/ui/tasks/{t['id']}")
    assert badges(html)[0][1] == "stalled: no reviewer · 40m"
    assert "with no active reviewer" in html


def test_a_verdict_takes_the_badge_away(team):  # noqa: F811
    hub, rev, b1, _ = team
    t = running(hub, [b1])
    finish(hub, b1, t["id"])
    stall(hub, t["id"])
    assert badges(get(hub, "/ui/tasks"))
    r = hub.client.post(f"/api/v1/tasks/{t['id']}/verdict", headers=rev.headers,
                        json={"verdict": "approve", "notes": ""})
    assert r.status_code == 200, r.text
    assert not badges(get(hub, "/ui/tasks")) and "Stalled review" not in get(hub, f"/ui/tasks/{t['id']}")
