"""Usage page: prompt sessions reported by runners, the aggregation over a window, the page and its nav entry."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from agenthub.hub import db, prompts, store, usage

from .conftest import OPERATOR
from .test_prompts import api, create, mk_task, online


def get(hub, path, **headers):
    return hub.client.get(path, headers=hub.op_headers(**headers))


def started(hub, agent, pid):
    assert api(hub, agent, "POST", f"/{pid}/start").status_code == 200


def report(hub, agent, pid, **fields):
    body = {"outcome": "success", "seconds": 90, "num_turns": 3, "cost_usd": 0.25} | fields
    return api(hub, agent, "POST", f"/{pid}/runs", json=body)


def task_run(hub, task_id, agent_id, *, start, minutes=10, outcome="success", turns=5, cost=0.5):
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, agent_id, started_at, ended_at, outcome, num_turns, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, agent_id, db.now_iso(start),
             db.now_iso(start + timedelta(minutes=minutes)) if outcome else None, outcome, turns, cost))


def prompt_runs(hub, pid=None):
    sql, args = "SELECT * FROM prompt_runs", ()
    if pid is not None:
        sql, args = sql + " WHERE prompt_id = ?", (pid,)
    return [dict(r) for r in hub.conn().execute(sql + " ORDER BY id", args)]


# --- the endpoint -------------------------------------------------------------------------------------------------

def test_a_recipient_reports_each_prompt_session(hub):
    a = hub.enroll("alice")
    online(hub, a)
    t = mk_task(hub, a)
    pid = create(hub, f"task:{t['id']}")["id"]
    started(hub, a, pid)
    before = datetime.now(UTC)
    r = report(hub, a, pid, outcome="usage_limit", seconds=600, num_turns=1, cost_usd=0, session_id="s1")
    assert r.status_code == 201, r.text
    assert report(hub, a, pid, session_id="s1").status_code == 201
    first, second = prompt_runs(hub, pid)
    assert (first["agent_id"], first["outcome"], first["num_turns"], first["session_id"], first["project"]) == (
        a.id, "usage_limit", 1, "s1", "proj")
    # The hub's clock places the session: it ended now, `seconds` after it started.
    took = datetime.fromisoformat(first["ended_at"]) - datetime.fromisoformat(first["started_at"])
    assert took == timedelta(seconds=600) and datetime.fromisoformat(first["ended_at"]) >= before
    assert (second["outcome"], second["num_turns"], second["cost_usd"]) == ("success", 3, 0.25)


def test_a_settled_recipient_still_reports_its_session(hub):
    """A group recipient claims its share inside the session; the session's usage arrives after that."""
    a, b = hub.enroll("alice"), hub.enroll("bob")
    online(hub, a, b)
    pid = create(hub, "project:proj")["id"]
    started(hub, a, pid)
    assert api(hub, a, "POST", f"/{pid}/decline", json={"reason": "busy"}).status_code == 200
    assert report(hub, a, pid).status_code == 201


def test_only_a_recipient_that_started_may_report(hub):
    a, b, c = hub.enroll("alice"), hub.enroll("bob"), hub.enroll("carol")
    online(hub, a, b)
    pid = create(hub, f"agent:{a.id}")["id"]
    assert report(hub, c, pid).status_code == 404  # not a recipient
    assert report(hub, a, pid).status_code == 409  # never started
    assert report(hub, a, 999).status_code == 404
    assert prompt_runs(hub) == []


def test_report_is_validated(hub):
    a = hub.enroll("alice")
    online(hub, a)
    pid = create(hub, f"agent:{a.id}")["id"]
    started(hub, a, pid)
    for bad in ({"seconds": -1}, {"seconds": prompts.MAX_RUN_SECONDS + 1}, {"num_turns": -1}, {"cost_usd": -0.5},
                {"outcome": ""}, {"outcome": "x" * 65}):
        assert report(hub, a, pid, **bad).status_code == 422, bad
    assert hub.client.post(f"/api/v1/prompts/{pid}/runs", json={"outcome": "success", "seconds": 1}).status_code == 401


def test_agent_prompt_counts_in_the_reported_project_only_if_the_agent_may_see_it(hub):
    a = hub.enroll("alice", projects=("proj", "other"))
    online(hub, a)
    conn = hub.conn()
    with db.transaction(conn):
        for name in ("proj", "other", "secret"):
            store.ensure_project(conn, name, actor=OPERATOR)
    pid = create(hub, f"agent:{a.id}")["id"]
    started(hub, a, pid)
    for project in ("other", "secret", "nope", None):
        assert report(hub, a, pid, project=project).status_code == 201
    assert [r["project"] for r in prompt_runs(hub, pid)] == ["other", None, None, None]


def test_a_task_prompt_counts_in_its_own_project_whatever_the_runner_says(hub):
    a = hub.enroll("alice", projects=("proj", "other"))
    online(hub, a)
    t = mk_task(hub, a)
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "other", actor=OPERATOR)
    pid = create(hub, f"task:{t['id']}")["id"]
    started(hub, a, pid)
    assert report(hub, a, pid, project="other").status_code == 201
    assert prompt_runs(hub, pid)[0]["project"] == "proj"


def test_reports_per_recipient_are_not_capped(hub):
    # 0.3.6.1: a prompt waits out the usage limit as often as it's hit, so it may run any number of sessions
    a = hub.enroll("alice")
    online(hub, a)
    pid = create(hub, f"agent:{a.id}")["id"]
    started(hub, a, pid)
    for _ in range(25):
        assert report(hub, a, pid).status_code == 201
    assert len(prompt_runs(hub, pid)) == 25


def test_migration_adds_prompt_runs_to_an_existing_hub(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = db.connect(path)
    for version, script in enumerate(db.MIGRATIONS[:18], start=1):
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {version};COMMIT;")
    assert db.migrate(conn) == len(db.MIGRATIONS)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(prompt_runs)")}
    assert {"prompt_id", "agent_id", "project", "started_at", "ended_at", "outcome", "num_turns", "cost_usd"} <= cols


# --- the aggregation ----------------------------------------------------------------------------------------------

@pytest.fixture
def mixed(hub):
    """alice: two task runs (one a limit hit) and a prompt about her task; bob: an old task run and an agent prompt."""
    a, b = hub.enroll("alice"), hub.enroll("bob", projects=("proj", "side"))
    online(hub, a, b)
    t1 = mk_task(hub, a, title="Fix login")
    t2 = mk_task(hub, b, project="side", title="Old work")
    now = datetime.now(UTC)
    task_run(hub, t1["id"], a.id, start=now - timedelta(hours=2), minutes=30, turns=10, cost=1.0)
    task_run(hub, t1["id"], a.id, start=now - timedelta(hours=1), minutes=6, outcome="usage_limit", turns=2, cost=0.2)
    task_run(hub, t1["id"], a.id, start=now - timedelta(minutes=5), outcome=None, turns=None, cost=None)  # running
    task_run(hub, t2["id"], b.id, start=now - timedelta(days=10), minutes=60, turns=40, cost=4.0)
    p1 = create(hub, f"task:{t1['id']}")["id"]
    started(hub, a, p1)
    report(hub, a, p1, seconds=120, num_turns=3, cost_usd=0.3)
    p2 = create(hub, f"agent:{b.id}")["id"]
    started(hub, b, p2)
    report(hub, b, p2, seconds=60, num_turns=1, cost_usd=None, project="side")
    return hub, a, b, t1, t2, p1, p2


def test_totals_over_the_default_window(mixed):
    hub, a, b, t1, t2, p1, p2 = mixed
    u = usage.summary(hub.conn())
    assert u["window"] == "7d"
    task, prompt, total = u["totals"]["task"], u["totals"]["prompt"], u["totals"]["all"]
    # bob's 10-day-old run is outside the window
    assert (task["runs"], task["limit_hits"], task["running"], task["turns"]) == (3, 1, 1, 12)
    assert task["minutes"] == pytest.approx(36) and task["cost"] == pytest.approx(1.2) and task["costed"] == 2
    assert (prompt["runs"], prompt["turns"], prompt["costed"]) == (2, 4, 1)
    assert prompt["minutes"] == pytest.approx(3) and prompt["cost"] == pytest.approx(0.3)
    assert total["runs"] == 5 and total["turns"] == 16 and total["cost"] == pytest.approx(1.5)


def test_breakdowns_by_agent_project_and_task(mixed):
    hub, a, b, t1, t2, p1, p2 = mixed
    u = usage.summary(hub.conn())
    assert [g["label"] for g in u["by_agent"]] == ["alice", "bob"]  # heaviest first
    alice, bob = u["by_agent"]
    assert (alice["task"]["turns"], alice["prompt"]["turns"]) == (12, 3)
    assert (bob["task"]["runs"], bob["prompt"]["runs"]) == (0, 1)
    assert {g["project"]: (g["task"]["runs"], g["prompt"]["runs"]) for g in u["by_project"]} == {
        "proj": (3, 1), "side": (0, 1)}
    # The prompt about task 1 counts on task 1's line, as prompt usage; bob's agent prompt is on no task.
    (line,) = u["by_task"]
    assert (line["task_id"], line["title"], line["project"]) == (t1["id"], "Fix login", "proj")
    assert (line["task"]["runs"], line["prompt"]["runs"], line["prompt"]["turns"]) == (3, 1, 3)


def test_windows(mixed):
    hub = mixed[0]
    conn = hub.conn()
    assert usage.summary(conn, "24h")["totals"]["all"]["runs"] == 5
    assert usage.summary(conn, "30d")["totals"]["all"]["runs"] == 6
    assert usage.summary(conn, "all")["totals"]["task"]["turns"] == 52
    assert usage.summary(conn, "bogus")["window"] == "7d"
    later = datetime.now(UTC) + timedelta(days=2)
    assert usage.summary(conn, "24h", now=later)["totals"]["all"]["runs"] == 0


def test_recent_runs_newest_first_with_limit_hits(mixed):
    hub, a, b, t1, t2, p1, p2 = mixed
    recent = usage.summary(hub.conn())["recent"]
    assert [r["kind"] for r in recent][:2] == ["prompt", "prompt"]  # reported just now
    assert [r["started_at"] for r in recent] == sorted((r["started_at"] for r in recent), reverse=True)
    (hit,) = [r for r in recent if r["limit_hit"]]
    assert (hit["kind"], hit["task_id"], hit["agent"]) == ("task", t1["id"], "alice")


def test_per_day_strip(mixed):
    hub = mixed[0]
    now = datetime.now(UTC)
    days = usage.per_day(hub.conn(), 7, now=now)
    assert len(days) == 7 and days[-1]["day"] == now.date().isoformat()
    assert days[0]["day"] == (now - timedelta(days=6)).date().isoformat()
    assert sum(d["all"]["runs"] for d in days) == 5  # bob's 10-day-old run is before the strip
    assert max(d["height"]["task"] + d["height"]["prompt"] for d in days) == pytest.approx(100)
    assert all(d["height"] == {"task": 0, "prompt": 0} for d in days if not d["all"]["runs"])
    assert len(usage.summary(hub.conn(), "30d")["days"]) == 30


def test_empty_hub(hub):
    u = usage.summary(hub.conn(), "all")
    assert u["totals"]["all"]["runs"] == 0 and u["recent"] == [] and u["by_task"] == []
    assert all(d["height"] == {"task": 0, "prompt": 0} for d in u["days"])


# --- the page -----------------------------------------------------------------------------------------------------

def test_usage_is_in_the_nav_right_after_reports(hub):
    for path in ("/ui/dashboard", "/ui/usage"):
        nav = re.search(r"<nav aria-label=\"Pages\">.*?</nav>", get(hub, path).text, re.S).group(0)
        labels = re.findall(r'<a href="/ui/(\w+)"', nav)
        assert labels[-2:] == ["reports", "usage"]
    nav = re.search(r"<nav aria-label=\"Pages\">.*?</nav>", get(hub, "/ui/usage").text, re.S).group(0)
    assert re.findall(r'<a href="(/ui/\w+)" class="active"', nav) == ["/ui/usage"]


def test_usage_page_shows_tasks_and_prompts(mixed):
    hub, a, b, t1, t2, p1, p2 = mixed
    r = get(hub, "/ui/usage")
    assert r.status_code == 200 and "default-src 'self'" in r.headers["content-security-policy"]
    html = r.text
    assert "<title>Usage · agenthub</title>" in html
    assert 'aria-current="page">Last 7 days</a>' in html
    for needle in ("By agent", "By project", "By task", "Per day", "Recent runs", "Fix login",
                   f'href="/ui/prompts/{p1}"', f'href="/ui/tasks/{t1["id"]}"', "chip-usage_limit", "limit hit",
                   f'href="/ui/agents/{a.id}"', "<svg class=\"usage-strip\""):
        assert needle in html, needle
    assert "Old work" not in html  # outside the window
    assert "Old work" in get(hub, "/ui/usage?window=all").text
    assert 'aria-current="page">All time</a>' in get(hub, "/ui/usage?window=all").text
    assert get(hub, "/ui/usage?window=nonsense").status_code == 200


def test_usage_page_has_no_inline_style_or_script_and_escapes_agent_text(mixed):
    hub, a = mixed[0], mixed[1]
    t = mk_task(hub, a, title="<b>bold</b> & co")
    task_run(hub, t["id"], a.id, start=datetime.now(UTC) - timedelta(minutes=30))
    html = get(hub, "/ui/usage").text
    assert "style=" not in html and not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert "<b>bold</b>" not in html and "&lt;b&gt;bold&lt;/b&gt; &amp; co" in html


def test_usage_page_is_live_and_polls_keep_the_window(mixed):
    hub = mixed[0]
    html = get(hub, "/ui/usage?window=30d").text
    assert 'id="live"' in html and 'hx-get="/ui/usage?window=30d"' in html
    live = re.search(r'<section id="live".*?</section>', html, re.S).group(0)
    assert "<form" not in live
    poll = get(hub, "/ui/usage?window=30d", **{"hx-request": "true", "hx-target": "live"}).text
    assert poll.lstrip().startswith('<section id="live"') and "<nav" not in poll and "Last 30 days" in poll


def test_empty_usage_page(hub):
    html = get(hub, "/ui/usage").text
    assert "No runs in this window." in html and "Nothing in this window." in html


def test_usage_page_needs_the_operator(hub):
    assert hub.client.get("/ui/usage").status_code in (401, 403)
