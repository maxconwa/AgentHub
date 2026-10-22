import json
import sqlite3

import httpx
import pytest

from agenthub.client import HubClient
from agenthub.hooks import install, permission
from agenthub.hub import approvals, db, tasks

from .conftest import OPERATOR

WRITE = {"tool_name": "Write", "tool_input": {"file_path": "/w/notes.md", "content": "hello"}}


@pytest.fixture
def task_env(dev_hub, monkeypatch):
    agent = dev_hub.enroll("worker")
    conn = dev_hub.conn()
    with db.transaction(conn):
        task = tasks.create_task(conn, actor=OPERATOR, project="proj", title="t", assignee="worker")
    monkeypatch.setenv("AGENTHUB_TASK_ID", str(task["id"]))
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    return dev_hub, task, lambda: HubClient("http://hub", agent.token, transport=transport)


def behavior(out):
    return out["hookSpecificOutput"]["decision"]["behavior"], out["hookSpecificOutput"]["decision"].get("message", "")


def decide(hub, approval_id, ok, note=""):
    conn = hub.conn()
    with db.transaction(conn):
        approvals.decide(conn, approval_id, approve=ok, actor=OPERATOR, note=note)


def test_interactive_sessions_are_left_alone(monkeypatch):
    monkeypatch.delenv("AGENTHUB_TASK_ID", raising=False)
    assert permission.handle(WRITE, client_factory=lambda: pytest.fail("no hub")) is None


def test_prompt_sessions_without_a_task_are_denied_with_a_pointer(monkeypatch):
    monkeypatch.delenv("AGENTHUB_TASK_ID", raising=False)
    monkeypatch.setenv("AGENTHUB_PROMPT_ID", "7")
    kind, msg = behavior(permission.handle(WRITE, client_factory=lambda: pytest.fail("no hub")))
    assert kind == "deny" and permission.NO_TASK_GRANTS in msg and "message_operator" in msg


def test_live_grant_flow_allows_exactly_once(task_env):
    hub, task, factory = task_env
    kind, msg = behavior(permission.handle(WRITE, client_factory=factory, wait_seconds=0, poll=0.01))
    assert kind == "deny" and "still pending" in msg
    (pending,) = approvals.list_approvals(hub.conn())
    assert pending["kind"] == "tool_permission" and pending["details"]["tool_name"] == "Write"
    assert pending["details"]["task_id"] == task["id"]
    decide(hub, pending["id"], True)
    assert behavior(permission.handle(WRITE, client_factory=factory, wait_seconds=0))[0] == "allow"
    assert behavior(permission.handle(WRITE, client_factory=factory, wait_seconds=0))[0] == "deny"


def test_grant_is_bound_to_exact_input(task_env):
    hub, task, factory = task_env
    permission.handle(WRITE, client_factory=factory, wait_seconds=0)
    decide(hub, approvals.list_approvals(hub.conn())[0]["id"], True)
    other = {"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd", "content": "x"}}
    assert behavior(permission.handle(other, client_factory=factory, wait_seconds=0))[0] == "deny"


def test_rejection_reason_reaches_the_agent(task_env):
    hub, task, factory = task_env
    permission.handle(WRITE, client_factory=factory, wait_seconds=0)
    decide(hub, approvals.list_approvals(hub.conn())[0]["id"], False, note="write to docs/ instead")
    kind, msg = behavior(permission.handle(WRITE, client_factory=factory, wait_seconds=0))
    # a new request is filed; the rejected one is not reused
    assert kind == "deny"
    rejected = approvals.list_approvals(hub.conn(), status="rejected")[0]
    assert rejected["decision_note"] == "write to docs/ instead"


def test_wait_returns_decision_made_while_waiting(task_env):
    import threading
    import time

    hub, task, factory = task_env
    out = {}
    t = threading.Thread(target=lambda: out.update(r=permission.handle(WRITE, client_factory=factory,
                                                                       wait_seconds=10, poll=0.02)))
    t.start()
    for _ in range(200):
        pending = approvals.list_approvals(hub.conn())
        if pending:
            break
        time.sleep(0.02)
    decide(hub, pending[0]["id"], True)
    t.join(10)
    assert behavior(out["r"])[0] == "allow"


def test_hub_down_denies(monkeypatch):
    monkeypatch.setenv("AGENTHUB_TASK_ID", "1")
    factory = lambda: HubClient("http://hub", "ahk_x_y", transport=httpx.MockTransport(lambda r: httpx.Response(503)))  # noqa: E731
    assert behavior(permission.handle(WRITE, client_factory=factory, wait_seconds=0))[0] == "deny"


def test_other_agents_cannot_request_for_my_task(task_env):
    hub, task, _ = task_env
    other = hub.enroll("other")
    r = hub.client.post("/api/v1/approvals/tool", headers=other.headers,
                        json={"task_id": task["id"], "tool_name": "Write", "tool_input": {}})
    assert r.status_code == 403


def test_large_inputs_are_hashed():
    big = {"file_path": "/x", "content": "a" * 10_000}
    small = permission.compact_input(big)
    assert set(small) == {"preview", "sha256", "size"} and len(small["preview"]) == permission.MAX_INLINE_INPUT
    changed = permission.compact_input({**big, "content": "a" * 9_999 + "b"})
    assert changed["sha256"] != small["sha256"]


def test_install_adds_permission_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    path = install.install("user", exe="/bin/agenthub", config_dir=tmp_path)
    data = json.loads(path.read_text())
    (entry,) = data["hooks"]["PermissionRequest"]
    assert entry["hooks"][0]["command"].endswith("hook permission")
    assert entry["hooks"][0]["timeout"] > permission.DEFAULT_WAIT_SECONDS
    install.uninstall("user")
    assert "PermissionRequest" not in json.loads(path.read_text())["hooks"]


def test_runner_service_unit(tmp_path):
    from agenthub.runner import service

    unit = service.write_unit("/opt/agenthub", tmp_path / "cfg", directory=tmp_path).read_text()
    assert "ExecStart=/opt/agenthub runner" in unit
    assert f"Environment=AGENTHUB_CONFIG_DIR={(tmp_path / 'cfg').resolve()}" in unit
    assert f"RestartPreventExitStatus={service.EXIT_REVOKED}" in unit


def test_migration_from_schema_2_preserves_data(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for version, script in enumerate(db.MIGRATIONS[:2], start=1):
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {version};COMMIT;")
    conn.executescript(
        "INSERT INTO projects VALUES ('p', '', 'x');"
        "INSERT INTO approvals (kind, project, requester, details, status, created_at) "
        "VALUES ('task_create', 'p', 'ag1', '{\"task_id\": 1}', 'pending', 'x');"
        "INSERT INTO tasks (project, title, created_by, state, approval_id, created_at, updated_at) "
        "VALUES ('p', 't', 'ag1', 'awaiting_approval', 1, 'x', 'x');"
    )
    conn.close()
    new = db.init_db(path)
    assert new.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert new.execute("SELECT kind FROM approvals").fetchone()[0] == "task_create"
    assert tuple(new.execute("SELECT approval_id, last_note FROM tasks").fetchone()) == (1, "")
    assert new.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert new.execute("PRAGMA foreign_key_check").fetchall() == []


def test_runner_launchd_plist(tmp_path, monkeypatch):
    import plistlib

    from agenthub.runner import service

    monkeypatch.setattr(service.shutil, "which", lambda name: "/Users/agent/.local/bin/claude")
    path = service.write_plist("/opt/agenthub/bin/agenthub", tmp_path / "cfg", directory=tmp_path)
    assert path.name == f"{service.LAUNCHD_LABEL}.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == service.LAUNCHD_LABEL
    assert data["ProgramArguments"] == ["/opt/agenthub/bin/agenthub", "runner"]
    env = data["EnvironmentVariables"]
    assert env["AGENTHUB_CONFIG_DIR"] == str((tmp_path / "cfg").resolve())
    # launchd's own PATH lacks both executables' directories
    assert env["PATH"].split(":")[:2] == ["/opt/agenthub/bin", "/Users/agent/.local/bin"]
    assert data["KeepAlive"] == {"SuccessfulExit": False} and data["RunAtLoad"] is True


def test_revoked_runner_exits_cleanly_only_under_launchd(monkeypatch):
    from agenthub.runner import service

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert service.revoked_exit_code() == service.EXIT_REVOKED
    monkeypatch.setenv("XPC_SERVICE_NAME", service.LAUNCHD_LABEL)
    assert service.revoked_exit_code() == 0
