"""Hub and agent configuration (TOML files outside the repo)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from agenthub import paths


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML value: {value!r}")


def dump_toml(data: dict[str, Any]) -> str:
    """Scalars and lists as keys, dicts of scalars as tables (after the keys, as TOML requires)."""
    out = "".join(f"{k} = {_toml_value(v)}\n" for k, v in data.items() if v is not None and not isinstance(v, dict))
    for k, v in data.items():
        if isinstance(v, dict) and v:
            out += f"\n[{k}]\n" + "".join(f"{_toml_value(sk)} = {_toml_value(sv)}\n" for sk, sv in v.items())
    return out


def write_private(path: Path, content: str) -> None:
    """Write a file that only the owner can read, without a world-readable window."""
    paths.ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    path.chmod(0o600)


def _from_dict[T](cls: type[T], data: dict[str, Any]) -> T:
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    return cls(**data)


@dataclass
class HubConfig:
    bind_host: str = "127.0.0.1"
    port: int = 8700
    db_path: str = field(default_factory=lambda: str(paths.default_db_path()))
    # Tailscale logins (e.g. "alice@example.com") allowed to operate the hub.
    operators: list[str] = field(default_factory=list)
    whois_cache_seconds: int = 30
    tailscale_bin: str = "tailscale"
    max_body_bytes: int = 64 * 1024
    # Rate limits: per agent (all agent endpoints), and per caller address for enrollment.
    agent_rate_per_second: float = 10.0
    agent_rate_burst: int = 100
    enroll_rate_per_minute: float = 5.0
    # The hub's tailnet URL (e.g. https://hub.example.ts.net), used for links in notifications.
    public_url: str = ""
    # ntfy push notifications for new approvals. Empty ntfy_url disables them.
    ntfy_url: str = ""
    ntfy_topic: str = ""
    # Optional file holding an ntfy access token (kept out of this config).
    ntfy_token_file: str = ""
    # Off by default: notifications go to an external service, so they carry
    # only the approval id, kind and requester, not commands or task text.
    ntfy_include_details: bool = False
    # A group prompt's discussion ends after this long; recipients that haven't claimed or declined time out.
    # 1 to 1440 (a day). A v0.1.0 hub rejects this key: remove it from hub.toml before rolling back.
    prompt_discussion_minutes: int = 30
    # Opt-in, and it weakens separation of powers: tagged (agent) machines whose requests count as the
    # operator, as {Tailscale node StableID: operator login}. Any process on such a machine that sends no
    # agent token is the operator there, agents included. Manage with `agenthub hub operator-devices`.
    operator_devices: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> HubConfig:
        path = path or paths.hub_config_path()
        with path.open("rb") as fh:
            cfg = _from_dict(cls, tomllib.load(fh))
        minutes = cfg.prompt_discussion_minutes
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
            raise ValueError(f"prompt_discussion_minutes must be a whole number of minutes from 1 to 1440, "
                             f"not {minutes!r}")
        if not isinstance(cfg.operator_devices, dict):
            raise ValueError("operator_devices must be a table of node StableID = operator login")
        for node, login in cfg.operator_devices.items():
            if login not in cfg.operators:
                raise ValueError(f"operator_devices: {node} maps to {login!r}, which is not in operators")
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or paths.hub_config_path()
        write_private(path, dump_toml(asdict(self)))
        return path


@dataclass
class AgentConfig:
    hub_url: str
    agent_id: str
    name: str
    token_file: str

    @classmethod
    def load(cls, path: Path | None = None) -> AgentConfig:
        path = path or paths.agent_config_path()
        with path.open("rb") as fh:
            return _from_dict(cls, tomllib.load(fh))

    def save(self, path: Path | None = None) -> Path:
        path = path or paths.agent_config_path()
        write_private(path, dump_toml(asdict(self)))
        return path

    def read_token(self) -> str:
        token_path = Path(self.token_file)
        mode = token_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(f"{token_path} must not be readable by group/others (mode {mode:o})")
        return token_path.read_text().strip()
