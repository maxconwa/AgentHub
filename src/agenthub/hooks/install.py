"""Install agenthub's hooks and never-list deny rules into Claude Code settings.

Settings layout per code.claude.com/docs/en/hooks and /settings (fetched
2026-10-21, Claude Code 2.1.306):
  user:    $CLAUDE_CONFIG_DIR/settings.json (default ~/.claude/settings.json)
  project: <dir>/.claude/settings.json
  local:   <dir>/.claude/settings.local.json
Hook entries and permission arrays from all scopes are merged by Claude Code.
``onFailure: "block"`` (v2.1.295+) makes a crashed or timed-out PreToolUse
hook block the tool call instead of letting it through.

Install is idempotent: existing agenthub entries are replaced, everything
else in the file is preserved, and a one-time backup is written first.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from agenthub.hooks import guard

HOOK_TIMEOUT_SECONDS = 30
# The PermissionRequest hook may wait for a human; give it room beyond its own wait.
PERMISSION_HOOK_TIMEOUT_SECONDS = 660
MARKER = " hook pretooluse", " hook stop", " hook permission"
EVENTS = ("PreToolUse", "Stop", "PermissionRequest")


def settings_path(scope: str, project_dir: Path | None = None) -> Path:
    if scope == "user":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return base / "settings.json"
    project_dir = project_dir or Path.cwd()
    if scope == "project":
        return project_dir / ".claude" / "settings.json"
    if scope == "local":
        return project_dir / ".claude" / "settings.local.json"
    raise ValueError(f"unknown scope {scope!r}")


def hook_command(exe: str, which: str, config_dir: Path) -> str:
    return f"AGENTHUB_CONFIG_DIR={shlex.quote(str(config_dir.resolve()))} {shlex.quote(exe)} hook {which}"


def _is_ours(entry: dict[str, Any]) -> bool:
    return any(
        isinstance(h, dict) and isinstance(h.get("command"), str) and h["command"].endswith(MARKER)
        for h in entry.get("hooks", [])
    )


def merge_settings(settings: dict[str, Any], *, exe: str, config_dir: Path, deny_rules: list[str]) -> dict[str, Any]:
    settings = json.loads(json.dumps(settings))  # deep copy
    hooks = settings.setdefault("hooks", {})
    pre = [e for e in hooks.get("PreToolUse", []) if not _is_ours(e)]
    pre.append({
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": hook_command(exe, "pretooluse", config_dir),
            "timeout": HOOK_TIMEOUT_SECONDS,
            "onFailure": "block",
        }],
    })
    hooks["PreToolUse"] = pre
    stop = [e for e in hooks.get("Stop", []) if not _is_ours(e)]
    stop.append({"hooks": [{"type": "command", "command": hook_command(exe, "stop", config_dir),
                            "timeout": HOOK_TIMEOUT_SECONDS}]})
    hooks["Stop"] = stop
    perm = [e for e in hooks.get("PermissionRequest", []) if not _is_ours(e)]
    perm.append({"hooks": [{"type": "command", "command": hook_command(exe, "permission", config_dir),
                            "timeout": PERMISSION_HOOK_TIMEOUT_SECONDS}]})
    hooks["PermissionRequest"] = perm
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    deny.extend(r for r in deny_rules if r not in deny)
    return settings


def remove_from_settings(settings: dict[str, Any], *, deny_rules: list[str]) -> dict[str, Any]:
    settings = json.loads(json.dumps(settings))
    hooks = settings.get("hooks", {})
    for event in EVENTS:
        if event in hooks:
            hooks[event] = [e for e in hooks[event] if not _is_ours(e)]
            if not hooks[event]:
                del hooks[event]
    if "deny" in settings.get("permissions", {}):
        settings["permissions"]["deny"] = [r for r in settings["permissions"]["deny"] if r not in deny_rules]
    return settings


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".agenthub-backup")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def install(scope: str, *, exe: str, config_dir: Path, project_dir: Path | None = None) -> Path:
    path = settings_path(scope, project_dir)
    cfg = guard.load_guard_config()
    merged = merge_settings(_load(path), exe=exe, config_dir=config_dir,
                            deny_rules=guard.default_deny_rules(cfg.protected_branches))
    _write(path, merged)
    return path


def uninstall(scope: str, *, project_dir: Path | None = None) -> Path:
    path = settings_path(scope, project_dir)
    cfg = guard.load_guard_config()
    _write(path, remove_from_settings(_load(path), deny_rules=guard.default_deny_rules(cfg.protected_branches)))
    return path
