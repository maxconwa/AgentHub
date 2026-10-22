"""`agenthub doctor`: check this machine's agenthub setup.

Runs agent checks if this machine has joined a hub (agent.toml), hub checks if
it runs a hub (hub.toml), or both. Each check reports ok / warn / fail; the
command exits non-zero if anything failed.

Tailscale output fields per tailscale v1.102.2 (ipn/ipnstate, ipn/serve.go):
``tailscale status --json`` has ``BackendState`` ("Running", "NeedsLogin", ...)
and ``Self.Tags``; ``tailscale serve status --json`` prints the ServeConfig,
whose ``Web[host:port].Handlers[path].Proxy`` holds the proxy target.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenthub import paths

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def run_cmd(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def check_private(path: Path, name: str, want: int) -> Check:
    if not path.exists():
        return Check(name, FAIL, f"{path} is missing")
    mode = _mode(path)
    if mode & 0o077:
        return Check(name, FAIL, f"{path} is mode {mode:o}; run: chmod {want:o} {path}")
    return Check(name, OK, f"{path} ({mode:o})")


def tailscale_status(run: Runner, tailscale: str = "tailscale") -> dict[str, Any] | None:
    proc = run([tailscale, "status", "--json"])
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def check_tailscale(run: Runner, want_tag: str, tailscale: str = "tailscale") -> list[Check]:
    st = tailscale_status(run, tailscale)
    if st is None:
        return [Check("tailscale", FAIL, "`tailscale status` failed; is tailscaled running?")]
    state = st.get("BackendState")
    if state != "Running":
        return [Check("tailscale", FAIL, f"backend state is {state}; run `tailscale up`")]
    tags = (st.get("Self") or {}).get("Tags") or []
    out = [Check("tailscale", OK, "connected")]
    if want_tag in tags:
        out.append(Check("tailscale tag", OK, want_tag))
    else:
        why = ("an untagged agent machine carries your user identity and could act as the operator"
               if want_tag == "tag:agent" else "the example ACL policy grants access to this tag")
        out.append(Check("tailscale tag", WARN, f"this device is not tagged {want_tag}: {why}"))
    return out


# --- agent side ----------------------------------------------------------------------

def _settings_files(cwd: Path) -> list[Path]:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return [base / "settings.json", cwd / ".claude" / "settings.json", cwd / ".claude" / "settings.local.json"]


def check_api_billing(cwd: Path, project_dirs: list[Path] | tuple[Path, ...] = ()) -> Check:
    """Fail if anything would make Claude Code bill API credits instead of the operator's subscription.

    That is a billing variable in this environment (interactive ``claude`` would use it; the runner removes
    them from its sessions), or ``apiKeyHelper`` or a billing variable in an ``env`` block of a Claude Code
    settings file for this directory or a runner project checkout (those reach a session past the runner).
    """
    from agenthub.runner.session import BILLING_ENV_VARS

    found = [name for name in BILLING_ENV_VARS if name in os.environ]
    seen: set[Path] = set()
    for base in (cwd, *project_dirs):
        for path in _settings_files(Path(base).expanduser()):
            if path in seen:
                continue
            seen.add(path)
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if "apiKeyHelper" in data:
                found.append(f"apiKeyHelper in {path}")
            env = data.get("env") if isinstance(data.get("env"), dict) else {}
            found += [f"{name} in {path}" for name in BILLING_ENV_VARS if name in env]
    if found:
        return Check("API billing", FAIL,
                     f"{', '.join(found)} would make Claude Code bill API credits instead of your subscription")
    return Check("API billing", OK, "no API key, gateway or cloud provider settings found")


def check_hooks(config_dir: Path, cwd: Path) -> list[Check]:
    from agenthub.hooks import guard

    marker = f"AGENTHUB_CONFIG_DIR={config_dir.resolve()}"
    found: dict[str, dict] = {}
    deny: set[str] = set()
    for path in _settings_files(cwd):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        deny.update((data.get("permissions") or {}).get("deny") or [])
        for event, entries in (data.get("hooks") or {}).items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    cmd = hook.get("command", "")
                    if marker in cmd and " hook " in cmd:
                        found[event] = hook
    out = []
    pre = found.get("PreToolUse")
    if pre is None:
        out.append(Check("hook PreToolUse", FAIL, "not installed; run `agenthub hook install`"))
    elif pre.get("onFailure") != "block":
        out.append(Check("hook PreToolUse", FAIL, "installed without onFailure=block (would fail open); reinstall"))
    else:
        out.append(Check("hook PreToolUse", OK, "installed, fails closed"))
    for event in ("Stop", "PermissionRequest"):
        out.append(Check(f"hook {event}", OK if event in found else WARN,
                         "installed" if event in found else "not installed; run `agenthub hook install`"))
    try:
        cfg = guard.load_guard_config()
        out.append(Check("guard config", OK, f"{len(cfg.rules)} rules"))
        missing = [r for r in guard.default_deny_rules(cfg.protected_branches) if r not in deny]
        out.append(Check("deny rules", OK if not missing else WARN,
                         "never-allow list installed" if not missing else f"{len(missing)} missing; reinstall hooks"))
    except guard.GuardConfigError as exc:
        out.append(Check("guard config", FAIL, f"{exc} (all Bash commands are blocked)"))
    return out


async def _hub_checks(cfg) -> list[Check]:
    import httpx

    from agenthub.client import HubClient, HubError

    out = []
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(cfg.hub_url.rstrip("/") + "/healthz")
        resp.raise_for_status()
        out.append(Check("hub reachable", OK, f"{cfg.hub_url} (version {resp.json().get('version')})"))
    except (httpx.HTTPError, ValueError) as exc:
        return [Check("hub reachable", FAIL, f"{cfg.hub_url}: {exc.__class__.__name__}")]
    try:
        async with HubClient.from_config(cfg, timeout=10) as client:
            me = await client.me()
    except PermissionError as exc:
        return out + [Check("token", FAIL, str(exc))]
    except HubError as exc:
        hint = {401: "token rejected (revoked or wrong); re-enroll with `agenthub join`",
                403: f"refused: {exc.detail}"}.get(exc.status, str(exc.detail))
        return out + [Check("token", FAIL, hint)]
    if me["status"] == "active":
        out.append(Check("token", OK, f"agent {me['name']} is active; projects: {', '.join(me['allowed_projects'])}"))
    else:
        out.append(Check("token", WARN, f"agent {me['name']} is {me['status']}; ask the operator to approve it"))
    return out


def agent_checks(run: Runner = run_cmd, cwd: Path | None = None) -> list[Check]:
    from agenthub.config import AgentConfig
    from agenthub.runner.config import RunnerConfigError, load_runner_config
    from agenthub.runner.service import UNIT_NAME

    cwd = cwd or Path.cwd()
    config_dir = paths.config_dir()
    out: list[Check] = []
    if os.geteuid() == 0:
        out.append(Check("user", FAIL, "running as root; run agents as a dedicated non-root user"))
    out += check_tailscale(run, "tag:agent")
    try:
        project_dirs = [Path(d) for d in load_runner_config().projects.values()]
    except RunnerConfigError:
        project_dirs = []  # an invalid runner.toml is the "runner config" check's to report
    out.append(check_api_billing(cwd, project_dirs))
    out.append(check_private(config_dir, "config dir", 0o700))
    out.append(check_private(paths.agent_config_path(), "agent.toml", 0o600))
    try:
        cfg = AgentConfig.load()
    except (OSError, ValueError) as exc:
        return out + [Check("agent config", FAIL, str(exc))]
    out.append(check_private(Path(cfg.token_file), "token file", 0o600))
    out += asyncio.run(_hub_checks(cfg))
    out += check_hooks(config_dir, cwd)

    claude = shutil.which("claude")
    if not claude:
        out.append(Check("claude", FAIL, "`claude` not on PATH"))
    else:
        proc = run([claude, "mcp", "get", "agenthub"])
        out.append(Check("MCP server", OK if proc.returncode == 0 else WARN,
                         "registered" if proc.returncode == 0 else "not registered here; rerun `agenthub join` "
                         "or `claude mcp add-json` (local-scope entries only show in their project directory)"))
    try:
        rcfg = load_runner_config()
        missing = [n for n, d in rcfg.projects.items() if not Path(d).expanduser().is_dir()]
        if not rcfg.projects:
            out.append(Check("runner config", WARN, "no [projects] mapped; the runner cannot run tasks"))
        elif missing:
            out.append(Check("runner config", WARN, f"missing directories for: {', '.join(missing)}"))
        else:
            out.append(Check("runner config", OK, f"{len(rcfg.projects)} project(s) mapped"))
    except RunnerConfigError as exc:
        out.append(Check("runner config", FAIL, str(exc)))
    if shutil.which("systemctl"):
        active = run(["systemctl", "--user", "is-active", UNIT_NAME]).stdout.strip()
        out.append(Check("runner service", OK if active == "active" else WARN, active or "unknown"))
        linger = run(["loginctl", "show-user", getpass.getuser(), "--property=Linger"]).stdout.strip()
        out.append(Check("linger", OK if linger == "Linger=yes" else WARN,
                         "enabled" if linger == "Linger=yes" else "off: the runner stops when you log out; "
                         "run `sudo loginctl enable-linger $USER`"))
    return out


# --- hub side --------------------------------------------------------------------------

def serve_targets(run: Runner, tailscale: str = "tailscale") -> list[str]:
    proc = run([tailscale, "serve", "status", "--json"])
    try:
        data = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else {}
    except ValueError:
        return []
    return [h.get("Proxy") for web in (data.get("Web") or {}).values()
            for h in (web.get("Handlers") or {}).values() if h.get("Proxy")]


def hub_checks(run: Runner = run_cmd) -> list[Check]:
    import ipaddress

    from agenthub.config import HubConfig
    from agenthub.hub import db

    out: list[Check] = []
    try:
        cfg = HubConfig.load()
    except (OSError, ValueError) as exc:
        return [Check("hub config", FAIL, str(exc))]
    out.append(check_private(paths.hub_config_path(), "hub.toml", 0o600))
    try:
        loopback = ipaddress.ip_address(cfg.bind_host).is_loopback
    except ValueError:
        loopback = False
    out.append(Check("bind address", OK if loopback else FAIL,
                     f"{cfg.bind_host}:{cfg.port}" + ("" if loopback else " is not loopback; the hub will refuse")))
    out.append(Check("operators", OK if cfg.operators else WARN,
                     ", ".join(cfg.operators) or "none: nobody can use the dashboard"))
    db_path = Path(cfg.db_path)
    if db_path.exists():
        out.append(check_private(db_path, "database", 0o600))
        conn = sqlite3.connect(db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        current = len(db.MIGRATIONS)
        out.append(Check("schema", OK if version == current else WARN,
                         f"version {version}" + ("" if version == current else f" (hub will migrate to {current})")))
        out.append(Check("WAL mode", OK if journal == "wal" else WARN, journal))
    else:
        out.append(Check("database", WARN, f"{db_path} not created yet (run `agenthub hub init` or start the hub)"))
    csrf = paths.config_dir() / "csrf.key"
    if csrf.exists():
        out.append(check_private(csrf, "csrf key", 0o600))
    out += check_tailscale(run, "tag:agent-hub", cfg.tailscale_bin)
    want = f"http://127.0.0.1:{cfg.port}"
    targets = serve_targets(run, cfg.tailscale_bin)
    out.append(Check("tailscale serve", OK if want in targets else WARN,
                     f"proxies to {want}" if want in targets else
                     f"not proxying to {want}; run `tailscale serve --bg --https=443 {want}`"))
    out.append(Check("public_url", OK if cfg.public_url else WARN, cfg.public_url or "unset: notification links won't work"))
    if cfg.ntfy_url:
        public = "ntfy.sh" in cfg.ntfy_url and not cfg.ntfy_token_file
        out.append(Check("ntfy", WARN if public or cfg.ntfy_include_details else OK,
                         "public ntfy.sh topic without a token: anyone who guesses it can read notifications"
                         if public else ("details included in notifications" if cfg.ntfy_include_details
                                         else f"topic {cfg.ntfy_topic}")))
    return out


def run_doctor(run: Runner = run_cmd) -> tuple[list[tuple[str, list[Check]]], bool]:
    sections = []
    if paths.agent_config_path().exists():
        sections.append(("agent", agent_checks(run)))
    if paths.hub_config_path().exists():
        sections.append(("hub", hub_checks(run)))
    ok = all(c.status != FAIL for _, checks in sections for c in checks)
    return sections, ok
