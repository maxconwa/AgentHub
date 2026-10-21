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
    path = install.install("user", exe="/bin/agenthub", config_dir=paths.config_dir())
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert checks["hook PreToolUse"].status == "ok" and checks["deny rules"].status == "ok"
    data = json.loads(path.read_text())
    del data["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"]
    path.write_text(json.dumps(data))
    checks = {c.name: c for c in doctor.check_hooks(paths.config_dir(), tmp_path)}
    assert checks["hook PreToolUse"].status == "fail" and "fail open" in checks["hook PreToolUse"].detail


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
