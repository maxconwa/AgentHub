"""Local classification of Bash commands that are never allowed.

This runs inside the PreToolUse hook on the agent machine and never consults
the hub. Since 0.3.6 it blocks only one thing, an agent fetching the hub's
operator dashboard: approvals, questions and prompts there are decided by a
human, and an agent on a device allowed to operate the hub could otherwise
decide them itself. Everything else is left to Claude Code's permission mode
(``auto`` in runner sessions) and the PermissionRequest hook.

Patterns scan the raw command text (including quoted strings, ``bash -c
"..."``, and command substitutions). Pattern matching is a speed bump, not a
sandbox.

Config: ``$AGENTHUB_CONFIG_DIR/guard.toml`` (see examples/guard.example.toml).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agenthub import paths


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: str
    description: str
    ignore_case: bool = False

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE if self.ignore_case else 0)


NEVER_RULES = [
    # The dashboard is the human operator's; an agent fetching it may be about to act as them (possible
    # from machines in the hub's operator_devices). Matches a tailnet name or address followed by /ui.
    Rule("hub-dashboard",
         r"(?:\.ts\.net|\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}|\bfd7a:115c:a1e0:[\w:]*\]?)(?::\d+)?/ui\b",
         "fetching the hub's operator dashboard (it is for humans)", ignore_case=True),
]


def default_deny_rules(hub_host: str | None = None) -> list[str]:
    """Claude Code permissions.deny entries: WebFetch of the hub, which agents reach only through the MCP
    server (its web pages are the human operator's)."""
    return [f"WebFetch(domain:{hub_host})"] if hub_host else []


# The deny rules agenthub up to 0.3.5 installed for its never-list; `hook install` removes them.
_OLD_RM = re.compile(r"Bash\(rm (?:-rf|-fr|-Rf|-r -f) (?:/|~|~/|\$HOME)(?: \*)?\)")
_OLD_PUSH = re.compile(r"Bash\(git push (?:(?:--force|-f|--force-with-lease) origin \S+|origin \S+ "
                       r"(?:--force|-f|--force-with-lease)|origin \+\S+) \*\)")


def is_old_deny_rule(rule: str) -> bool:
    return bool(_OLD_RM.fullmatch(rule) or _OLD_PUSH.fullmatch(rule))


# guard.toml keys of the command rules removed in 0.3.6; still accepted so older files keep working.
OLD_KEYS = ("protected_branches", "shared_branches", "disabled_rules", "rules")


@dataclass
class GuardConfig:
    stop_max_continuations: int = 3
    ignored_keys: list[str] = field(default_factory=list)


class GuardConfigError(Exception):
    pass


def guard_config_path() -> Path:
    return paths.config_dir() / "guard.toml"


def load_guard_config(path: Path | None = None) -> GuardConfig:
    """Defaults if the file is absent. A broken file raises."""
    path = path or guard_config_path()
    if not path.exists():
        return GuardConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        ignored = sorted(k for k in OLD_KEYS if data.pop(k, None) is not None)
        cfg = GuardConfig(stop_max_continuations=int(data.pop("stop_max_continuations", 3)), ignored_keys=ignored)
        if data:
            raise GuardConfigError(f"unknown keys: {', '.join(sorted(data))}")
    except (OSError, ValueError, TypeError) as exc:
        raise GuardConfigError(f"invalid {path}: {exc}") from exc
    return cfg


@dataclass(frozen=True)
class Match:
    rule_id: str
    description: str


def classify(command: str) -> list[Match]:
    """The never-allowed rules a Bash command matches."""
    return [Match(r.id, r.description) for r in NEVER_RULES if r.compiled().search(command)]
