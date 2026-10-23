"""Names with capitals (0.3.9.0): kept as written, unique ignoring case, looked up ignoring case everywhere (API, MCP,
CLI, @mentions); slug() for systems that can't take capitals; migration 23 on an existing database."""

import sqlite3

import httpx
import pytest
from mcp import Client
from typer.testing import CliRunner

from agenthub import names
from agenthub.cli import app as cli_app
from agenthub.client import HubClient
from agenthub.hub import bus, chats, db, store, tasks
from agenthub.mcp_server import build_server

from .conftest import OPERATOR


def text(result) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


def api(hub, agent, method, path, **kw):
    return hub.client.request(method, f"/api/v1{path}", headers=agent.headers, **kw)


def op_send(hub, kind, ref, body):
    conn = hub.conn()
    with db.transaction(conn):
        return chats.send(conn, kind, ref, body, OPERATOR)


# --- the rule -----------------------------------------------------------------------------------------------------


def test_name_rule_and_slug():
    for good in ("golem", "Golem", "Hub-Builder-1", "A", "9lives", "x_Y-z", "a" * 64):
        assert names.is_name(good), good
    for bad in ("", "-x", "_x", "a b", "a" * 65, "café", "a.b", "a/b", "K"):  # U+212A KELVIN SIGN
        assert not names.is_name(bad), bad
    assert names.slug("Hub-Builder-1") == "hub-builder-1"
    assert names.slug("golem") == "golem"  # a lowercase name is its own slug: units, folders, branches don't move
    assert names.same("Golem", "gOLEM") and not names.same("golem", "golem2")


def test_mentions_are_parsed_ignoring_case_and_kept_as_written():
    assert names.mentions("@Bob and @bob, ask @Hub-Builder-1.") == ["Bob", "Hub-Builder-1"]
    assert names.mentions("mail bob@example.com or me@x") == []  # an email address is no mention
    assert names.mentions("@@bob") == []
    assert names.mentions("(@alice) @carol's @dave-") == ["alice", "carol", "dave-"]
    assert names.mentions("@" + "a" * 65) == []  # too long to be a name, and no prefix of it is taken


# --- unique ignoring case -------------------------------------------------------------------------------------------


def test_projects_differing_only_in_case_are_one_project(hub):
    conn = hub.conn()
    with db.transaction(conn):
        assert store.create_project(conn, "Golem", actor=OPERATOR) == "Golem"
    with pytest.raises(store.Conflict, match="names ignore case"), db.transaction(conn):
        store.create_project(conn, "golem", actor=OPERATOR)
    with db.transaction(conn):
        assert store.ensure_project(conn, "GOLEM", actor=OPERATOR) is False
        assert store.ensure_projects(conn, ["golem", "Golem", "proj"], actor=OPERATOR) == ["Golem", "proj"]
    assert [p["name"] for p in store.list_projects(conn) if names.same(p["name"], "golem")] == ["Golem"]
    assert store.project_name(conn, "gOlEm") == "Golem" and store.project_name(conn, "nope") is None
    # the database enforces it too, whatever writes the row (a rolled-back hub that doesn't know name_key)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO projects (name, description, created_at) VALUES ('GOLEM', '', 'x')")


def test_agents_differing_only_in_case_are_rejected(hub):
    hub.enroll("Golem")
    ip = hub.new_ip()
    hub.whois.add(ip, node_id="nother", name="other-host.example.ts.net")
    r = hub.client.post("/api/v1/enroll", json={"code": hub.code(("proj",)), "name": "golem"},
                        headers={"x-forwarded-for": ip})
    assert r.status_code == 409 and "names ignore case" in r.json()["detail"]
    rows = hub.conn().execute("SELECT name, name_key FROM agents").fetchall()
    assert [tuple(r) for r in rows] == [("Golem", "golem")]


def test_names_are_shown_as_written(hub):
    a = hub.enroll("Hub-Builder-1", projects=("Golem",))
    me = api(hub, a, "GET", "/agents/me").json()
    assert me["name"] == "Hub-Builder-1" and me["allowed_projects"] == ["Golem"]


# --- lookups ignore case: API ---------------------------------------------------------------------------------------


def test_api_lookups_ignore_case(hub):
    a = hub.enroll("Alice", projects=("Golem",))
    b = hub.enroll("bob", projects=("golem",))  # the enrollment names the same project in other case
    assert store.find_agent(hub.conn(), "bob").allowed_projects == ("Golem",)
    created = api(hub, a, "POST", "/projects/GOLEM/tasks", json={"title": "t", "reason": "r"})
    assert created.status_code == 201, created.text
    assert created.json()["task"]["project"] == "Golem"
    assert [t["project"] for t in api(hub, b, "GET", "/projects/golem/tasks").json()["tasks"]] == ["Golem"]
    # a chat named in any case is the project's one chat; `to` names an agent in any case
    posted = api(hub, a, "POST", "/chats/project/gOLEM/messages", json={"body": "hi", "to": "BOB"})
    assert posted.status_code == 201, posted.text
    assert posted.json()["meta"]["to"] == b.id
    assert api(hub, b, "GET", "/chats/project/golem/messages").json()["chat"]["ref"] == "Golem"
    # a project the agent isn't in stays out of reach in any case
    c = hub.enroll("carol", projects=("other",))
    assert api(hub, c, "GET", "/projects/GOLEM/tasks").status_code == 403
    ask = api(hub, a, "POST", "/projects/golem/asks", json={"to": "BoB", "question": "q?", "timeout_seconds": 10})
    assert ask.status_code == 201, ask.text


def test_checkout_keys_are_stored_by_the_projects_spelling(hub):
    a = hub.enroll("alice", projects=("Golem",))
    with hub.client.websocket_connect("/api/v1/ws", headers=a.headers) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "heartbeat", "checkouts": {"golem": "/src/golem", "unknown": "/src/x"}})
        assert ws.receive_json()["type"] == "heartbeat_ack"
    row = hub.conn().execute("SELECT checkouts FROM agents WHERE id = ?", (a.id,)).fetchone()
    assert row["checkouts"] == '{"Golem":"/src/golem","unknown":"/src/x"}'


# --- lookups ignore case: MCP ---------------------------------------------------------------------------------------


async def test_mcp_tools_take_names_in_any_case(dev_hub):
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    a = dev_hub.enroll("Alice", projects=("Golem",))
    b = dev_hub.enroll("Bob", projects=("Golem",))
    server = build_server(lambda: HubClient("http://hub", a.token, transport=transport), poll_seconds=0.01)
    async with Client(server) as c:
        who = text(await c.call_tool("whoami", {}))
        assert "Alice" in who and "Golem" in who
        posted = await c.call_tool("chat_post", {"chat": "project:golem", "body": "hello @bob", "to": "BOB"})
        assert not posted.is_error, text(posted)
        listed = await c.call_tool("list_tasks", {"project": "GOLEM"})
        assert not listed.is_error, text(listed)
    row = dev_hub.conn().execute("SELECT meta FROM chat_messages WHERE body = 'hello @bob'").fetchone()
    assert f'"to":"{b.id}"' in row["meta"] and f'"mentions":["{b.id}"]' in row["meta"]


# --- lookups ignore case: CLI ---------------------------------------------------------------------------------------


def test_cli_takes_names_in_any_case(tmp_path):
    from agenthub.cli import _hub_conn
    from agenthub.hub.store import Device

    runner = CliRunner()
    runner.invoke(cli_app, ["hub", "init", "--no-systemd"])
    assert runner.invoke(cli_app, ["hub", "project", "add", "Golem"]).output.strip() == "created: Golem"
    assert runner.invoke(cli_app, ["hub", "project", "add", "golem"]).output.strip() == "exists: Golem"
    code = runner.invoke(cli_app, ["hub", "enroll", "--projects", "GOLEM"]).stdout.strip().splitlines()[0]
    conn = _hub_conn()
    with db.transaction(conn):
        store.redeem_enrollment(conn, code=code, name="Hub-Builder-1", device=Device("n1", "host", "100.64.0.9"))
    approved = runner.invoke(cli_app, ["hub", "agents", "approve", "hub-builder-1"])
    assert approved.exit_code == 0 and "approved Hub-Builder-1" in approved.output and "Golem" in approved.output
    created = runner.invoke(cli_app, ["hub", "tasks", "create", "golem", "T", "--assignee", "HUB-BUILDER-1"])
    assert created.exit_code == 0, created.output
    listed = runner.invoke(cli_app, ["hub", "tasks", "list", "--project", "gOLEM"]).output
    assert "Golem" in listed and "Hub-Builder-1" in listed
    assert runner.invoke(cli_app, ["hub", "agents", "projects", "HUB-builder-1", "golem,new"]).output.strip() == \
        "Hub-Builder-1: Golem, new"


# --- @mentions ------------------------------------------------------------------------------------------------------


def test_operator_mentions_resolve_ignoring_case_and_address_the_agent(hub):
    a = hub.enroll("Alice", projects=("Golem",))
    b = hub.enroll("Hub-Builder-1", projects=("Golem",))
    hub.enroll("carol", projects=("other",))
    body = "@hub-builder-1 and @CAROL (not here) and @nobody: go"
    m = op_send(hub, "project", "golem", body)
    assert m["body"] == body  # shown as written
    assert m["meta"]["mentions"] == [b.id]
    ds = {d["mentioned"] for d in api(hub, a, "GET", "/chats/deliveries").json()["deliveries"]}
    assert ds == {False}
    assert [d["mentioned"] for d in api(hub, b, "GET", "/chats/deliveries").json()["deliveries"]] == [True]


def test_agent_mentions_reach_the_mentioned_agent_as_mail(hub):
    a = hub.enroll("Alice", projects=("Golem",))
    b = hub.enroll("Bob", projects=("Golem",))
    c = hub.enroll("Carol", projects=("Golem",))
    # addressed to Carol, but it @mentions Bob: Bob gets it as mail addressed to him; Alice (the author) isn't
    # mentioned by her own @alice
    r = api(hub, a, "POST", "/chats/project/Golem/messages", json={"body": "@BOB @alice see this", "to": "carol"})
    assert r.status_code == 201, r.text
    assert r.json()["meta"]["mentions"] == [b.id]
    mail = [m for m in api(hub, b, "GET", "/inbox").json()["messages"] if m["kind"] == "chat"]
    assert [(m["body"], m["addressed_to_me"], m["mentioned"]) for m in mail] == [("@BOB @alice see this", True, True)]
    carol = [m for m in api(hub, c, "GET", "/inbox").json()["messages"] if m["kind"] == "chat"]
    assert [(m["addressed_to_me"], m["mentioned"]) for m in carol] == [(True, False)]
    # the push carries ids and flags only
    ev = hub.conn().execute("SELECT * FROM events WHERE type = 'chat.message' ORDER BY id DESC LIMIT 1").fetchone()
    from agenthub.hub.events import row_to_event

    event = row_to_event(ev)
    assert event["payload"]["mentions"] == [b.id] and "body" not in event["payload"]
    note = bus._notification(event, b.id)["event"]["data"]
    assert note["mentioned"] is True and note["addressed_to_me"] is True and "body" not in note


# --- migration 23 on an existing database -----------------------------------------------------------------------


def test_migration_on_an_existing_database_keeps_lowercase_names_working(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    newest = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", newest[:22])  # a 0.3.8 database
    conn = db.init_db(path)
    with db.transaction(conn):
        conn.execute("INSERT INTO projects (name, description, created_at) VALUES ('golem', '', 'x')")
        conn.execute("INSERT INTO agents (id, name, device_name, device_ip, device_node_id, token_hash, status, "
                     "allowed_projects, created_at) VALUES ('ag1', 'hub-builder-1', 'd', 'i', 'n', 'h', 'active', "
                     "'[\"golem\"]', 'x')")
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS", newest)
    conn = db.init_db(path)  # 0.3.9.0 starts
    assert conn.execute("SELECT name_key FROM projects").fetchone()[0] == "golem"
    assert conn.execute("SELECT name_key FROM agents").fetchone()[0] == "hub-builder-1"
    assert store.find_agent(conn, "hub-builder-1").id == "ag1" == store.find_agent(conn, "Hub-Builder-1").id
    assert store.project_name(conn, "golem") == "golem" == store.project_name(conn, "GOLEM")
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="Golem", title="t")
    assert t["project"] == "golem"
    with pytest.raises(store.Conflict), db.transaction(conn):
        store.create_project(conn, "Golem", actor=OPERATOR)


def test_a_rolled_back_hub_still_fills_name_key(tmp_path, monkeypatch):
    """0.3.8's code inserts without name_key; the triggers fill it, so 0.3.9.0's lookups and uniqueness hold after
    a downgrade and an upgrade again."""
    path = tmp_path / "hub.sqlite3"
    db.init_db(path).close()
    newest = len(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:22])
    conn = db.init_db(path)  # what a rolled-back 0.3.8 hub does at start: leaves the newer schema alone
    assert conn.execute("PRAGMA user_version").fetchone()[0] == newest
    conn.execute("INSERT INTO projects (name, description, created_at) VALUES ('legacy', '', 'x')")
    assert conn.execute("SELECT name_key FROM projects WHERE name = 'legacy'").fetchone()[0] == "legacy"
    conn.execute("UPDATE projects SET name = 'Legacy2' WHERE name = 'legacy'")
    assert conn.execute("SELECT name_key FROM projects WHERE name = 'Legacy2'").fetchone()[0] == "legacy2"


# --- respelling a name in other case (before rolling back to 0.3.8) --------------------------------------------------


def test_a_project_is_respelled_everywhere_it_is_named(hub):
    a = hub.enroll("alice", projects=("Golem",))
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="Golem", title="t")
        chats.send(conn, "project", "Golem", "hello", OPERATOR)
        chats.send(conn, "task", str(t["id"]), "on it", OPERATOR)
        store.create_enrollment(conn, created_by=OPERATOR, projects=["Golem"])
        conn.execute("UPDATE agents SET checkouts = '{\"Golem\":\"/src/g\"}' WHERE id = ?", (a.id,))
    with db.transaction(conn):
        assert store.rename_project(conn, "GOLEM", "golem", actor=OPERATOR) == "golem"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [p["name"] for p in store.list_projects(conn) if p["name"] != "proj"] == ["golem"]
    assert tasks.get_task(conn, t["id"])["project"] == "golem"
    assert {(r["kind"], r["ref"], r["project"]) for r in conn.execute("SELECT kind, ref, project FROM chats")} >= {
        ("project", "golem", "golem"), ("task", str(t["id"]), "golem")}
    assert store.find_agent(conn, a.id).allowed_projects == ("golem",)
    assert conn.execute("SELECT checkouts FROM agents WHERE id = ?", (a.id,)).fetchone()[0] == '{"golem":"/src/g"}'
    assert all("Golem" not in r[0] for r in conn.execute("SELECT allowed_projects FROM enrollments"))
    # what the hub did before the rename still reads, and an agent's calls work under the new spelling
    assert [m["body"] for m in api(hub, a, "GET", "/chats/project/golem/messages").json()["messages"]
            if m["kind"] == "text"] == ["hello"]
    assert api(hub, a, "GET", "/projects/golem/tasks").json()["tasks"][0]["id"] == t["id"]
    renamed = conn.execute("SELECT payload FROM events WHERE type = 'project.renamed'").fetchone()[0]
    assert renamed == '{"from":"Golem","to":"golem"}'


def test_a_rename_only_changes_case(hub):
    hub.enroll("Alice", projects=("Golem",))
    conn = hub.conn()
    for call in (lambda: store.rename_project(conn, "Golem", "golem2", actor=OPERATOR),
                 lambda: store.rename_agent(conn, "alice", "bob", actor=OPERATOR),
                 lambda: store.rename_agent(conn, "alice", "al ice", actor=OPERATOR)):
        with pytest.raises(store.Invalid), db.transaction(conn):
            call()
    with db.transaction(conn):
        a = store.rename_agent(conn, "ALICE", "alice", actor=OPERATOR)
    assert a.name == "alice" and conn.execute("SELECT name_key FROM agents").fetchone()[0] == "alice"


def test_cli_respells_agents_and_projects(tmp_path):
    from agenthub.cli import _hub_conn
    from agenthub.hub.store import Device

    runner = CliRunner()
    runner.invoke(cli_app, ["hub", "init", "--no-systemd"])
    code = runner.invoke(cli_app, ["hub", "enroll", "--projects", "Golem"]).stdout.strip().splitlines()[0]
    conn = _hub_conn()
    with db.transaction(conn):
        store.redeem_enrollment(conn, code=code, name="Hub-Builder-1", device=Device("n1", "host", "100.64.0.9"))
    r = runner.invoke(cli_app, ["hub", "agents", "rename", "hub-builder-1", "hub-builder-1"])
    assert r.exit_code == 0 and r.output.startswith("renamed Hub-Builder-1 to hub-builder-1"), r.output
    r = runner.invoke(cli_app, ["hub", "project", "rename", "golem", "golem"])
    assert r.exit_code == 0 and r.output.startswith("renamed project Golem to golem"), r.output
    assert runner.invoke(cli_app, ["hub", "project", "rename", "golem", "other"]).exit_code == 1
    assert runner.invoke(cli_app, ["hub", "agents", "list"]).output.split()[1] == "hub-builder-1"
