"""Runner configuration: ``$AGENTHUB_CONFIG_DIR/runner.toml`` (see examples/runner.example.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agenthub import paths


class RunnerConfigError(Exception):
    pass


@dataclass
class RunnerConfig:
    # project name -> local checkout where that project's tasks run
    projects: dict[str, str] = field(default_factory=dict)
    concurrency: int = 1
    claude_bin: str = "claude"
    # Extra arguments for every `claude -p` session. Permission flags are left
    # out on purpose: sessions use the agent user's own Claude Code settings,
    # and anything else is granted live through the PermissionRequest hook.
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
    if not isinstance(cfg.projects, dict) or not all(isinstance(v, str) for v in cfg.projects.values()):
        raise RunnerConfigError("[projects] must map project names to directory paths")
    return cfg
