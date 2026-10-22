"""The runner's service (installed and started by `agenthub join`).

Linux: a systemd user service. It only runs while the user has a session
unless lingering is enabled (loginctl(1), pam_systemd(8)): run
`loginctl enable-linger <user>` once, as an administrator, for the dedicated
agent user.

macOS: a launchd agent in ~/Library/LaunchAgents, bootstrapped into the
user's gui/<uid> domain (launchd.plist(5), launchctl(1)). That domain exists
only while the user is logged in to the desktop, which is also what gives
`claude` access to its login keychain; fast user switching keeps the agent
user's session (and the runner) alive in the background.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "agenthub-runner.service"
LAUNCHD_LABEL = "com.agenthub.runner"
EXIT_REVOKED = 3  # the runner exits with this when the hub revokes the agent; do not restart
# Searched after the directories of agenthub and claude: launchd starts jobs with only /usr/bin:/bin:/usr/sbin:/sbin.
LAUNCHD_BASE_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def use_launchd() -> bool:
    return sys.platform == "darwin"


def revoked_exit_code() -> int:
    """launchd cannot exempt one exit status from KeepAlive, only a clean exit, so under launchd exit 0."""
    return 0 if os.environ.get("XPC_SERVICE_NAME") == LAUNCHD_LABEL else EXIT_REVOKED


def install(exe: str, config_dir: Path) -> tuple[Path, str | None]:
    """Write and start the runner service for this platform. Returns its file and a problem, if any."""
    if use_launchd():
        path = write_plist(exe, config_dir)
        return path, launchd_start(path)
    path = write_unit(exe, config_dir)
    return path, enable_and_start()

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


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def launchd_path(exe: str) -> str:
    dirs = [str(Path(exe).parent)]
    claude = shutil.which("claude")
    if claude:
        dirs.append(str(Path(claude).parent))
    return ":".join(dict.fromkeys([*dirs, *LAUNCHD_BASE_PATH]))


def plist_data(exe: str, config_dir: Path) -> dict:
    log = Path.home() / "Library" / "Logs" / "agenthub-runner.log"
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [exe, "runner"],
        "EnvironmentVariables": {"AGENTHUB_CONFIG_DIR": str(config_dir.resolve()), "PATH": launchd_path(exe)},
        "RunAtLoad": True,
        # Restart after a crash or a failed start, but not after a clean exit (see revoked_exit_code).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def write_plist(exe: str, config_dir: Path, directory: Path | None = None) -> Path:
    directory = directory or launch_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{LAUNCHD_LABEL}.plist"
    path.write_bytes(plistlib.dumps(plist_data(exe, config_dir)))
    return path


def launchd_start(path: Path) -> str | None:
    """(Re)load the agent into the gui domain. Returns None on success, or what to do by hand."""
    domain = launchd_domain()
    # bootout of a job that is not loaded fails harmlessly; it makes a rerun pick up a rewritten plist.
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return (f"`launchctl bootstrap {domain}` failed ({proc.stderr.strip() or proc.stdout.strip()}); "
                "log in to this user's desktop session and run "
                f"`launchctl bootstrap {domain} {path}`, or start the runner yourself with `agenthub runner`")
    return None
