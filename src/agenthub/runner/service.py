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

Several agents on one account each have their own config dir and their own
service. The agent in the default config dir keeps the plain names
(agenthub-runner.service, com.agenthub.runner); any other is suffixed with
its agent name's slug, the name lowercased (agenthub-runner-<slug>.service,
com.agenthub.runner.<slug>; see agenthub.names): "Hub-Builder-1" gets
agenthub-runner-hub-builder-1.service, and a lowercase name is unchanged.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from agenthub import paths
from agenthub.names import slug

UNIT_NAME = "agenthub-runner.service"
LAUNCHD_LABEL = "com.agenthub.runner"
EXIT_REVOKED = 3  # the runner exits with this when the hub revokes the agent; do not restart
# Searched after the directories of agenthub and claude: launchd starts jobs with only /usr/bin:/bin:/usr/sbin:/sbin.
LAUNCHD_BASE_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def use_launchd() -> bool:
    return sys.platform == "darwin"


def instance(config_dir: Path, name: str) -> str | None:
    """None for the agent in the default config dir, else what its service is suffixed with: the agent name's slug."""
    return None if config_dir.resolve() == paths.default_config_dir().resolve() else slug(name)


def unit_name(inst: str | None = None) -> str:
    return f"agenthub-runner-{inst}.service" if inst else UNIT_NAME


def launchd_label(inst: str | None = None) -> str:
    return f"{LAUNCHD_LABEL}.{inst}" if inst else LAUNCHD_LABEL


def launchd_log(inst: str | None = None) -> Path:
    return Path.home() / "Library" / "Logs" / (f"agenthub-runner-{inst}.log" if inst else "agenthub-runner.log")


def revoked_exit_code() -> int:
    """launchd cannot exempt one exit status from KeepAlive, only a clean exit, so under launchd exit 0."""
    label = os.environ.get("XPC_SERVICE_NAME", "")
    return 0 if label == LAUNCHD_LABEL or label.startswith(LAUNCHD_LABEL + ".") else EXIT_REVOKED


def install(exe: str, config_dir: Path, name: str) -> tuple[Path, str | None]:
    """Write and start this agent's runner service. Returns its file and a problem, if any."""
    inst = instance(config_dir, name)
    if use_launchd():
        path = write_plist(exe, config_dir, inst=inst)
        return path, launchd_start(path, launchd_label(inst))
    path = write_unit(exe, config_dir, inst=inst)
    return path, enable_and_start(unit_name(inst))


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


def write_unit(exe: str, config_dir: Path, directory: Path | None = None, inst: str | None = None) -> Path:
    directory = directory or unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / unit_name(inst)
    path.write_text(UNIT.format(exe=exe, config_dir=config_dir.resolve(), revoked=EXIT_REVOKED))
    return path


def enable_and_start(unit: str = UNIT_NAME) -> str | None:
    """Returns None on success, or a message explaining what to do by hand."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return "systemctl not found; start the runner yourself with `agenthub runner`"
    for args in (["--user", "daemon-reload"], ["--user", "enable", "--now", unit]):
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


def plist_data(exe: str, config_dir: Path, inst: str | None = None) -> dict:
    log = launchd_log(inst)
    return {
        "Label": launchd_label(inst),
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


def write_plist(exe: str, config_dir: Path, directory: Path | None = None, inst: str | None = None) -> Path:
    directory = directory or launch_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{launchd_label(inst)}.plist"
    path.write_bytes(plistlib.dumps(plist_data(exe, config_dir, inst)))
    return path


def launchd_start(path: Path, label: str = LAUNCHD_LABEL) -> str | None:
    """(Re)load the agent into the gui domain. Returns None on success, or what to do by hand."""
    domain = launchd_domain()
    # bootout of a job that is not loaded fails harmlessly; it makes a rerun pick up a rewritten plist.
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return (f"`launchctl bootstrap {domain}` failed ({proc.stderr.strip() or proc.stdout.strip()}); "
                "log in to this user's desktop session and run "
                f"`launchctl bootstrap {domain} {path}`, or start the runner yourself with `agenthub runner`")
    return None
