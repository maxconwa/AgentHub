"""``agenthub join``: enroll this machine's agent with the hub.

MCP registration follows the Claude Code MCP docs (code.claude.com/docs/en/mcp,
fetched 2026-10-21, Claude Code 2.1.306): ``claude mcp add-json <name> '<json>'
--scope local|project|user``. There is no documented upsert, so we remove any
existing entry first. Local scope is keyed by the current project directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from agenthub import paths
from agenthub.client import HubClient
from agenthub.config import AgentConfig, write_private

MCP_SERVER_NAME = "agenthub"
# ask_agent / wait_for_approval block for minutes; give the server a generous per-call timeout.
MCP_TOOL_TIMEOUT_MS = 15 * 60 * 1000


class JoinError(Exception):
    pass


def check_hub_url(url: str, *, insecure_dev: bool) -> str:
    if url.startswith("https://"):
        return url.rstrip("/")
    if url.startswith("http://") and insecure_dev:
        return url.rstrip("/")
    raise JoinError("hub URL must be https:// (tailscale serve); http:// is only allowed with --insecure-dev")


def agenthub_command() -> str:
    """Absolute path of the agenthub executable, for Claude Code to launch."""
    found = shutil.which("agenthub")
    if found:
        return str(Path(found).resolve())
    argv0 = Path(sys.argv[0])
    if argv0.name == "agenthub" and argv0.exists():
        return str(argv0.resolve())
    raise JoinError("cannot locate the agenthub executable on PATH")


def fallback_config_dir() -> Path:
    """The agent that Claude Code sessions not started by a runner act as: the agent in the default config
    dir if there is one, else the one being set up. Runner sessions carry their own $AGENTHUB_CONFIG_DIR."""
    default = paths.default_config_dir()
    return default if (default / "agent.toml").exists() else paths.config_dir()


def mcp_server_spec(command: str, fallback_dir: Path) -> dict:
    # The token is NOT placed in Claude Code's config; the MCP server reads it
    # from the 0600 token file under AGENTHUB_CONFIG_DIR. Claude Code expands
    # ${VAR:-default} in a server's env (checked against Claude Code 2.1.308), so
    # one registration serves every agent on the account (see hooks.install).
    from agenthub.hooks.install import env_default

    return {
        "type": "stdio",
        "command": command,
        "args": ["mcp"],
        "env": {"AGENTHUB_CONFIG_DIR": env_default(fallback_dir)},
        "timeout": MCP_TOOL_TIMEOUT_MS,
    }


def register_mcp(spec: dict, *, scope: str, name: str = MCP_SERVER_NAME, cwd: Path | None = None) -> None:
    claude = shutil.which("claude")
    if not claude:
        raise JoinError("`claude` not found on PATH; install Claude Code or rerun with --no-mcp")
    subprocess.run([claude, "mcp", "remove", name, "--scope", scope], cwd=cwd, capture_output=True)
    proc = subprocess.run(
        [claude, "mcp", "add-json", name, json.dumps(spec), "--scope", scope],
        cwd=cwd, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise JoinError(f"claude mcp add-json failed: {proc.stderr.strip() or proc.stdout.strip()}")


async def enroll(
    hub_url: str,
    code: str,
    name: str,
    *,
    insecure_dev: bool = False,
    force: bool = False,
    transport=None,
) -> tuple[AgentConfig, Path, str]:
    hub_url = check_hub_url(hub_url, insecure_dev=insecure_dev)
    config_path = paths.agent_config_path()
    if config_path.exists() and not force:
        raise JoinError(f"{config_path} already exists: an agent on this account has already joined. To add "
                        f"another agent, give it its own config dir: agenthub --config-dir "
                        f"~/.config/agenthub-{name} join ... (or use --force to replace this one)")
    async with HubClient(hub_url, transport=transport) as client:
        resp = await client.enroll(code, name)
    token_path = paths.config_dir() / "token"
    write_private(token_path, resp["token"] + "\n")
    agent = resp["agent"]
    cfg = AgentConfig(hub_url=hub_url, agent_id=agent["id"], name=agent["name"], token_file=str(token_path))
    cfg.save(config_path)
    return cfg, config_path, agent["status"]
