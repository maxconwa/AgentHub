"""Device usage stats the runner sends with its heartbeat: CPU %, RAM and, per NVIDIA GPU, VRAM and utilisation.

No new dependency. On Linux the CPU and memory figures come from /proc/stat and /proc/meminfo. Elsewhere psutil is
used if it is installed, and otherwise those figures are None ("unavailable"). GPUs come from ``nvidia-smi``, which is
looked up once (a missing binary is remembered, so a machine without a GPU pays nothing). It runs with a hard
timeout, and after a failure it is skipped for :data:`GPU_BACKOFF_SECONDS`. No GPU, or any failure, gives
``gpus: []`` and never an error.

:meth:`Collector.sample` blocks (it reads files and may wait up to :data:`NVIDIA_SMI_TIMEOUT` for nvidia-smi), so the
runner calls it in a thread and never on the event loop. See the shape pinned in board ``prompt-20/interfaces``.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NVIDIA_SMI_TIMEOUT = 2.0
GPU_BACKOFF_SECONDS = 60.0
MAX_GPUS = 16
NVIDIA_SMI_QUERY = ["--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits"]
MIB = 1 << 20

_UNSET = object()


def _read_cpu_times(path: Path) -> tuple[int, int] | None:
    """(busy, total) jiffies from the first line of /proc/stat, or None if unreadable."""
    try:
        first = path.read_text().splitlines()[0].split()
        if first[0] != "cpu":
            return None
        fields = [int(x) for x in first[1:]]
    except (OSError, IndexError, ValueError):
        return None
    if len(fields) < 4:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    # guest and guest_nice (fields 9, 10) are already counted in user and nice.
    total = sum(fields[:8])
    return total - idle, total


def _read_meminfo(path: Path) -> tuple[int, int] | None:
    """(used, total) bytes from /proc/meminfo (used = MemTotal - MemAvailable), or None if unreadable."""
    try:
        values = {}
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                values[key] = int(rest.split()[0]) * 1024  # kB
        total, available = values["MemTotal"], values["MemAvailable"]
    except (OSError, KeyError, IndexError, ValueError):
        return None
    if total <= 0 or not 0 <= available <= total:
        return None
    return total - available, total


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_nvidia_smi(output: str) -> list[dict[str, Any]]:
    """GPUs from ``nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu
    --format=csv,noheader,nounits`` output. Lines that don't parse are skipped, and a utilisation of "[N/A]" is None.
    A GPU name may itself contain commas, so the name is everything between the first field and the last three."""
    gpus = []
    for line in output.splitlines():
        raw = line.split(",")
        if len(raw) < 5:
            continue
        parts = [p.strip() for p in raw]
        index, name, used, total, util = parts[0], ",".join(raw[1:-3]).strip(), *parts[-3:]
        used_mib, total_mib, util_pct = _number(used), _number(total), _number(util)
        if not index.isdigit() or used_mib is None or total_mib is None:
            continue
        if not 0 <= used_mib <= total_mib or total_mib <= 0 or not name.isprintable():
            continue
        if util_pct is not None and not 0 <= util_pct <= 100:
            util_pct = None
        gpus.append({"index": int(index), "name": name[:100], "mem_used": int(used_mib * MIB),
                     "mem_total": int(total_mib * MIB), "util_pct": util_pct})
        if len(gpus) == MAX_GPUS:
            break
    return gpus


class Collector:
    """Takes samples; keeps what a CPU % delta needs between them. Not thread-safe: one caller at a time."""

    def __init__(self, *, proc: Path = Path("/proc"), nvidia_smi: Any = _UNSET, platform: str = sys.platform,
                 clock=time.monotonic) -> None:
        self.proc = proc
        self.linux = platform.startswith("linux")
        self.clock = clock
        self._nvidia_smi: str | None | object = nvidia_smi  # _UNSET until looked up once; None = not present
        self._gpu_retry_at = 0.0
        self._cpu_prev: tuple[int, int] | None = None
        self._psutil: Any = _UNSET

    # --- CPU and memory -----------------------------------------------------------------------------------------

    def _psutil_module(self) -> Any:
        if self._psutil is _UNSET:
            try:
                import psutil  # optional: never a dependency

                psutil.cpu_percent(interval=None)  # primes its delta; the first call always says 0.0
                self._psutil = psutil
            except Exception:  # noqa: BLE001 - not installed, or broken on this platform
                self._psutil = None
        return self._psutil

    def cpu_pct(self) -> float | None:
        """Whole-machine CPU % since the previous call. The first call reads twice, 0.2 s apart (it runs in a
        thread). None if unavailable."""
        if self.linux:
            now = _read_cpu_times(self.proc / "stat")
            if now is None:
                return None
            prev = self._cpu_prev
            if prev is None:
                time.sleep(0.2)
                prev, now = now, _read_cpu_times(self.proc / "stat")
                if now is None:
                    return None
            self._cpu_prev = now
            busy, total = now[0] - prev[0], now[1] - prev[1]
            if total <= 0 or busy < 0:
                return None
            return round(min(100.0, 100.0 * busy / total), 1)
        psutil = self._psutil_module()
        if psutil is None:
            return None
        try:
            value = float(psutil.cpu_percent(interval=None))
        except Exception:  # noqa: BLE001
            return None
        return round(value, 1) if math.isfinite(value) and 0 <= value <= 100 else None

    def memory(self) -> tuple[int, int] | None:
        """(used, total) bytes, or None if unavailable."""
        if self.linux:
            return _read_meminfo(self.proc / "meminfo")
        psutil = self._psutil_module()
        if psutil is None:
            return None
        try:
            vm = psutil.virtual_memory()
            total, used = int(vm.total), int(vm.total - vm.available)
        except Exception:  # noqa: BLE001
            return None
        return (used, total) if total > 0 and 0 <= used <= total else None

    # --- GPUs ---------------------------------------------------------------------------------------------------

    def gpus(self) -> list[dict[str, Any]]:
        """Per-GPU VRAM and utilisation from nvidia-smi; [] if there is none, or it fails, hangs or prints garbage."""
        if self._nvidia_smi is _UNSET:
            self._nvidia_smi = shutil.which("nvidia-smi")
        binary = self._nvidia_smi
        if binary is None or self.clock() < self._gpu_retry_at:
            return []
        try:
            done = subprocess.run([binary, *NVIDIA_SMI_QUERY], capture_output=True, text=True,
                                  timeout=NVIDIA_SMI_TIMEOUT, stdin=subprocess.DEVNULL, check=False)
            gpus = parse_nvidia_smi(done.stdout) if done.returncode == 0 else []
            failed = done.returncode != 0 or not gpus
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
            log.debug("nvidia-smi failed: %s", exc)
            gpus, failed = [], True
        if failed:
            self._gpu_retry_at = self.clock() + GPU_BACKOFF_SECONDS
        return gpus

    # --- all of it ----------------------------------------------------------------------------------------------

    def sample(self) -> dict[str, Any]:
        """One sample in the heartbeat's ``stats`` shape. Blocking: call it in a thread."""
        mem = self.memory()
        return {"cpu_pct": self.cpu_pct(),
                "mem_used": mem[0] if mem else None, "mem_total": mem[1] if mem else None,
                "gpus": self.gpus()}
