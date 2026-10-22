from typer.testing import CliRunner

from agenthub import paths
from agenthub.cli import app


def test_hub_init_writes_private_config_and_unit_with_overrides(tmp_path):
    unit_dir = tmp_path / "units"
    result = CliRunner().invoke(app, ["hub", "init", "--operator", "op@example.com", "--systemd-dir", str(unit_dir)])
    assert result.exit_code == 0, result.output
    cfg = paths.hub_config_path()
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert 'operators = ["op@example.com"]' in cfg.read_text()
    unit = (unit_dir / "agenthub-hub.service").read_text()
    assert f"Environment=AGENTHUB_CONFIG_DIR={paths.config_dir()}" in unit
    assert "hub serve" in unit and "--insecure-dev" not in unit
    again = CliRunner().invoke(app, ["hub", "init", "--no-systemd"])
    assert again.exit_code == 1 and "already exists" in again.output


def test_enroll_and_approve_via_cli(tmp_path):
    runner = CliRunner()
    runner.invoke(app, ["hub", "init", "--no-systemd"])
    result = runner.invoke(app, ["hub", "enroll", "--projects", "alpha,beta"])
    assert result.exit_code == 0
    code = result.stdout.strip().splitlines()[0]
    assert len(code) == 14
    assert runner.invoke(app, ["hub", "project", "list"]).stdout.split()[::1][:1] == ["alpha"]
    assert runner.invoke(app, ["hub", "agents", "approve", "ghost"]).exit_code == 1


def test_cli_add_and_remove_agent(tmp_path):
    from agenthub.cli import _hub_conn
    from agenthub.hub import db, store, tasks
    from agenthub.hub.store import Device

    runner = CliRunner()
    runner.invoke(app, ["hub", "init", "--no-systemd"])
    code = runner.invoke(app, ["hub", "enroll", "--projects", "alpha"]).stdout.strip().splitlines()[0]
    conn = _hub_conn()
    with db.transaction(conn):
        agent, _ = store.redeem_enrollment(conn, code=code, name="rev", device=Device("n1", "host", "100.64.0.9"))
        conn.execute("UPDATE agents SET status = 'active' WHERE id = ?", (agent.id,))
    created = runner.invoke(app, ["hub", "tasks", "create", "alpha", "T"])
    assert created.exit_code == 0, created.output
    r = runner.invoke(app, ["hub", "tasks", "add-agent", "1", "rev", "--role", "Reviewer: check x",
                            "--stage", "reviews"])
    assert r.exit_code == 0, r.output
    got = tasks.get_task(conn, 1)
    assert [(m["name"], m["role"], m["stage"]) for m in got["agents"]] == [("rev", "Reviewer: check x", "reviews")]
    assert "rev(Reviewer: check x)" in runner.invoke(app, ["hub", "tasks", "list"]).output
    bad = runner.invoke(app, ["hub", "tasks", "add-agent", "1", "rev", "--role", "r", "--stage", "bogus"])
    assert bad.exit_code != 0
    assert runner.invoke(app, ["hub", "tasks", "remove-agent", "1", "rev"]).exit_code == 0
    assert tasks.get_task(conn, 1)["agents"] == []
    assert runner.invoke(app, ["hub", "tasks", "remove-agent", "1", "rev"]).exit_code == 1
    c2 = runner.invoke(app, ["hub", "tasks", "create", "alpha", "U", "--assignee", "rev", "--role", "Builder"])
    assert c2.exit_code == 0, c2.output
    assert tasks.get_task(conn, 2)["agents"][0]["role"] == "Builder"


def test_hub_config_round_trips_operator_devices(tmp_path):
    import pytest

    from agenthub.config import HubConfig

    cfg = HubConfig(operators=["op@example.com"], operator_devices={"nABC123": "op@example.com"})
    path = cfg.save(tmp_path / "hub.toml")
    assert HubConfig.load(path).operator_devices == {"nABC123": "op@example.com"}
    HubConfig(operators=["op@example.com"], operator_devices={"nX": "other@example.com"}).save(path)
    with pytest.raises(ValueError, match="not in operators"):
        HubConfig.load(path)
