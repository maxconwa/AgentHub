"""Agent avatars: each agent's stored look, and the pose that shows its state.

A look is ``(avatar_version, avatar_seed)`` on the agent row; the art itself is
drawn by :mod:`agenthub.hub.avatar_art`. The pose is derived from hub state on
every page render, first match wins (see :func:`poses`). ``talking`` and
``janitor`` are drawn but not derived yet (they arrive with messaging and the
janitor).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from agenthub.hub import avatar_art, events
from agenthub.hub.db import now_iso
from agenthub.hub.store import NotFound


def reroll(conn: sqlite3.Connection, agent_id: str, *, actor: str) -> dict[str, int]:
    """Give the agent a new look (latest generator, fresh seed). Call inside a transaction."""
    look = {"version": avatar_art.GENERATOR_VERSION, "seed": avatar_art.new_seed()}
    cur = conn.execute("UPDATE agents SET avatar_version = ?, avatar_seed = ? WHERE id = ?",
                       (look["version"], look["seed"], agent_id))
    if cur.rowcount == 0:
        raise NotFound(f"no agent {agent_id}")
    events.emit(conn, type="agent.avatar_changed", actor=actor, payload={"agent_id": agent_id, **look})
    return look


def _ids(conn: sqlite3.Connection, sql: str, *args: Any) -> set[str]:
    return {r[0] for r in conn.execute(sql, args)}


def poses(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, str]:
    """Agent id -> pose for every agent that isn't revoked."""
    now_s = now_iso(now or datetime.now(UTC))
    agents = conn.execute("SELECT id, connected FROM agents WHERE status != 'revoked'").fetchall()
    needs_you = _ids(conn, "SELECT requester FROM approvals WHERE status = 'pending'")
    recharging = _ids(conn, "SELECT agent_id FROM task_agents WHERE paused_until > ?", now_s)
    running = {r["agent_id"]: r["stage"] for r in conn.execute(
        "SELECT r.agent_id, ta.stage FROM task_runs r "
        "LEFT JOIN task_agents ta ON ta.task_id = r.task_id AND ta.agent_id = r.agent_id "
        "WHERE r.ended_at IS NULL")}
    waiting = _ids(conn, "SELECT from_agent FROM asks WHERE status = 'pending' AND expires_at > ?", now_s)
    # The latest ended run per agent errored, on a task that is still live.
    errored = _ids(conn,
        "SELECT r.agent_id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.outcome = 'error' AND t.state NOT IN ('done', 'cancelled') AND r.id = "
        "(SELECT MAX(id) FROM task_runs WHERE agent_id = r.agent_id AND ended_at IS NOT NULL)")
    part_done = _ids(conn,
        "SELECT ta.agent_id FROM task_agents ta JOIN tasks t ON t.id = ta.task_id "
        "WHERE ta.stage = 'works' AND ta.finished_at IS NOT NULL AND t.state IN ('in_progress', 'review')")
    out = {}
    for a in agents:
        aid = a["id"]
        if not a["connected"]:
            pose = "offline"
        elif aid in needs_you:
            pose = "needs_you"
        elif aid in recharging:
            pose = "recharging"
        elif aid in running:
            pose = "reviewing" if running[aid] == "reviews" else "working"
        elif aid in waiting:
            pose = "waiting"
        elif aid in errored:
            pose = "error"
        elif aid in part_done:
            pose = "part_done"
        else:
            pose = "idle"
        out[aid] = pose
    return out


def url(version: int, seed: int, pose: str, *, scene: bool) -> str:
    return f"/ui/avatars/{version}/{seed}/{'scene' if scene else 'mini'}/{pose}.svg"


def board(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """What templates need to draw each non-revoked agent: both image URLs, alt text, pose."""
    current = poses(conn, now)
    out = {}
    for r in conn.execute("SELECT id, name, avatar_version, avatar_seed FROM agents WHERE status != 'revoked'"):
        pose = current.get(r["id"], "idle")
        v, s = r["avatar_version"], r["avatar_seed"]
        out[r["id"]] = {"pose": pose, "scene": url(v, s, pose, scene=True), "mini": url(v, s, pose, scene=False),
                        "alt": f"{r['name']}, {avatar_art.POSE_LABELS[pose]}"}
    return out
