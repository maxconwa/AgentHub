"""Locations of runtime state. Nothing here ever points inside the project tree.

Defaults follow the XDG base directory spec:
  config: $AGENTHUB_CONFIG_DIR or $XDG_CONFIG_HOME/agenthub or ~/.config/agenthub
  data:   $AGENTHUB_DATA_DIR   or $XDG_DATA_HOME/agenthub   or ~/.local/share/agenthub
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    override = os.environ.get("AGENTHUB_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "agenthub"


def data_dir() -> Path:
    override = os.environ.get("AGENTHUB_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_DATA_HOME", ".local/share") / "agenthub"


def hub_config_path() -> Path:
    return config_dir() / "hub.toml"


def agent_config_path() -> Path:
    return config_dir() / "agent.toml"


def default_db_path() -> Path:
    return data_dir() / "hub.sqlite3"


def ensure_private_dir(path: Path) -> Path:
    """Create a directory readable only by the owner."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path
