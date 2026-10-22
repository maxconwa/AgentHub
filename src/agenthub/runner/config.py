"""Runner configuration: ``$AGENTHUB_CONFIG_DIR/runner.toml`` (see examples/runner.example.toml)."""

from __future__ import annotations

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
    if not isinstance(cfg.projects, dict) or not all(isinstance(v, str) for v in cfg.projects.values()):
        raise RunnerConfigError("[projects] must map project names to directory paths")
    return cfg
