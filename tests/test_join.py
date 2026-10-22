import asyncio
import json
import subprocess

import httpx
import pytest

from agenthub import join, paths
from agenthub.config import AgentConfig


def mode(path) -> int:
    return path.stat().st_mode & 0o777


async def test_enroll_stores_private_token_and_config(dev_hub):
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    cfg, cfg_path, status = await join.enroll(
        "http://hub", dev_hub.code(), "worker", insecure_dev=True, transport=transport
    )
    assert status == "pending"
    token_path = paths.config_dir() / "token"
    assert mode(token_path) == 0o600 and mode(cfg_path) == 0o600 and mode(paths.config_dir()) == 0o700
    assert token_path.read_text().startswith("ahk_")
    assert "ahk_" not in cfg_path.read_text()  # token lives only in the token file
    assert AgentConfig.load().read_token() == token_path.read_text().strip()


async def test_enroll_refuses_to_overwrite_existing_agent(dev_hub):
    transport = httpx.ASGITransport(app=dev_hub.client.app)
    await join.enroll("http://hub", dev_hub.code(), "one", insecure_dev=True, transport=transport)
    with pytest.raises(join.JoinError, match="already joined"):
        await join.enroll("http://hub", dev_hub.code(), "two", insecure_dev=True, transport=transport)


def test_https_required_outside_dev():
    with pytest.raises(join.JoinError):
        join.check_hub_url("http://hub.example.ts.net", insecure_dev=False)
    assert join.check_hub_url("https://hub.example.ts.net/", insecure_dev=False) == "https://hub.example.ts.net"


def test_read_token_refuses_loose_permissions(tmp_path):
    token = tmp_path / "token"
    token.write_text("ahk_a_b")
    token.chmod(0o644)
    cfg = AgentConfig(hub_url="https://h", agent_id="a", name="n", token_file=str(token))
    with pytest.raises(PermissionError):
        cfg.read_token()


def test_mcp_spec_has_no_secret(tmp_path):
    spec = join.mcp_server_spec("/usr/bin/agenthub", tmp_path)
    assert spec["args"] == ["mcp"] and spec["type"] == "stdio"
    assert spec["env"] == {"AGENTHUB_CONFIG_DIR": f"${{AGENTHUB_CONFIG_DIR:-{tmp_path.resolve()}}}"}
    assert "ahk_" not in json.dumps(spec)


def test_register_mcp_removes_then_adds(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(join.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(join.subprocess, "run", fake_run)
    join.register_mcp({"type": "stdio", "command": "x", "args": ["mcp"]}, scope="local")
    assert calls[0] == ["/bin/claude", "mcp", "remove", "agenthub", "--scope", "local"]
    assert calls[1][:4] == ["/bin/claude", "mcp", "add-json", "agenthub"]
    assert calls[1][-2:] == ["--scope", "local"]


def test_fallback_agent_is_the_default_config_dirs(tmp_path):
    # conftest points AGENTHUB_CONFIG_DIR and XDG_CONFIG_HOME at separate temporary dirs
    assert join.fallback_config_dir() == paths.config_dir()
    default = paths.default_config_dir()
    default.mkdir(parents=True)
    (default / "agent.toml").write_text("")
    assert join.fallback_config_dir() == default


def test_second_agent_is_pointed_at_its_own_config_dir():
    paths.config_dir().mkdir(parents=True)
    paths.agent_config_path().write_text("")
    with pytest.raises(join.JoinError, match="--config-dir ~/.config/agenthub-tester"):
        asyncio.run(join.enroll("https://hub.example", "CODE", "tester"))
