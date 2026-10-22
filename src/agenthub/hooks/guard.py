"""Local classification of risky Bash commands.

This runs inside the PreToolUse hook on the agent machine and never consults
the hub: the hub can only *consume* an approval for a command this module
already flagged, never declare a command safe.

Matching is deliberately conservative. Patterns scan the raw command text
(including quoted strings, ``bash -c "..."``, and command substitutions), so
``echo "rm -rf x"`` is flagged too. A false positive costs one approval; a false
negative could cost a repo. Pattern matching is still a speed bump, not a
sandbox: the real protections are that agents hold no credentials for
protected branches and that branch protection is enabled on the remote.

Config: ``$AGENTHUB_CONFIG_DIR/guard.toml`` (see examples/guard.example.toml).
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agenthub import paths

# `git` plus any global options before the subcommand (-C dir, -c k=v, --git-dir=...).
_GIT = r"\bgit(?:\s+(?:-C\s+\S+|-c\s+\S+|--[\w-]+(?:=\S+)?))*\s+"
_ARGS = r"[^;&|\n]*"  # the rest of one simple command
_NOT_NAME = r"(?![\w./-])"  # end of a branch name ("main" but not "main-fix")


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: str
    description: str
    ignore_case: bool = False

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE if self.ignore_case else 0)


def _protected(branches: list[str]) -> str:
    return "(?:" + "|".join(re.escape(b) for b in branches) + ")"


def default_rules(protected: list[str]) -> list[Rule]:
    prot = _protected(protected)
    return [
        Rule("rm-recursive-or-force",
             r"(?<!git )(?<![\w-])rm\s+(?:" + _ARGS + r"\s)?(?:-[a-zA-Z]*[rRf][a-zA-Z]*|--recursive|--force)\b",
             "recursive or forced rm"),
        Rule("sql-drop-truncate",
             r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA|VIEW|INDEX|USER|ROLE)|TRUNCATE)\b",
             "SQL DROP or TRUNCATE", ignore_case=True),
        Rule("git-push-force",
             _GIT + r"push\b" + _ARGS
             + r"(?:\s--force(?:-with-lease|-if-includes)?\b|\s-[a-zA-Z]*f[a-zA-Z]*\b|\s\+\S|\s--mirror\b"
             + r"|\s--delete\b|\s-d\b|\s:\S)",
             "force push or remote branch deletion"),
        Rule("git-push-protected",
             _GIT + r"push\b" + _ARGS + r"[\s:/]" + prot + _NOT_NAME,
             f"push to {'/'.join(protected)}"),
        Rule("git-merge-into-protected",
             r"\b(?:checkout|switch)\s+" + prot + _NOT_NAME + r"[\s\S]*" + _GIT + r"merge(?![\w-])",
             f"merge into {'/'.join(protected)}"),
        Rule("gh-pr-merge", r"\bgh\s+pr\s+merge\b", "merging a pull request"),
        # The dashboard is the human operator's; an agent fetching it may be about to act as them (possible
        # from machines in the hub's operator_devices). Matches a tailnet name or address followed by /ui.
        Rule("hub-dashboard",
             r"(?:\.ts\.net|\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}|\bfd7a:115c:a1e0:[\w:]*\]?)(?::\d+)?/ui\b",
             "fetching the hub's operator dashboard (it is for humans)", ignore_case=True),
    ]


def default_never_rules(protected: list[str]) -> list[Rule]:
    """Commands that are refused outright and cannot be approved."""
    prot = _protected(protected)
    return [
        Rule("never-rm-root-or-home",
             r"(?<![\w-])rm\s+(?:-\S+\s+)*(?:/|~|\$HOME|\$\{HOME\})/?\*?(?:\s|$|[;&|])",
             "rm of / or the home directory"),
        Rule("never-force-push-protected",
             _GIT + r"push\b" + _ARGS
             + r"(?:\s--force\S*|\s-[a-zA-Z]*f[a-zA-Z]*\b|\s\+\S*)" + _ARGS + r"[\s:/+]" + prot + _NOT_NAME
             + r"|" + _GIT + r"push\b" + _ARGS + r"[\s:/+]" + prot + _NOT_NAME + _ARGS
             + r"(?:\s--force\S*|\s-[a-zA-Z]*f[a-zA-Z]*\b)"
             + r"|" + _GIT + r"push\b" + _ARGS + r"\s\+(?:\S*:)?(?:refs/heads/)?" + prot + _NOT_NAME,
             f"force push to {'/'.join(protected)}"),
    ]


def default_deny_rules(protected: list[str], hub_host: str | None = None) -> list[str]:
    """Claude Code permissions.deny entries mirroring the never-list.

    Syntax per code.claude.com/docs/en/permissions (fetched 2026-10-21): a
    trailing `` *`` also matches the bare command. Deny rules beat a hook's
    "allow", so only never-allowed commands belong here. With ``hub_host``,
    WebFetch of the hub is denied too: agents reach it only through the MCP
    server, and its web pages are the human operator's.
    """
    rules = [f"WebFetch(domain:{hub_host})"] if hub_host else []
    for target in ("/", "~", "~/", "$HOME"):
        for flags in ("-rf", "-fr", "-Rf", "-r -f"):
            rules.append(f"Bash(rm {flags} {target})")
            rules.append(f"Bash(rm {flags} {target} *)")
    for branch in protected:
        for force in ("--force", "-f", "--force-with-lease"):
            rules.append(f"Bash(git push {force} origin {branch} *)")
            rules.append(f"Bash(git push origin {branch} {force} *)")
        rules.append(f"Bash(git push origin +{branch} *)")
    return rules


@dataclass
class GuardConfig:
    protected_branches: list[str] = field(default_factory=lambda: ["main", "master"])
    shared_branches: list[str] = field(default_factory=lambda: ["main", "master", "develop", "release/*"])
    extra_rules: list[Rule] = field(default_factory=list)
    disabled_rules: list[str] = field(default_factory=list)
    stop_max_continuations: int = 3

    @property
    def rules(self) -> list[Rule]:
        rules = default_rules(self.protected_branches) + self.extra_rules
        return [r for r in rules if r.id not in self.disabled_rules]

    @property
    def never_rules(self) -> list[Rule]:
        return default_never_rules(self.protected_branches)


class GuardConfigError(Exception):
    pass


def guard_config_path() -> Path:
    return paths.config_dir() / "guard.toml"


def load_guard_config(path: Path | None = None) -> GuardConfig:
    """Defaults if the file is absent. A broken file raises (the hook then fails closed)."""
    path = path or guard_config_path()
    if not path.exists():
        return GuardConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        cfg = GuardConfig(
            protected_branches=list(data.pop("protected_branches", GuardConfig().protected_branches)),
            shared_branches=list(data.pop("shared_branches", GuardConfig().shared_branches)),
            disabled_rules=list(data.pop("disabled_rules", [])),
            stop_max_continuations=int(data.pop("stop_max_continuations", 3)),
            extra_rules=[Rule(**r) for r in data.pop("rules", [])],
        )
        if data:
            raise GuardConfigError(f"unknown keys: {', '.join(sorted(data))}")
        if not cfg.protected_branches:
            raise GuardConfigError("protected_branches must not be empty")
        for rule in cfg.rules:
            rule.compiled()
    except (OSError, ValueError, TypeError, re.error) as exc:
        raise GuardConfigError(f"invalid {path}: {exc}") from exc
    return cfg


# --- branch-dependent checks ---------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||&|\n")
_GIT_SUB = re.compile(_GIT + r"(push|merge|reset)(?![\w-])(.*)$", re.DOTALL)
_CHANGES_DIR = re.compile(r"(?:^|[;&|\s(])(?:cd|pushd)\s|\bgit\s+(?:\S+\s+)*?(?:-C\s|--git-dir|--work-tree)")


def current_branch(cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _positional(args: str) -> list[str]:
    return [a for a in args.split() if not a.startswith("-")]


@dataclass(frozen=True)
class Match:
    rule_id: str
    description: str


def _branch_matches(
    command: str, cwd: str, cfg: GuardConfig, branch_of: Callable[[str], str | None]
) -> list[Match]:
    out: list[Match] = []
    # If the command can change which repo/branch git operates on, we cannot
    # know the branch from cwd, so treat it as unknown (= risky).
    branch: str | None | bool = False  # False = not looked up yet
    for segment in _SEGMENT_SPLIT.split(command):
        m = _GIT_SUB.search(segment)
        if not m:
            continue
        sub, args = m.group(1), m.group(2)
        if branch is False:
            branch = None if _CHANGES_DIR.search(command) else branch_of(cwd)
        protected = branch is None or branch in cfg.protected_branches
        shared = branch is None or any(fnmatch.fnmatchcase(branch, p) for p in cfg.shared_branches)
        where = f"branch {branch}" if branch else "an unknown branch"
        positional = _positional(args)
        pushes_current = len(positional) <= 1 or "HEAD" in positional
        if sub == "push" and pushes_current and protected:
            out.append(Match("git-push-protected", f"push from {where} (protected)"))
        elif sub == "merge" and protected:
            out.append(Match("git-merge-into-protected", f"merge into {where} (protected)"))
        elif sub == "reset" and re.search(r"\s--hard\b", " " + args) and shared:
            out.append(Match("git-reset-hard-shared", f"git reset --hard on {where} (shared)"))
    return out


def classify(
    command: str,
    cwd: str,
    cfg: GuardConfig,
    *,
    branch_of: Callable[[str], str | None] = current_branch,
) -> tuple[list[Match], list[Match]]:
    """Return (never_matches, risky_matches) for a Bash command."""
    never = [Match(r.id, r.description) for r in cfg.never_rules if r.compiled().search(command)]
    risky = [Match(r.id, r.description) for r in cfg.rules if r.compiled().search(command)]
    seen = {m.rule_id for m in risky}
    risky += [m for m in _branch_matches(command, cwd, cfg, branch_of) if m.rule_id not in seen]
    return never, risky
