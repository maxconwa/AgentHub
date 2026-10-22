"""Prompts: the operator wakes agents (hub model, agent API, events)."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agenthub.config import HubConfig
from agenthub.hub import bus, db, private, prompts, store, task_agents, tasks
from agenthub.hub.app import expire_prompts
from agenthub.hub.dashboard import event_summary
from agenthub.hub.notify import build_notification

from .conftest import OP_LOGIN, OPERATOR

SECRET = "SECRET-PROMPT-TEXT"
CFG = HubConfig(public_url="https://hub.example.ts.net", ntfy_url="https://ntfy.example", ntfy_topic="ops-x1")


def online(hub, *agents, connected=True, prompts=True):
    """Connect agents (as far as the hub knows); ``prompts``: their runner said it runs prompts."""
    conn = hub.conn()
    with db.transaction(conn):
        for a in agents:
            conn.execute("UPDATE agents SET connected = ?, prompt_support = ? WHERE id = ?",
                         (int(connected), int(prompts), a.id))


def same_device(hub, node, *agents):
    """Move agents onto one machine (in the database and in whois, so their tokens still verify)."""
    conn = hub.conn()
    with db.transaction(conn):
        for a in agents:
            conn.execute("UPDATE agents SET device_node_id = ?, device_name = 'box' WHERE id = ?", (node, a.id))
            hub.whois.add(a.ip, node_id=node, name="box")


def mk_task(hub, *agents, project="proj", title="t"):
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, project, actor=OPERATOR)
        t = tasks.create_task(conn, actor=OPERATOR, project=project, title=title)
        for a in agents:
            task_agents.add_agent(conn, t["id"], a.id, role="", stage="works", actor=OPERATOR)
    return t


def create(hub, target, body=f"Please look at this. {SECRET}", actor=OPERATOR):
    conn = hub.conn()
    with db.transaction(conn):
        return prompts.create(conn, target=target, body=body, actor=actor)


def api(hub, agent, method, path, **kw):
    return hub.client.request(method, f"/api/v1/prompts{path}", headers=agent.headers, **kw)


def prompt_events(hub, pid=None):
    rows = hub.conn().execute("SELECT * FROM events WHERE type LIKE 'prompt.%' ORDER BY id").fetchall()
    out = [{"type": r["type"], "payload": json.loads(r["payload"]), "actor": r["actor"], "project": r["project"],
            "id": r["id"], "ts": r["ts"], "device": r["device"]} for r in rows]
    return [e for e in out if pid is None or e["payload"]["prompt_id"] == pid]


@pytest.fixture
def trio(hub):
    a, b, c = hub.enroll("alice"), hub.enroll("bob"), hub.enroll("carol")
    return hub, a, b, c


# --- schema ----------------------------------------------------------------------------------------


def test_migration_14_creates_prompt_tables(hub):
    conn = hub.conn()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 14
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(prompts)")}
    assert cols == {"id", "target", "body", "mode", "project", "thread", "status", "created_by", "created_at",
                    "finished_at"}
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(prompt_recipients)")}
    assert cols == {"prompt_id", "agent_id", "status", "task_id", "note", "updated_at", "note_by_hub"}  # 17
    indexed = {tuple(r[2] for r in conn.execute(f"PRAGMA index_info({ix['name']})"))
               for ix in conn.execute("PRAGMA index_list(prompt_recipients)")}
    assert ("agent_id", "status") in indexed and ("status", "id") in {
        tuple(r[2] for r in conn.execute(f"PRAGMA index_info({ix['name']})"))
        for ix in conn.execute("PRAGMA index_list(prompts)")}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO prompts (target, body, mode, status, created_by, created_at) "
                     "VALUES ('agent:x', 'b', 'many', 'pending', 'h', 'x')")


def _db_at(path, version):
    """A database migrated up to ``version`` only (as a deployed hub would have it)."""
    conn = db.connect(path)
    for v, script in enumerate(db.MIGRATIONS[:version], start=1):
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {v};COMMIT;")
    return conn


def _seed_v13(conn):
    conn.execute("INSERT INTO agents (id, name, device_name, device_ip, device_node_id, token_hash, status, "
                 "created_at, connected) VALUES ('ag1', 'alice', 'box', '100.64.0.9', 'n1', 'h', 'active', 'x', 1)")
    conn.execute("INSERT INTO private_messages (agent_id, author, body, ts) "
                 "VALUES ('ag1', 'human:op@example.com', 'Prompt #1: typed by hand', '2026-01-01T00:00:00.000000Z')")


def test_migrations_from_v0_1_0_schema_13(tmp_path):
    conn = _db_at(tmp_path / "hub.sqlite3", 13)
    _seed_v13(conn)
    assert db.migrate(conn) == len(db.MIGRATIONS)
    (m,) = conn.execute("SELECT * FROM private_messages").fetchall()
    assert m["prompt_id"] is None and m["read_at"] is None  # an ordinary message stays unread mail
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_15_tags_prompt_copies_made_at_schema_14(tmp_path):
    conn = _db_at(tmp_path / "hub.sqlite3", 14)
    _seed_v13(conn)
    op = "human:op@example.com"
    for pid in (1, 12):
        conn.execute("INSERT INTO prompts (id, target, body, mode, status, created_by, created_at) "
                     "VALUES (?, 'agent:ag1', 'b', 'single', 'pending', ?, '2026-02-01T00:00:00.000000Z')", (pid, op))
        conn.execute("INSERT INTO prompt_recipients (prompt_id, agent_id, status, updated_at) "
                     "VALUES (?, 'ag1', 'pending', 'x')", (pid,))
    for body in ("Prompt #12: b", "Prompt #1: b", "Prompt #1 is odd"):
        conn.execute("INSERT INTO private_messages (agent_id, author, body, ts) "
                     "VALUES ('ag1', ?, ?, '2026-02-01T00:00:00.000001Z')", (op, body))
    conn.execute("INSERT INTO private_messages (agent_id, author, body, ts) "
                 "VALUES ('ag1', 'ag1', 'Prompt #1: b', '2026-02-01T00:00:01.000000Z')")
    assert db.migrate(conn) == len(db.MIGRATIONS)
    tags = [(r["body"], r["prompt_id"]) for r in conn.execute("SELECT * FROM private_messages ORDER BY id")]
    # the hand-typed message predates prompt #1; the agent's own message is never a prompt copy
    assert tags == [("Prompt #1: typed by hand", None), ("Prompt #12: b", 12), ("Prompt #1: b", 1),
                    ("Prompt #1 is odd", None), ("Prompt #1: b", None)]


def test_migration_17_flags_the_hubs_notes_made_at_schema_14(tmp_path):
    conn = _db_at(tmp_path / "hub.sqlite3", 14)
    _seed_v13(conn)
    conn.execute("INSERT INTO prompts (id, target, body, mode, status, created_by, created_at) "
                 "VALUES (1, 'agent:ag1', 'b', 'group', 'done', 'human:op', 'x')")
    for agent, status, note in (("ag1", "timed_out", "discussion timed out"),):
        conn.execute("INSERT INTO prompt_recipients (prompt_id, agent_id, status, note, updated_at) "
                     "VALUES (1, ?, ?, ?, 'x')", (agent, status, note))
    conn.execute("INSERT INTO agents (id, name, device_name, device_ip, device_node_id, token_hash, status, "
                 "created_at) VALUES ('ag2', 'bob', 'box', '100.64.0.8', 'n1', 'h', 'active', 'x'), "
                 "('ag3', 'carol', 'box', '100.64.0.7', 'n1', 'h', 'active', 'x')")
    conn.execute("INSERT INTO prompt_recipients (prompt_id, agent_id, status, note, updated_at) "
                 "VALUES (1, 'ag2', 'skipped', 'offline', 'x'), (1, 'ag3', 'failed', 'agent revoked', 'x')")
    assert db.migrate(conn) == len(db.MIGRATIONS)
    flags = {r["agent_id"]: r["note_by_hub"] for r in conn.execute("SELECT * FROM prompt_recipients")}
    # skipped and timed_out are only ever the hub's; a failed row's note may be the agent's, so it isn't guessed
    assert flags == {"ag1": 1, "ag2": 1, "ag3": 0}


# --- create ------------------------------------------------------------------------------------------


def test_single_prompt_to_one_live_agent(trio):
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}")
    assert (p["mode"], p["status"], p["thread"], p["project"]) == ("single", "pending", None, None)
    assert p["created_by"] == OPERATOR and SECRET in p["body"]
    assert [(r["agent_id"], r["status"]) for r in p["recipients"]] == [(a.id, "pending")]
    ev = prompt_events(hub, p["id"])
    assert [e["type"] for e in ev] == ["prompt.created"]
    assert ev[0]["payload"]["agents"] == [a.id] and ev[0]["payload"]["mode"] == "single"


def test_body_is_stripped_and_bounded(trio):
    hub, a, b, c = trio
    online(hub, a)
    with pytest.raises(store.Invalid):
        create(hub, f"agent:{a.id}", body="   \n ")
    with pytest.raises(store.Invalid):
        create(hub, f"agent:{a.id}", body="x" * 8001)
    assert create(hub, f"agent:{a.id}", body="  hi  ")["body"] == "hi"
    assert create(hub, f"agent:{a.id}", body="y" * 8000)["status"] == "pending"


def test_unknown_or_malformed_target(trio):
    hub, a, b, c = trio
    for target in ("agent:nobody", "task:999", "project:nope", "device:nope", "bogus", "task:x"):
        with pytest.raises(store.NotFound):
            create(hub, target)


def test_only_a_human_operator_creates_prompts(trio):
    hub, a, b, c = trio
    online(hub, a)
    with pytest.raises(store.Forbidden):
        create(hub, f"agent:{a.id}", actor=a.id)


def test_no_agent_route_creates_prompts(trio):
    hub, a, b, c = trio
    online(hub, a)
    for method, path in (("POST", ""), ("POST", "/"), ("PUT", ""), ("POST", "/new")):
        r = api(hub, a, method, path, json={"target": f"agent:{a.id}", "body": "do it"})
        assert r.status_code in (404, 405), (method, path, r.status_code)
    assert hub.conn().execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0


def test_group_prompt_to_a_task_opens_a_channel_thread(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b, c)
    online(hub, a, b)  # carol is offline
    p = create(hub, f"task:{t['id']}")
    assert (p["mode"], p["status"], p["project"]) == ("group", "discussing", "proj")
    assert p["thread"] == f"board:ch-proj~prompt-{p['id']}"
    statuses = {r["agent_id"]: r["status"] for r in p["recipients"]}
    assert statuses == {a.id: "pending", b.id: "pending", c.id: "skipped"}
    assert [r["name"] for r in p["recipients"] if r["status"] == "skipped"] == ["carol"]
    msgs = store.recent_messages(hub.conn(), project="proj", channel=f"prompt-{p['id']}")
    assert [(m["author"], m["body"]) for m in msgs] == [(OPERATOR, pointer(p["id"], "alice and bob"))]
    ev = prompt_events(hub, p["id"])[0]
    assert sorted(ev["payload"]["agents"]) == sorted([a.id, b.id]) and ev["payload"]["skipped"] == [c.id]
    assert ev["payload"]["mode"] == "group"


def test_group_prompt_to_a_project_targets_agents_on_its_live_tasks(trio):
    hub, a, b, c = trio
    mk_task(hub, a)
    mk_task(hub, b)
    online(hub, a, b, c)
    p = create(hub, "project:proj")
    assert p["mode"] == "group" and {r["agent_id"] for r in p["recipients"]} == {a.id, b.id}
    assert p["thread"] == f"board:ch-proj~prompt-{p['id']}" and p["project"] == "proj"


def test_group_prompt_to_a_device_uses_the_device_board(trio):
    hub, a, b, c = trio
    same_device(hub, "nbox", a, b)
    online(hub, a, b)
    p = create(hub, "device:nbox")
    assert (p["mode"], p["status"], p["project"], p["thread"]) == ("group", "discussing", None, "device:nbox")
    notes = hub.conn().execute("SELECT author, body FROM device_messages WHERE node_id = 'nbox'").fetchall()
    assert [(n["author"], n["body"]) for n in notes] == [(OPERATOR, pointer(p["id"], "alice and bob"))]


def pointer(pid, names):
    return (f"Prompt #{pid} from the operator: {names} discuss it here in their prompt sessions; the text is in "
            f"prompt_status(prompt_id={pid}). Others: no action.")


def test_group_threads_get_a_pointer_never_the_operators_text(hub):
    """Every project agent reads the channel and every agent on the machine reads the device board, so the body
    stays out of both: recipients read it in their prompt sessions; others (even busy recipients' task sessions)
    must not act on it."""
    a, b = hub.enroll("alice"), hub.enroll("bob")
    c = hub.enroll("carol")  # in the project, but not on the task: not a recipient
    same_device(hub, "nbox", a, b, c)
    t = mk_task(hub, a, b)
    online(hub, a, b, c)
    by_task = create(hub, f"task:{t['id']}", body=f"Split this. {SECRET}")
    by_device = create(hub, "device:nbox", body="z" * 7000 + SECRET)
    assert by_task["mode"] == by_device["mode"] == "group" and by_device["body"].endswith(SECRET)
    conn = hub.conn()
    posts = [r["body"] for r in conn.execute("SELECT body FROM messages")]
    posts += [r["body"] for r in conn.execute("SELECT body FROM device_messages")]
    assert posts == [pointer(by_task["id"], "alice and bob"), pointer(by_device["id"], "alice, bob and carol")]
    inbox = hub.client.get("/api/v1/inbox", headers=c.headers).json()["messages"]
    assert inbox and SECRET not in json.dumps(inbox)  # the pointers only (her own prompt's copy is never mail)
    assert api(hub, c, "GET", f"/{by_task['id']}").status_code == 404


def test_single_live_recipient_of_a_task_is_single_mode(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b)
    online(hub, a)
    p = create(hub, f"task:{t['id']}")
    assert (p["mode"], p["status"], p["project"], p["thread"]) == ("single", "pending", "proj", None)
    assert {r["agent_id"]: r["status"] for r in p["recipients"]} == {a.id: "pending", b.id: "skipped"}


def test_all_offline_fails_at_once(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b)
    p = create(hub, f"task:{t['id']}")
    assert p["status"] == "failed" and p["finished_at"]
    assert {r["status"] for r in p["recipients"]} == {"skipped"}
    assert hub.conn().execute("SELECT COUNT(*) FROM private_messages").fetchone()[0] == 0
    ev = prompt_events(hub, p["id"])[0]
    assert ev["payload"]["agents"] == [] and ev["payload"]["status"] == "failed"
    msg = build_notification(ev, {a.id: "alice", b.id: "bob"}, CFG)
    assert msg["title"].startswith(f"Prompt #{p['id']}:") and "alice" in msg["message"] + msg["title"]


def test_revoked_or_pending_agents_are_not_recipients(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b)
    online(hub, a, b)
    hub.set_status(b, "revoked")
    p = create(hub, f"task:{t['id']}")
    assert [r["agent_id"] for r in p["recipients"]] == [a.id] and p["mode"] == "single"


def test_private_chat_copy_for_each_live_recipient(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b, c)
    online(hub, a, b)
    p = create(hub, f"task:{t['id']}")
    for agent in (a, b):
        chat = private.thread(hub.conn(), agent.id)
        assert [(m["author"], m["body"]) for m in chat] == [(OPERATOR, f"Prompt #{p['id']}: {p['body']}")]
    assert private.thread(hub.conn(), c.id) == []


def test_private_copy_is_not_unread_mail_for_a_busy_agent(trio):
    """A busy agent's running task session must not get the prompt early from its inbox (the Stop hook peeks
    with addressed_only, read_inbox reads); the prompt session itself is where it acts. The chat still shows it."""
    hub, a, b, c = trio
    t = mk_task(hub, a)
    online(hub, a)
    conn = hub.conn()
    with db.transaction(conn):
        tasks.transition(conn, t["id"], "claimed", actor=OPERATOR, role="operator")
        tasks.transition(conn, t["id"], "in_progress", actor=OPERATOR, role="operator")
        tasks.start_run(conn, t["id"], store.get_agent(conn, a.id))  # busy: a task session is running
    with db.transaction(conn):
        private.send(conn, a.id, author=OPERATOR, body="a plain note")
    p = create(hub, f"agent:{a.id}")
    for query in ("?peek=true&addressed_only=true", "?peek=true", ""):
        got = hub.client.get(f"/api/v1/inbox{query}", headers=a.headers).json()["messages"]
        assert [m["body"] for m in got if m["kind"] == "private"] == ["a plain note"], query
        assert SECRET not in json.dumps(got)
    chat = private.thread(hub.conn(), a.id)
    assert [m["prompt_id"] for m in chat] == [None, p["id"]]
    html = hub.client.get(f"/ui/chats/{a.id}", headers=hub.op_headers()).text
    assert SECRET in html and f'href="/ui/prompts/{p["id"]}"' in html


def test_connected_agent_whose_runner_is_too_old_is_skipped(trio):
    """A v0.1.0 runner neither runs prompts nor polls for them: its agent is not woken, and the operator is told."""
    hub, a, b, c = trio
    t = mk_task(hub, a, b, c)
    online(hub, a, prompts=False)
    online(hub, b)
    p = create(hub, f"task:{t['id']}")
    assert (p["mode"], p["status"]) == ("single", "pending")
    assert {r["agent_id"]: (r["status"], r["note"]) for r in p["recipients"]} == {
        a.id: ("skipped", "runner too old for prompts"), b.id: ("pending", ""), c.id: ("skipped", "offline")}
    assert [m["agent_id"] for m in hub.conn().execute("SELECT agent_id FROM private_messages")] == [b.id]
    ev = prompt_events(hub, p["id"])[0]
    assert ev["payload"]["agents"] == [b.id] and sorted(ev["payload"]["skipped"]) == sorted([a.id, c.id])
    assert ev["payload"]["too_old"] == [a.id]
    assert api(hub, a, "GET", "/pending").json()["prompts"] == []
    msg = build_notification(ev, {a.id: "alice", b.id: "bob", c.id: "carol"}, CFG)
    assert msg["title"] == f"Prompt #{p['id']}: carol offline, skipped; alice runner too old for prompts, skipped"
    html = hub.client.get(f"/ui/prompts/{p['id']}", headers=hub.op_headers()).text
    assert "runner too old for prompts" in html
    online(hub, b, connected=False)
    p = create(hub, f"agent:{a.id}")
    assert p["status"] == "failed" and p["recipients"][0]["note"] == "runner too old for prompts"
    msg = build_notification(prompt_events(hub, p["id"])[0], {a.id: "alice"}, CFG)
    assert msg["title"] == f"Prompt #{p['id']}: alice runner too old for prompts, skipped; nobody could be woken"


def test_prompt_button_names_agents_whose_runner_is_too_old(trio):
    from agenthub.hub import views

    hub, a, b, c = trio
    t = mk_task(hub, a, b, c)
    online(hub, a, prompts=False)
    online(hub, b)
    state = views.prompt_state(hub.conn(), f"task:{t['id']}")
    assert state["wakes"] == ["bob"] and state["too_old"] == ["alice"] and state["reason"] is None
    html = hub.client.get(f"/ui/tasks/{t['id']}", headers=hub.op_headers()).text
    assert "Prompt wakes: task #" in html and "not alice (runner too old for prompts; upgrade it)" in html
    online(hub, b, prompts=False)
    state = views.prompt_state(hub.conn(), f"task:{t['id']}")
    assert state["wakes"] == [] and state["reason"] == (
        "Prompt is off: the runners of alice and bob are too old for prompts; upgrade them.")
    state = views.prompt_state(hub.conn(), f"agent:{a.id}")
    assert state["reason"] == "Prompt is off: alice's runner is too old for prompts; upgrade it."


def test_heartbeat_tells_the_hub_whether_the_runner_runs_prompts(trio):
    """Set by a heartbeat with ``"prompts": 1``, cleared by one without it (a v0.1.0 runner's, which still works)."""
    hub, a, b, c = trio

    def support():
        return hub.conn().execute("SELECT prompt_support FROM agents WHERE id = ?", (a.id,)).fetchone()[0]

    assert support() == 0
    with hub.client.websocket_connect("/api/v1/ws", headers=a.headers) as ws:
        ws.receive_json()
        for frame, expected in (({"prompts": 1}, 1), ({"checkouts": {"proj": "/w"}, "current_dir": None}, 0),
                                ({"checkouts": {"proj": "/w"}, "current_dir": None, "prompts": 1}, 1), ({}, 0),
                                ({"prompts": 1}, 1), ({"prompts": "yes"}, 0)):
            ws.send_json({"type": "heartbeat", **frame})
            assert ws.receive_json()["type"] == "heartbeat_ack"
            assert support() == expected, frame
    row = hub.conn().execute("SELECT checkouts, last_heartbeat FROM agents WHERE id = ?", (a.id,)).fetchone()
    assert json.loads(row["checkouts"]) == {"proj": "/w"} and row["last_heartbeat"]


def test_a_runner_fails_a_prompt_that_was_too_old_when_it_picked_it_up(trio):
    hub, a, b, c = trio
    online(hub, a)
    old, fresh = create(hub, f"agent:{a.id}"), create(hub, f"agent:{a.id}")
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE prompts SET created_at = ? WHERE id = ?",
                     (db.now_iso(datetime.now(UTC) - timedelta(hours=6, minutes=1)), old["id"]))
    r = api(hub, a, "POST", f"/{fresh['id']}/finish", json={"ok": False, "stale": True})
    assert r.status_code == 409  # the hub checks the age itself
    r = api(hub, a, "POST", f"/{old['id']}/finish", json={"ok": True, "note": "ignored", "stale": True})
    assert r.status_code == 200 and r.json()["my_status"] == "failed" and r.json()["status"] == "failed"
    assert prompts.get(conn, old["id"])["recipients"][0]["note"] == "too old when the runner picked it up"
    api(hub, a, "POST", f"/{fresh['id']}/start")
    assert api(hub, a, "POST", f"/{fresh['id']}/finish", json={"ok": False, "stale": True}).status_code == 409


def test_long_private_copy_is_truncated_with_a_pointer(trio):
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}", body="q" * 8000)
    body = private.thread(hub.conn(), a.id)[0]["body"]
    assert len(body) <= private.MAX_BODY_CHARS and body.startswith(f"Prompt #{p['id']}: ")
    assert f"prompt_status with prompt_id={p['id']}" in body


def test_events_carry_no_body(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b)
    online(hub, a, b)
    p = create(hub, f"task:{t['id']}")
    api(hub, a, "POST", f"/{p['id']}/start")
    api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "my part", "body": "details"})
    api(hub, b, "POST", f"/{p['id']}/decline", json={"reason": "busy"})
    evs = prompt_events(hub, p["id"])
    assert [e["type"] for e in evs] == ["prompt.created", "prompt.updated", "prompt.updated", "prompt.updated"]
    for e in evs:
        assert SECRET not in json.dumps(e["payload"])
        assert SECRET not in event_summary(e, {})
        note = build_notification(e, {}, CFG)
        assert note is None or SECRET not in json.dumps(note)


# --- the agent API -------------------------------------------------------------------------------


def test_pending_start_finish_single(trio):
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}")
    got = api(hub, a, "GET", "/pending").json()["prompts"]
    assert [(x["id"], x["mode"], x["my_status"], x["body"]) for x in got] == [(p["id"], "single", "pending", p["body"])]
    assert api(hub, b, "GET", "/pending").json()["prompts"] == []
    r = api(hub, a, "POST", f"/{p['id']}/start")
    assert r.status_code == 200 and r.json()["status"] == "running" and r.json()["my_status"] == "running"
    assert api(hub, a, "POST", f"/{p['id']}/start").status_code == 409
    assert [x["my_status"] for x in api(hub, a, "GET", "/pending").json()["prompts"]] == ["running"]  # resumable
    r = api(hub, a, "POST", f"/{p['id']}/finish", json={"ok": True, "note": "did it"})
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["my_status"] == "done"
    full = prompts.get(hub.conn(), p["id"])
    assert full["finished_at"] and full["recipients"][0]["note"] == "did it"
    assert api(hub, a, "GET", "/pending").json()["prompts"] == []


def test_single_recipient_failing_fails_the_prompt(trio):
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}")
    api(hub, a, "POST", f"/{p['id']}/start")
    r = api(hub, a, "POST", f"/{p['id']}/finish", json={"ok": False, "note": "no checkout configured for proj"})
    assert r.json()["status"] == "failed" and r.json()["my_status"] == "failed"
    ev = prompt_events(hub, p["id"])[-1]
    msg = build_notification(ev, {a.id: "alice"}, CFG)
    assert msg["title"] == f"Prompt #{p['id']}: alice failed" and msg["click"].endswith(f"/ui/prompts/{p['id']}")
    assert "checkout" not in json.dumps(msg)  # names only


def test_other_agents_cannot_see_or_act_on_a_prompt(trio):
    hub, a, b, c = trio
    online(hub, a, b)
    p = create(hub, f"agent:{a.id}")
    for method, path, body in (("GET", f"/{p['id']}", None), ("POST", f"/{p['id']}/start", None),
                               ("POST", f"/{p['id']}/finish", {"ok": True}),
                               ("POST", f"/{p['id']}/claim", {"title": "x"}),
                               ("POST", f"/{p['id']}/decline", {"reason": "no"}),
                               ("GET", "/9999", None)):
        assert api(hub, b, method, path, json=body).status_code == 404, (method, path)
    assert prompts.get(hub.conn(), p["id"])["recipients"][0]["status"] == "pending"


def test_pending_agent_cannot_use_the_api(hub):
    p = hub.enroll("newbie", approve=False)
    assert api(hub, p, "GET", "/pending").status_code == 403


def test_recipient_view_shows_body_thread_and_co_recipients(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b, c, title="Visible task")
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "secret", actor=OPERATOR)
        store.set_agent_projects(conn, b.id, ["proj", "secret"], actor=OPERATOR)
    mk_task(hub, b, project="secret", title="Hidden elsewhere")
    online(hub, a, b)
    p = create(hub, f"task:{t['id']}")
    view = api(hub, a, "GET", f"/{p['id']}").json()
    assert view["body"] == p["body"] and view["mode"] == "group" and view["status"] == "discussing"
    assert view["thread"] == {"kind": "channel", "project": "proj", "channel": f"prompt-{p['id']}",
                              "key": f"board:ch-proj~prompt-{p['id']}"}
    others = {r["name"]: r for r in view["recipients"]}
    assert set(others) == {"alice", "bob", "carol"} and others["carol"]["status"] == "skipped"
    assert others["bob"]["device"] == "bob-host.example.ts.net" and others["bob"]["connected"] is True
    assert [x["title"] for x in others["bob"]["tasks"]] == ["Visible task"]  # not other projects' tasks
    assert view["my_status"] == "pending"


def test_device_thread_view(trio):
    hub, a, b, c = trio
    same_device(hub, "nbox", a, b)
    online(hub, a, b)
    p = create(hub, "device:nbox")
    view = api(hub, b, "GET", f"/{p['id']}").json()
    assert view["thread"] == {"kind": "device", "node_id": "nbox", "key": "device:nbox"}


# --- claim and decline -----------------------------------------------------------------------------


@pytest.fixture
def group(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b, c)
    online(hub, a, b, c)
    return hub, a, b, c, create(hub, f"task:{t['id']}")


def test_claim_creates_one_approved_task_for_the_agent(group):
    hub, a, b, c, p = group
    before = hub.conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    r = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "  Write the parser ", "body": "Only the parser."})
    assert r.status_code == 201, r.text
    task = r.json()
    assert (task["title"], task["body"], task["state"], task["project"]) == (
        "Write the parser", "Only the parser.", "open", "proj")
    # the title and body are the agent's words, so the task is the agent's; no approval row (the prompt was the
    # operator's)
    assert task["created_by"] == a.id and task["approval_id"] is None and task["approval_status"] is None
    assert [(m["agent_id"], m["role"], m["stage"], m["budget"], m["added_by"]) for m in task["agents"]] == [
        (a.id, f"Share of prompt #{p['id']}", "works", tasks.DEFAULT_BUDGET, a.id)]
    assert hub.conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before + 1
    created = hub.conn().execute("SELECT actor, payload FROM events WHERE type = 'task.created' "
                                 "ORDER BY id DESC LIMIT 1").fetchone()
    assert created["actor"] == a.id and json.loads(created["payload"])["prompt_id"] == p["id"]
    rec = {r["agent_id"]: r for r in prompts.get(hub.conn(), p["id"])["recipients"]}
    assert rec[a.id]["status"] == "claimed" and rec[a.id]["task_id"] == task["id"]
    assert rec[a.id]["task_title"] == "Write the parser"


def test_share_task_is_launchable_by_the_runner(group):
    import asyncio
    from types import SimpleNamespace

    from agenthub.runner.daemon import Runner

    hub, a, b, c, p = group
    tid = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "mine"}).json()["id"]

    async def get_task(task_id):
        return hub.client.get(f"/api/v1/tasks/{task_id}", headers=a.headers).json()

    stub = SimpleNamespace(client=SimpleNamespace(get_task=get_task), agent=SimpleNamespace(agent_id=a.id))
    task, me = asyncio.run(Runner._verified_task(stub, tid))
    assert task["state"] == "open" and me["role"] == f"Share of prompt #{p['id']}"


def test_dashboard_shows_the_share_task_by_the_agent(group):
    hub, a, b, c, p = group
    tid = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "Parser share"}).json()["id"]
    page = hub.client.get(f"/ui/tasks/{tid}", headers=hub.op_headers()).text
    assert "created by alice" in page
    project = hub.client.get("/ui/projects/proj", headers=hub.op_headers()).text
    assert 'Parser share</span> <span class="by-label">by alice</span>' in project


def test_claim_after_decline_is_refused(group):
    hub, a, b, c, p = group
    assert api(hub, a, "POST", f"/{p['id']}/decline", json={"reason": "busy"}).status_code == 200
    before = hub.conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "after all"}).status_code == 409
    assert hub.conn().execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


@pytest.mark.parametrize("kind", ["task", "project"])
def test_claim_by_an_agent_no_longer_in_the_project_creates_nothing(hub, kind):
    a, b = hub.enroll("alice"), hub.enroll("bob")
    t = mk_task(hub, a, b)
    online(hub, a, b)
    p = create(hub, f"task:{t['id']}" if kind == "task" else "project:proj")
    conn = hub.conn()
    with db.transaction(conn):
        store.ensure_project(conn, "other", actor=OPERATOR)
        store.set_agent_projects(conn, a.id, ["other"], actor=OPERATOR)
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    r = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "mine"})
    assert 400 <= r.status_code < 500
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    assert {x["agent_id"]: x["status"] for x in prompts.get(conn, p["id"])["recipients"]}[a.id] == "pending"


def test_second_claim_by_the_same_agent_is_refused(group):
    hub, a, b, c, p = group
    first = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "part one"}).json()
    r = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "part two"})
    assert r.status_code == 409
    assert hub.conn().execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'part%'").fetchone()[0] == 1
    rec = {r["agent_id"]: r for r in prompts.get(hub.conn(), p["id"])["recipients"]}
    assert rec[a.id]["task_id"] == first["id"]
    assert api(hub, a, "POST", f"/{p['id']}/decline", json={"reason": "changed my mind"}).status_code == 409


def test_two_agents_claim_overlapping_shares_each_get_one_task(group):
    hub, a, b, c, p = group
    ta = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "the parser"}).json()
    tb = api(hub, b, "POST", f"/{p['id']}/claim", json={"title": "the parser"}).json()
    assert ta["id"] != tb["id"]
    assert [m["agent_id"] for m in ta["agents"]] == [a.id] and [m["agent_id"] for m in tb["agents"]] == [b.id]


def test_claim_validates_title_and_body(group):
    hub, a, b, c, p = group
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "   "}).status_code == 422
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x" * 201}).status_code == 422
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x", "body": "b" * 16001}).status_code == 422
    assert prompts.get(hub.conn(), p["id"])["recipients"][0]["status"] == "pending"


def test_single_prompts_have_no_shares(trio):
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}")
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x"}).status_code == 409
    assert api(hub, a, "POST", f"/{p['id']}/decline", json={"reason": "x"}).status_code == 409


def test_skipped_recipient_cannot_claim(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b, c)
    online(hub, a, b)
    p = create(hub, f"task:{t['id']}")
    assert api(hub, c, "POST", f"/{p['id']}/claim", json={"title": "x"}).status_code == 409


def test_decline_and_done_when_all_decided(group):
    hub, a, b, c, p = group
    api(hub, a, "POST", f"/{p['id']}/start")
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "mine"}).status_code == 201
    r = api(hub, b, "POST", f"/{p['id']}/decline", json={"reason": "my machine has no GPU"})
    assert r.status_code == 200 and r.json()["my_status"] == "declined" and r.json()["status"] == "discussing"
    # finishing the session after claiming keeps the claim
    assert api(hub, a, "POST", f"/{p['id']}/finish", json={"ok": True}).json()["my_status"] == "claimed"
    r = api(hub, c, "POST", f"/{p['id']}/decline", json={"reason": "nothing left"})
    assert r.json()["status"] == "done"
    full = prompts.get(hub.conn(), p["id"])
    assert full["status"] == "done" and full["finished_at"]
    assert {r["agent_id"]: r["note"] for r in full["recipients"]}[b.id] == "my machine has no GPU"
    last = prompt_events(hub, p["id"])[-1]
    assert last["payload"]["status"] == "done"
    msg = build_notification(last, {}, CFG)
    assert msg["title"] == f"Prompt #{p['id']}: done"


def test_device_prompt_claim_picks_a_project(hub):
    a, b = hub.enroll("alice", projects=("proj", "other")), hub.enroll("bob", projects=("other",))
    same_device(hub, "nbox", a, b)
    online(hub, a, b)
    mk_task(hub, b, project="other")
    p = create(hub, "device:nbox")
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x", "project": "nope"}).status_code == 403
    r = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x", "project": "other"})
    assert r.status_code == 201 and r.json()["project"] == "other"
    r = api(hub, b, "POST", f"/{p['id']}/claim", json={"title": "y"})  # its current task's project
    assert r.status_code == 201 and r.json()["project"] == "other"


def test_device_prompt_claim_without_a_task_must_name_the_project(hub):
    """No guessing: an agent with no current task says where its share goes."""
    a, b = hub.enroll("alice", projects=("proj", "other")), hub.enroll("bob", projects=("other",))
    same_device(hub, "nbox", a, b)
    online(hub, a, b)
    p = create(hub, "device:nbox")
    r = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x"})
    assert r.status_code == 422 and "say which project with project=" in r.json()["detail"]
    assert prompts.get(hub.conn(), p["id"])["recipients"][0]["status"] == "pending"
    r = api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x", "project": "proj"})
    assert r.status_code == 201 and r.json()["project"] == "proj"


def test_project_must_match_the_prompts_project(group):
    hub, a, b, c, p = group
    assert api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "x", "project": "else"}).status_code == 422


def test_the_card_labels_the_hubs_notes_and_never_guesses_from_agent_text(trio):
    hub, a, b, c = trio
    online(hub, a, b, c)
    t = mk_task(hub, a, b, c)
    forged = create(hub, f"agent:{a.id}")
    api(hub, a, "POST", f"/{forged['id']}/start")
    api(hub, a, "POST", f"/{forged['id']}/finish", json={"ok": False, "note": "agent revoked"})  # agent text
    grp = create(hub, f"task:{t['id']}")
    api(hub, b, "POST", f"/{grp['id']}/decline", json={"reason": "withdrawn by the operator"})  # agent text
    withdraw(hub, grp["id"])  # the hub's note for alice and carol
    conn = hub.conn()
    flags = lambda pid: {r["agent_id"]: r["note_by_hub"] for r in conn.execute(
        "SELECT agent_id, note_by_hub FROM prompt_recipients WHERE prompt_id = ?", (pid,))}
    assert flags(forged["id"]) == {a.id: 0}
    assert flags(grp["id"]) == {a.id: 1, b.id: 0, c.id: 1}
    page = hub.client.get(f"/ui/prompts/{forged['id']}", headers=hub.op_headers()).text
    assert "hub note" not in page and "agent text" in page and "note from alice" in page
    page = hub.client.get(f"/ui/prompts/{grp['id']}", headers=hub.op_headers()).text
    assert page.count('<span class="tag">hub note</span>') == 2 and "note from bob" in page
    online(hub, a, connected=False)
    stale = create(hub, f"agent:{b.id}")
    revoked = create(hub, f"agent:{c.id}")
    with db.transaction(conn):
        conn.execute("UPDATE prompts SET created_at = ? WHERE id = ?",
                     (db.now_iso(datetime.now(UTC) - timedelta(hours=7)), stale["id"]))
    api(hub, b, "POST", f"/{stale['id']}/finish", json={"ok": False, "stale": True})
    hub.set_status(c, "revoked")
    with db.transaction(conn):
        prompts.expire(conn, datetime.now(UTC), minutes=30)
    assert flags(stale["id"]) == {b.id: 1} and flags(revoked["id"]) == {c.id: 1}
    skipped = create(hub, f"agent:{a.id}")
    assert flags(skipped["id"]) == {a.id: 1}


# --- withdraw ---------------------------------------------------------------------------------------


def withdraw(hub, pid, **data):
    return hub.client.post(f"/ui/prompts/{pid}/withdraw", headers=hub.op_headers(), follow_redirects=False,
                           data={"csrf": hub.csrf(), **data})


def test_operator_withdraws_the_recipients_that_have_not_started(group):
    hub, a, b, c, p = group
    api(hub, b, "POST", f"/{p['id']}/start")
    page = hub.client.get(f"/ui/prompts/{p['id']}", headers=hub.op_headers()).text
    assert f'action="/ui/prompts/{p["id"]}/withdraw"' in page
    r = withdraw(hub, p["id"])
    assert r.status_code == 303 and r.headers["location"] == f"/ui/prompts/{p['id']}"
    got = prompts.get(hub.conn(), p["id"])
    assert {r["agent_id"]: (r["status"], r["note"]) for r in got["recipients"]} == {
        a.id: ("failed", "withdrawn by the operator"), b.id: ("running", ""),
        c.id: ("failed", "withdrawn by the operator")}
    assert got["status"] == "discussing"  # bob's session is left to finish
    assert api(hub, a, "GET", "/pending").json()["prompts"] == []
    assert api(hub, a, "POST", f"/{p['id']}/start").status_code == 409  # a runner that queued it drops it
    ev = prompt_events(hub, p["id"])[-1]
    assert ev["actor"] == f"human:{OP_LOGIN}" and sorted(ev["payload"]["withdrawn"]) == sorted([a.id, c.id])
    assert sorted(bus.recipients(ev, hub.conn())) == sorted([a.id, b.id, c.id])  # their runners hear of it
    assert build_notification(ev, {}, CFG) is None  # the operator did it: no push
    assert "withdrawn" in event_summary(ev, {a.id: "alice", c.id: "carol"})
    page = hub.client.get(f"/ui/prompts/{p['id']}", headers=hub.op_headers()).text
    assert "/withdraw" not in page and "withdrawn by the operator" in page  # nothing left to withdraw
    assert withdraw(hub, p["id"]).status_code == 409
    api(hub, b, "POST", f"/{p['id']}/claim", json={"title": "mine"})
    assert prompts.get(hub.conn(), p["id"])["status"] == "done"


def test_withdrawing_a_single_prompt_fails_it(trio):
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}")
    panel = hub.client.get(f"/ui/panel?item=agent:{a.id}", headers=hub.op_headers()).text
    assert f'hx-post="/ui/prompts/{p["id"]}/withdraw"' in panel and 'name="panel" value="agent:' in panel
    r = hub.client.post(f"/ui/prompts/{p['id']}/withdraw", headers={**hub.op_headers(), "hx-request": "true"},
                        data={"csrf": hub.csrf(), "panel": f"agent:{a.id}"})
    assert r.status_code == 200 and "/withdraw" not in r.text  # the panel, re-rendered
    got = prompts.get(hub.conn(), p["id"])
    assert got["status"] == "failed" and got["finished_at"] and got["recipients"][0]["status"] == "failed"


def test_withdraw_needs_the_operator_and_csrf(trio):
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}")
    r = hub.client.post(f"/ui/prompts/{p['id']}/withdraw", headers=hub.op_headers(), follow_redirects=False)
    assert r.status_code == 403
    with pytest.raises(store.Forbidden):
        conn = hub.conn()
        with db.transaction(conn):
            prompts.withdraw(conn, p["id"], actor=a.id)
    assert withdraw(hub, 9999).status_code == 404
    assert prompts.get(hub.conn(), p["id"])["recipients"][0]["status"] == "pending"


# --- expiry -----------------------------------------------------------------------------------------


def test_expiry_marks_undecided_recipients_timed_out(group):
    hub, a, b, c, p = group
    api(hub, a, "POST", f"/{p['id']}/claim", json={"title": "mine"})
    api(hub, b, "POST", f"/{p['id']}/start")
    conn = hub.conn()
    with db.transaction(conn):
        assert prompts.expire(conn, datetime.now(UTC) + timedelta(minutes=29), minutes=30) == []
    with db.transaction(conn):
        assert prompts.expire(conn, datetime.now(UTC) + timedelta(minutes=31), minutes=30) == [p["id"]]
    full = prompts.get(conn, p["id"])
    assert {r["agent_id"]: r["status"] for r in full["recipients"]} == {a.id: "claimed", b.id: "timed_out",
                                                                       c.id: "timed_out"}
    assert full["status"] == "done"
    ev = prompt_events(hub, p["id"])[-1]
    assert sorted(ev["payload"]["timed_out"]) == sorted([b.id, c.id]) and ev["actor"] == "system:hub"
    msg = build_notification(ev, {b.id: "bob", c.id: "carol"}, CFG)
    assert "timed out" in msg["title"] and "bob" in msg["title"] and "carol" in msg["title"]
    # late moves are refused, and a late finish changes nothing
    assert api(hub, b, "POST", f"/{p['id']}/claim", json={"title": "late"}).status_code == 409
    assert api(hub, b, "POST", f"/{p['id']}/finish", json={"ok": True}).json()["my_status"] == "timed_out"


def test_expiry_leaves_single_prompts_alone_but_fails_revoked_recipients(trio):
    hub, a, b, c = trio
    online(hub, a, b)
    pa, pb = create(hub, f"agent:{a.id}"), create(hub, f"agent:{b.id}")
    hub.set_status(b, "revoked")
    conn = hub.conn()
    with db.transaction(conn):
        prompts.expire(conn, datetime.now(UTC) + timedelta(days=1), minutes=30)
    assert prompts.get(conn, pa["id"])["status"] == "pending"
    got = prompts.get(conn, pb["id"])
    assert got["status"] == "failed" and got["recipients"][0]["status"] == "failed"


def test_single_prompt_to_an_agent_that_went_offline_times_out(trio):
    hub, a, b, c = trio
    online(hub, a, b)
    stuck = create(hub, f"agent:{a.id}")
    started = create(hub, f"agent:{b.id}")
    api(hub, b, "POST", f"/{started['id']}/start")
    online(hub, a, connected=False)
    online(hub, c)
    live = create(hub, f"agent:{c.id}")
    conn = hub.conn()
    with db.transaction(conn):
        assert prompts.expire(conn, datetime.now(UTC) + timedelta(minutes=29), minutes=30) == []
    with db.transaction(conn):
        assert prompts.expire(conn, datetime.now(UTC) + timedelta(minutes=31), minutes=30) == [stuck["id"]]
    got = prompts.get(conn, stuck["id"])
    assert got["status"] == "failed" and got["finished_at"] and got["recipients"][0]["status"] == "timed_out"
    assert prompts.get(conn, started["id"])["status"] == "running"  # connected and running: never cut off
    assert prompts.get(conn, live["id"])["status"] == "pending"  # connected: its runner will pick it up
    ev = prompt_events(hub, stuck["id"])[-1]
    assert ev["payload"]["status"] == "failed" and ev["payload"]["timed_out"] == [a.id]
    msg = build_notification(ev, {a.id: "alice"}, CFG)
    assert msg["title"] == f"Prompt #{stuck['id']}: alice timed out"
    assert event_summary(ev, {a.id: "alice"}) == f"prompt #{stuck['id']} failed: alice timed out"


def test_pending_single_prompt_survives_a_brief_disconnect(trio):
    """A hub restart or a network blip right at the expiry check doesn't time out a pending prompt: like a running
    one, it times out only once the agent has been gone for longer than the discussion time."""
    hub, a, b, c = trio
    online(hub, a, b)
    blip, gone = create(hub, f"agent:{a.id}"), create(hub, f"agent:{b.id}")
    conn = hub.conn()
    later = datetime.now(UTC) + timedelta(minutes=31)
    with db.transaction(conn):
        conn.execute("UPDATE agents SET connected = 0, last_heartbeat = ? WHERE id = ?",
                     (db.now_iso(later - timedelta(seconds=5)), a.id))
        conn.execute("UPDATE agents SET connected = 0, last_heartbeat = ? WHERE id = ?",
                     (db.now_iso(later - timedelta(minutes=31)), b.id))
    with db.transaction(conn):
        assert prompts.expire(conn, later, minutes=30) == [gone["id"]]
    assert prompts.get(conn, blip["id"])["status"] == "pending"
    assert prompts.get(conn, gone["id"])["recipients"][0]["status"] == "timed_out"


def test_pending_prompt_whose_runner_was_downgraded_times_out(trio):
    """A connected agent whose runner went back to one too old for prompts would never run it."""
    hub, a, b, c = trio
    online(hub, a)
    p = create(hub, f"agent:{a.id}")
    online(hub, a, prompts=False)
    conn = hub.conn()
    with db.transaction(conn):
        assert prompts.expire(conn, datetime.now(UTC) + timedelta(minutes=29), minutes=30) == []
    with db.transaction(conn):
        assert prompts.expire(conn, datetime.now(UTC) + timedelta(minutes=31), minutes=30) == [p["id"]]
    got = prompts.get(conn, p["id"])
    assert got["status"] == "failed" and got["recipients"][0]["status"] == "timed_out"
    assert got["recipients"][0]["note"] == "runner too old for prompts; never started"


def test_running_prompt_whose_runner_never_comes_back_fails(trio):
    hub, a, b, c = trio
    online(hub, a, b)
    gone, alive = create(hub, f"agent:{a.id}"), create(hub, f"agent:{b.id}")
    for agent, p in ((a, gone), (b, alive)):
        api(hub, agent, "POST", f"/{p['id']}/start")
    conn = hub.conn()
    beat = datetime.now(UTC)
    with db.transaction(conn):
        conn.execute("UPDATE agents SET connected = 0, last_heartbeat = ? WHERE id = ?", (db.now_iso(beat), a.id))
        conn.execute("UPDATE agents SET last_heartbeat = ? WHERE id = ?", (db.now_iso(beat), b.id))
    with db.transaction(conn):  # disconnected, but not for long enough yet
        assert prompts.expire(conn, beat + timedelta(minutes=29), minutes=30) == []
    with db.transaction(conn):
        assert prompts.expire(conn, beat + timedelta(minutes=31), minutes=30) == [gone["id"]]
    got = prompts.get(conn, gone["id"])
    assert got["status"] == "failed" and got["finished_at"]
    assert got["recipients"][0]["status"] == "failed" and "disconnected" in got["recipients"][0]["note"]
    assert prompts.get(conn, alive["id"])["status"] == "running"  # connected: still running
    ev = prompt_events(hub, gone["id"])[-1]
    assert ev["payload"]["status"] == "failed" and ev["payload"]["failed"] == [a.id]
    assert build_notification(ev, {a.id: "alice"}, CFG)["title"] == f"Prompt #{gone['id']}: alice failed"


def test_target_with_nobody_to_prompt(trio):
    hub, a, b, c = trio
    p = create(hub, "project:proj")  # no live tasks: no recipients at all
    assert p["status"] == "failed" and p["recipients"] == []
    msg = build_notification(prompt_events(hub, p["id"])[0], {}, CFG)
    assert msg["title"] == f"Prompt #{p['id']}: nobody to prompt"


def test_hub_loop_runs_expiry(tmp_path):
    from .conftest import make_hub

    hub = make_hub(tmp_path)
    hub.state.config.prompt_discussion_minutes = 0
    hub.state.prompt_expiry_seconds = 0.02
    a, b = hub.enroll("a"), hub.enroll("b")
    t = mk_task(hub, a, b)
    online(hub, a, b)
    p = create(hub, f"task:{t['id']}")
    import time

    with TestClient(hub.client.app, client=("127.0.0.1", 40000)):
        deadline = time.monotonic() + 5
        while prompts.get(hub.conn(), p["id"])["status"] != "done" and time.monotonic() < deadline:
            time.sleep(0.02)
    assert prompts.get(hub.conn(), p["id"])["status"] == "done"


def test_expire_prompts_helper_uses_the_configured_minutes(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b)
    online(hub, a, b)
    p = create(hub, f"task:{t['id']}")
    expire_prompts(hub.state)
    assert prompts.get(hub.conn(), p["id"])["status"] == "discussing"
    hub.state.config.prompt_discussion_minutes = 0
    expire_prompts(hub.state)
    assert prompts.get(hub.conn(), p["id"])["status"] == "done"


def test_config_default():
    assert HubConfig().prompt_discussion_minutes == 30


@pytest.mark.parametrize("value", ["0", "1441", "-5", "true", '"30"', "2.5"])
def test_discussion_minutes_are_checked_when_the_config_loads(tmp_path, value):
    path = tmp_path / "hub.toml"
    path.write_text(f"prompt_discussion_minutes = {value}\n")
    with pytest.raises(ValueError, match="prompt_discussion_minutes"):
        HubConfig.load(path)


def test_discussion_minutes_bounds_load(tmp_path):
    path = tmp_path / "hub.toml"
    for value in (1, 1440):
        path.write_text(f"prompt_discussion_minutes = {value}\n")
        assert HubConfig.load(path).prompt_discussion_minutes == value


# --- routing and summaries -------------------------------------------------------------------------


def test_bus_routes_prompt_events_to_recipients(group):
    hub, a, b, c, p = group
    conn = hub.conn()
    created = prompt_events(hub, p["id"])[0]
    assert bus.recipients(created, conn) == {a.id, b.id, c.id}
    note = bus._notification(created, a.id)
    assert note["event"]["data"]["prompt_id"] == p["id"] and note["event"]["data"]["mode"] == "group"
    api(hub, a, "POST", f"/{p['id']}/decline", json={"reason": "no"})
    updated = prompt_events(hub, p["id"])[-1]
    assert updated["type"] == "prompt.updated" and bus.recipients(updated, conn) == {a.id, b.id, c.id}


def test_bus_skips_offline_recipients_of_created(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b)
    online(hub, a)
    p = create(hub, f"task:{t['id']}")
    assert bus.recipients(prompt_events(hub, p["id"])[0], hub.conn()) == {a.id}


def test_skipped_recipients_notify_the_operator(trio):
    hub, a, b, c = trio
    t = mk_task(hub, a, b, c)
    online(hub, a)
    p = create(hub, f"task:{t['id']}")
    msg = build_notification(prompt_events(hub, p["id"])[0], {b.id: "bob", c.id: "carol"}, CFG)
    assert msg["title"].startswith(f"Prompt #{p['id']}:") and "bob" in msg["title"] and "carol" in msg["title"]
    online(hub, b, c)
    p2 = create(hub, f"task:{t['id']}")
    assert build_notification(prompt_events(hub, p2["id"])[0], {}, CFG) is None


def test_event_summaries(group):
    hub, a, b, c, p = group
    names = {a.id: "alice", b.id: "bob", c.id: "carol"}
    created = prompt_events(hub, p["id"])[0]
    assert event_summary(created, names) == f"prompt #{p['id']} to alice, bob, carol (group)"
    api(hub, b, "POST", f"/{p['id']}/decline", json={"reason": "no"})
    assert event_summary(prompt_events(hub, p["id"])[-1], names) == f"prompt #{p['id']}: bob declined"
