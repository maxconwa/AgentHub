import json
import subprocess

from typer.testing import CliRunner

from agenthub import doctor, paths
from agenthub.cli import app
from agenthub.config import AgentConfig, HubConfig
from agenthub.hooks import install


def fake_run(outputs):
    def run(argv):
        for prefix, (code, out) in outputs.items():
            if " ".join(argv).startswith(prefix):
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 1, "", "unknown")
    return run


def status(state="Running", tags=None):
    return (0, json.dumps({"BackendState": state, "Self": {"Tags": tags} if tags else {}}))


def test_tailscale_checks():
    ok = doctor.check_tailscale(fake_run({"tailscale status": status(tags=["tag:agent"])}), "tag:agent")
    assert [c.status for c in ok] == ["ok", "ok"]
    untagged = doctor.check_tailscale(fake_run({"tailscale status": status()}), "tag:agent")
    assert untagged[1].status == "warn" and "operator" in untagged[1].detail
    down = doctor.check_tailscale(fake_run({"tailscale status": status("NeedsLogin")}), "tag:agent")
    assert down[0].status == "fail"


def test_private_file_check(tmp_path):
    f = tmp_path / "t"
    f.write_text("x")
    f.chmod(0o644)
    assert doctor.check_private(f, "token", 0o600).status == "fail"
    f.chmod(0o600)
    assert doctor.check_private(f, "token", 0o600).status == "ok"
    assert doctor.check_private(tmp_path / "missing", "token", 0o600).status == "fail"


def test_serve_targets():
    out = {"TCP": {"443": {"HTTPS": True}}, "Web": {"hub.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8700"}}}}}
    assert doctor.serve_targets(fake_run({"tailscale serve": (0, json.dumps(out))})) == ["http://127.0.0.1:8700"]
    assert doctor.serve_targets(fake_run({"tailscale serve": (0, "")})) == []


def test_hook_checks_detect_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert checks["hook PreToolUse"].status == "fail"
    path = install.install("user", exe="/bin/agenthub", fallback_dir=paths.config_dir())
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert checks["hook PreToolUse"].status == "ok" and checks["deny rules"].status == "ok"
    data = json.loads(path.read_text())
    del data["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"]
    path.write_text(json.dumps(data))
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert checks["hook PreToolUse"].status == "fail" and "fail open" in checks["hook PreToolUse"].detail


def test_hook_checks_warn_about_the_old_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    path = install.install("user", exe="/bin/agenthub", fallback_dir=paths.config_dir())
    data = json.loads(path.read_text())
    data["permissions"]["deny"].append("Bash(rm -rf ~)")
    path.write_text(json.dumps(data))
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / "guard.toml").write_text('protected_branches = ["main"]\n')
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert checks["deny rules"].status == "warn" and "hook install" in checks["deny rules"].detail
    assert checks["guard config"].status == "warn" and "protected_branches" in checks["guard config"].detail


def test_hub_checks(tmp_path):
    HubConfig(bind_host="0.0.0.0", db_path=str(tmp_path / "h.db"), port=8700).save()
    run = fake_run({"tailscale status": status(tags=["tag:agent-hub"]), "tailscale serve": (0, "{}")})
    checks = {c.name: c for c in doctor.hub_checks(run)}
    assert checks["bind address"].status == "fail" and checks["operators"].status == "warn"
    assert checks["tailscale serve"].status == "warn" and checks["tailscale tag"].status == "ok"


def test_ntfy_privacy_warning(tmp_path):
    HubConfig(db_path=str(tmp_path / "h.db"), operators=["op@example.com"], ntfy_url="https://ntfy.sh",
              ntfy_topic="t").save()
    checks = {c.name: c for c in doctor.hub_checks(fake_run({}))}
    assert checks["ntfy"].status == "warn" and "guesses" in checks["ntfy"].detail


async def test_agent_token_states(tmp_path):
    from .test_runner import LiveHub

    hub = LiveHub(tmp_path)
    try:
        agent_cfg, _ = hub.agent("doc", tmp_path)
        assert {c.name: c.status for c in await doctor._hub_checks(agent_cfg)} == {"hub reachable": "ok", "token": "ok"}
        from agenthub.hub import approvals, db

        conn = hub.conn()
        with db.transaction(conn):
            approvals.set_agent_status(conn, agent_cfg.agent_id, "revoked", actor="human:op")
        token = {c.name: c for c in await doctor._hub_checks(agent_cfg)}["token"]
        assert token.status == "fail" and "revoked" in token.detail
    finally:
        hub.stop()
    down = await doctor._hub_checks(AgentConfig(hub_url="http://127.0.0.1:9", agent_id="a", name="n", token_file="/x"))
    assert down[0].status == "fail"


def test_cli_exits_nonzero_on_failure(tmp_path, monkeypatch):
    HubConfig(bind_host="0.0.0.0", db_path=str(tmp_path / "h.db")).save()
    monkeypatch.setattr(doctor, "run_cmd", fake_run({}))
    monkeypatch.setattr(doctor.run_doctor, "__defaults__", (fake_run({}),))
    result = CliRunner().invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1 and json.loads(result.stdout)["hub"]


def test_cli_with_nothing_configured():
    assert CliRunner().invoke(app, ["doctor"]).exit_code == 1


def test_doctor_flags_api_billing(tmp_path, monkeypatch):
    from agenthub.runner.session import BILLING_ENV_VARS

    for name in BILLING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)

    def billing(cwd=project, projects=()):
        check = doctor.check_api_billing(cwd, projects)
        assert check.name == "API billing"
        return check

    assert billing().status == "ok"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    check = billing()
    assert check.status == "fail"
    assert check.detail == "ANTHROPIC_API_KEY would make Claude Code bill API credits instead of your subscription"
    # agent_checks reports it even before the agent config exists
    rows = {c.name: c for c in doctor.agent_checks(fake_run({}), cwd=project)}
    assert rows["API billing"].status == "fail"
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    (claude_dir / "settings.json").write_text(json.dumps({"apiKeyHelper": "/bin/get-key"}))
    check = billing()
    assert check.status == "fail" and check.detail.startswith("apiKeyHelper")
    assert "would make Claude Code bill API credits instead of your subscription" in check.detail
    (claude_dir / "settings.json").write_text(json.dumps({"model": "haiku"}))
    assert billing().status == "ok"

    # a settings file's env block reaches the session past the runner's scrub
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}))
    assert billing().status == "fail" and "CLAUDE_CODE_USE_BEDROCK" in billing().detail
    (project / ".claude" / "settings.local.json").unlink()

    # the runner's project checkouts are checked too, not only the current directory
    other = tmp_path / "other"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": "echo k"}))
    assert billing().status == "ok"
    assert billing(projects=[other]).status == "fail"


def test_doctor_warns_when_auto_mode_is_disabled(tmp_path, monkeypatch):
    from agenthub.runner.config import RunnerConfig

    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setattr(doctor, "MANAGED_SETTINGS", tmp_path / "no-managed.json")
    auto, plan = RunnerConfig(), RunnerConfig(permission_mode="plan")

    check = doctor.check_permission_mode(auto)  # no settings file: not an error
    assert (check.name, check.status) == ("permission mode", "ok") and "auto" in check.detail

    (claude_dir / "settings.json").write_text(json.dumps({"permissions": {"disableAutoMode": "disable"}}))
    check = doctor.check_permission_mode(auto)
    assert check.status == "warn" and "disableAutoMode" in check.detail and "Manual" in check.detail
    assert doctor.check_permission_mode(plan).status == "ok"  # only matters for auto

    (claude_dir / "settings.json").write_text("{not json")
    assert doctor.check_permission_mode(auto).status == "ok"

    managed = tmp_path / "managed.json"
    managed.write_text(json.dumps({"permissions": {"disableAutoMode": "disable"}}))
    monkeypatch.setattr(doctor, "MANAGED_SETTINGS", managed)
    check = doctor.check_permission_mode(auto)
    assert check.status == "warn" and str(managed) in check.detail


def test_journal_mode_and_litestream_checks(tmp_path, monkeypatch):
    from agenthub.hub import db

    db_path = tmp_path / "h.db"
    db.init_db(str(db_path)).close()
    db_path.chmod(0o600)
    HubConfig(db_path=str(db_path), operators=["op@example.com"]).save()
    monkeypatch.setattr(doctor, "LITESTREAM_CONFIG", tmp_path / "litestream.yml")
    checks = {c.name: c for c in doctor.hub_checks(fake_run({}))}
    assert checks["journal_mode"].status == "ok" and checks["journal_mode"].detail == "wal"
    assert checks["litestream"].status == "warn" and "docs/backup.md" in checks["litestream"].detail
    (tmp_path / "litestream.yml").write_text("dbs: []\n")
    assert {c.name: c for c in doctor.hub_checks(fake_run({}))}["litestream"].status == "ok"
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=delete")
    conn.close()
    bad = {c.name: c for c in doctor.hub_checks(fake_run({}))}["journal_mode"]
    assert bad.status == "fail" and "delete" in bad.detail


def test_launchd_runner_check():
    from agenthub.runner import service

    target = f"launchctl print {service.launchd_domain()}/{service.LAUNCHD_LABEL}"
    running = doctor.check_launchd(fake_run({target: (0, "com.agenthub.runner = {\n\tstate = running\n}")}))
    assert running.status == "ok" and running.detail == "running"
    waiting = doctor.check_launchd(fake_run({target: (0, "\tstate = not running\n")}))
    assert waiting.status == "warn" and "runner.log" in waiting.detail
    missing = doctor.check_launchd(fake_run({}))
    assert missing.status == "warn" and "not loaded" in missing.detail


def test_tailscale_bin_finds_the_macos_app(tmp_path, monkeypatch):
    app_cli = tmp_path / "Tailscale"
    app_cli.write_text("")
    monkeypatch.setattr(doctor, "MACOS_TAILSCALE", app_cli)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    assert doctor.tailscale_bin() == str(app_cli)
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    assert doctor.tailscale_bin() == "tailscale"


def test_hooks_pinned_to_one_agent_are_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    path = install.install("user", exe="/bin/agenthub", fallback_dir=paths.config_dir())
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert "hook identity" not in checks
    data = json.loads(path.read_text())
    for entries in data["hooks"].values():  # the 0.2.1 form: AGENTHUB_CONFIG_DIR=<dir> agenthub hook ...
        for e in entries:
            e["hooks"][0]["command"] = f"AGENTHUB_CONFIG_DIR={paths.config_dir()} /bin/agenthub hook x"
    path.write_text(json.dumps(data))
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert checks["hook identity"].status == "warn" and checks["hook PreToolUse"].status == "ok"
