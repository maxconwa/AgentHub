"""systemd user service for the runner (installed and started by `agenthub join`).

A user service only runs while the user has a session unless lingering is
enabled (loginctl(1), pam_systemd(8)): run `loginctl enable-linger <user>`
once, as an administrator, for the dedicated agent user.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

UNIT_NAME = "agenthub-runner.service"
EXIT_REVOKED = 3  # the runner exits with this when the hub revokes the agent; do not restart

UNIT = """\
[Unit]
Description=agenthub runner (headless Claude Code task execution)
After=network-online.target

[Service]
Environment=AGENTHUB_CONFIG_DIR={config_dir}
ExecStart={exe} runner
Restart=on-failure
RestartSec=10
RestartPreventExitStatus={revoked}
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def write_unit(exe: str, config_dir: Path, directory: Path | None = None) -> Path:
    directory = directory or unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / UNIT_NAME
    path.write_text(UNIT.format(exe=exe, config_dir=config_dir.resolve(), revoked=EXIT_REVOKED))
    return path


def enable_and_start() -> str | None:
    """Returns None on success, or a message explaining what to do by hand."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return "systemctl not found; start the runner yourself with `agenthub runner`"
    for args in (["--user", "daemon-reload"], ["--user", "enable", "--now", UNIT_NAME]):
        proc = subprocess.run([systemctl, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            return f"`systemctl {' '.join(args)}` failed: {proc.stderr.strip() or proc.stdout.strip()}"
    return None
