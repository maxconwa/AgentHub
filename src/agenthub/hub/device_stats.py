"""Device usage stats (0.3.10): the CPU %, RAM and per-GPU VRAM each runner sends with its heartbeat.

A machine is its ``device_node_id``, which comes from the heartbeat's authenticated caller and never from the frame.
Several runners on one machine all send stats, so the hub accepts at most one sample per device every
:data:`MIN_INTERVAL_SECONDS` (checked in memory, before any validation or DB access). An accepted sample replaces
the device's single row in ``device_stats`` (migration 26), so the table never grows. It is also appended to a
bounded in-memory ring (:data:`RING_SIZE` per device, :data:`MAX_DEVICES` devices) that feeds the Devices page's
sparklines. The ring is never written to the DB, so it starts empty after a restart.

Stats are operator-only: the Devices pages and the JSON under /ui/api. Agents have no route or tool that reads them.
The shape is pinned in board ``prompt-20/interfaces``:
``{"ts", "cpu_pct", "mem_used", "mem_total", "gpus": [{"index", "name", "mem_used", "mem_total", "util_pct"}]}``.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from agenthub.hub import views
from agenthub.hub.dashboard import Conn, Op  # the dashboard's operator checks
from agenthub.hub.db import now_iso

log = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 10.0
RING_SIZE = 60
MAX_DEVICES = 256
STALE_SECONDS = 60
MAX_GPUS = 16
MAX_GPU_NAME = 100
MAX_BYTES = 1 << 50  # 1 PiB: anything bigger is a bug or a lie

_lock = threading.Lock()  # heartbeats arrive on the event loop; the GET routes run in the thread pool
_ring: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
_last_accepted: dict[str, float] = {}
_clock = time.monotonic


def reset() -> None:
    """Forget the ring and the per-device throttle (tests; a restart does the same)."""
    with _lock:
        _ring.clear()
        _last_accepted.clear()


# --- validation ------------------------------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    # an int is always finite, and math.isfinite() on one too big for a float raises OverflowError
    return isinstance(value, int | float) and not isinstance(value, bool) and (
        isinstance(value, int) or math.isfinite(value))


def _pct(value: Any) -> float | None:
    if value is None:
        return None
    if not _is_number(value) or not 0 <= value <= 100:
        raise ValueError(f"bad percentage {value!r}")
    return round(float(value), 1)


def _bytes_pair(used: Any, total: Any) -> tuple[int | None, int | None]:
    if used is None and total is None:
        return None, None
    for v in (used, total):
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_BYTES:
            raise ValueError(f"bad byte count {v!r}")
    if used > total:
        raise ValueError("used > total")
    return used, total


def _gpu(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("gpu entry is not an object")
    index, name = raw.get("index"), raw.get("name")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 255:
        raise ValueError(f"bad gpu index {index!r}")
    if not isinstance(name, str) or len(name) > MAX_GPU_NAME or not name.isprintable():
        raise ValueError("bad gpu name")
    used, total = _bytes_pair(raw.get("mem_used"), raw.get("mem_total"))
    if used is None:
        raise ValueError("gpu without memory figures")
    return {"index": index, "name": name, "mem_used": used, "mem_total": total, "util_pct": _pct(raw.get("util_pct"))}


def validate(raw: Any) -> dict[str, Any] | None:
    """The heartbeat's ``stats``, normalised to exactly the pinned keys (without ``ts``), or None if anything is
    wrong: then the whole sample is dropped and the rest of the heartbeat still applies."""
    if not isinstance(raw, dict):
        return None
    try:
        gpus = raw.get("gpus", [])
        if not isinstance(gpus, list) or len(gpus) > MAX_GPUS:
            raise ValueError("bad gpus list")
        used, total = _bytes_pair(raw.get("mem_used"), raw.get("mem_total"))
        return {"cpu_pct": _pct(raw.get("cpu_pct")), "mem_used": used, "mem_total": total,
                "gpus": [_gpu(g) for g in gpus]}
    except ValueError as exc:
        log.debug("dropping invalid device stats: %s", exc)
        return None


# --- recording -------------------------------------------------------------------------------------------------


def record(conn: sqlite3.Connection, node_id: str, agent_id: str, raw: Any) -> dict[str, Any] | None:
    """Store one heartbeat's stats for the caller's device; returns the stored sample (with ``ts``), or None if it
    was throttled (another runner on the device reported less than MIN_INTERVAL_SECONDS ago) or invalid."""
    now = _clock()
    with _lock:
        last = _last_accepted.get(node_id)
        if last is not None and now - last < MIN_INTERVAL_SECONDS:
            return None
    sample = validate(raw)
    if sample is None:
        return None
    ts = now_iso()
    conn.execute(
        "INSERT INTO device_stats (node_id, sample, agent_id, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET sample = excluded.sample, agent_id = excluded.agent_id, "
        "updated_at = excluded.updated_at",
        (node_id, json.dumps(sample, separators=(",", ":")), agent_id, ts))
    stored = {"ts": ts, **sample}
    with _lock:
        _last_accepted[node_id] = now
        ring = _ring.get(node_id)
        if ring is None:
            ring = _ring[node_id] = deque(maxlen=RING_SIZE)
        _ring.move_to_end(node_id)
        ring.append(stored)
        while len(_ring) > MAX_DEVICES:
            gone, _ = _ring.popitem(last=False)
            _last_accepted.pop(gone, None)
    return stored


# --- reading ---------------------------------------------------------------------------------------------------


def _from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {"ts": row["updated_at"], **json.loads(row["sample"])}


def get(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    """The device's latest sample (from the DB, so it survives a restart), or None if it never reported."""
    row = conn.execute("SELECT sample, updated_at FROM device_stats WHERE node_id = ?", (node_id,)).fetchone()
    return _from_row(row) if row else None


def latest(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """node_id -> latest sample, for every device that ever reported."""
    return {r["node_id"]: _from_row(r) for r in conn.execute("SELECT node_id, sample, updated_at FROM device_stats")}


def history(node_id: str) -> list[dict[str, Any]]:
    """The device's recent samples, oldest first (at most RING_SIZE; [] after a restart)."""
    with _lock:
        ring = _ring.get(node_id)
        return list(ring) if ring else []


def is_stale(sample: dict[str, Any] | None, now: datetime | None = None) -> bool:
    """True if there is no sample or it is older than STALE_SECONDS (the device's runners stopped reporting)."""
    if sample is None:
        return True
    taken = datetime.strptime(sample["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - taken).total_seconds() > STALE_SECONDS


# --- operator routes -------------------------------------------------------------------------------------------


def _entry(device: dict[str, Any]) -> dict[str, Any]:
    return {"node_id": device["node_id"], "name": device["name"], "online": device["online"],
            "stats": device["stats"], "stale": device["stats_stale"], "history": history(device["node_id"])}


def build_router() -> APIRouter:
    """Operator-only JSON for the Devices pages, under /ui so the dashboard's rules cover it (Tailscale identity,
    never an agent token, the agents' guard hook). Read-only, so no CSRF."""
    router = APIRouter(prefix="/ui/api")

    @router.get("/devices/stats")
    def all_stats(operator: Op, conn: Conn) -> dict[str, Any]:
        return {"stale_seconds": STALE_SECONDS, "devices": [_entry(d) for d in views.devices(conn)]}

    @router.get("/devices/{node_id}/stats")
    def one_device(node_id: str, operator: Op, conn: Conn) -> dict[str, Any]:
        return _entry(views.device(conn, node_id))

    return router
