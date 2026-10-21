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
