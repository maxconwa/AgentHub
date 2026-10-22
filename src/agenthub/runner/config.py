"""Runner configuration: ``$AGENTHUB_CONFIG_DIR/runner.toml`` (see examples/runner.example.toml)."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agenthub import paths


class RunnerConfigError(Exception):
    pass


# `bypassPermissions` is deliberately absent: agenthub never runs sessions with permission checks off.
PERMISSION_MODES = ("acceptEdits", "auto", "manual", "dontAsk", "plan")

# extra_args may not set or loosen the permission mode; use `permission_mode` instead.
FORBIDDEN_EXTRA_ARGS = ("--permission-mode", "--dangerously-skip-permissions",
                        "--allow-dangerously-skip-permissions", "--permission-prompt-tool")


@dataclass
class RunnerConfig:
    # project name -> local checkout where that project's tasks run
    projects: dict[str, str] = field(default_factory=dict)
    concurrency: int = 1
    claude_bin: str = "claude"
    # Passed as `--permission-mode` to every session (`claude -p` would start in Manual otherwise).
    permission_mode: str = "auto"
    # Extra arguments for every `claude -p` session. Sessions use the agent
    # user's own Claude Code settings, and anything they don't allow is granted
    # live through the PermissionRequest hook.
    extra_args: list[str] = field(default_factory=list)
    heartbeat_seconds: float = 15
    permission_wait_seconds: int = 600
    progress_flush_seconds: float = 5
    sync_seconds: float = 60
    # Optional limits on one operator prompt's session; unset (the default): none, it runs until it's done.
    prompt_max_turns: int | None = None
    prompt_wall_clock_minutes: int | None = None

    def project_dir(self, project: str) -> Path | None:
        path = self.projects.get(project)
        return Path(path).expanduser() if path else None


def runner_config_path() -> Path:
    return paths.config_dir() / "runner.toml"


def load_runner_config(path: Path | None = None) -> RunnerConfig:
    path = path or runner_config_path()
    if not path.exists():
        return RunnerConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        cfg = RunnerConfig(**data)
    except (OSError, ValueError, TypeError) as exc:
        raise RunnerConfigError(f"invalid {path}: {exc}") from exc
    if not 1 <= cfg.concurrency <= 32:
        raise RunnerConfigError("concurrency must be between 1 and 32")
    if cfg.permission_mode not in PERMISSION_MODES:
        raise RunnerConfigError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)} "
                                f"(got {cfg.permission_mode!r}; bypassPermissions is not allowed)")
    if not isinstance(cfg.extra_args, list) or not all(isinstance(arg, str) for arg in cfg.extra_args):
        raise RunnerConfigError("extra_args must be a list of strings, e.g. [\"--model\", \"sonnet\"]")
    for arg in cfg.extra_args:
        if arg.startswith(FORBIDDEN_EXTRA_ARGS):
            raise RunnerConfigError(f"extra_args may not contain {arg!r}: set permission_mode instead "
                                    "(permission-bypass and permission-prompt flags are not allowed)")
    for key in ("prompt_max_turns", "prompt_wall_clock_minutes"):
        value = getattr(cfg, key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
            raise RunnerConfigError(f"{key} must be unset (no limit) or a whole number of at least 1 (got {value!r})")
    if not isinstance(cfg.projects, dict) or not all(isinstance(v, str) for v in cfg.projects.values()):
        raise RunnerConfigError("[projects] must map project names to directory paths")
    return cfg


def configured_model(cfg: RunnerConfig) -> str | None:
    """The model this runner's sessions will use, as far as it can tell before one runs: ``--model`` in
    ``extra_args``, else ``model`` in this account's Claude Code user settings, else ``$ANTHROPIC_MODEL``.
    None means Claude Code's default for the account."""
    args = cfg.extra_args
    for i, arg in enumerate(args):
        if arg == "--model" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    settings = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    try:
        model = json.loads(settings.read_text()).get("model")
    except (OSError, ValueError, AttributeError):
        model = None
    return model if isinstance(model, str) and model else os.environ.get("ANTHROPIC_MODEL") or None
