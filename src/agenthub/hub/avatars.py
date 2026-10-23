"""Agent avatars: each agent's stored look, and the pose that shows its state.

A look is ``(avatar_version, avatar_seed)`` on the agent row; the art itself is
drawn by :mod:`agenthub.hub.avatar_art`. The pose is derived from hub state on
every page render, first match wins (see :func:`poses`). ``talking`` is an
agent in a group prompt's discussion; ``janitor`` is drawn but not derived yet
(it arrives with the janitor). ``error`` is drawn but no longer derived: an errored
task run shows ``failed``, like a failed prompt session.
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
    from agenthub.hub import prompts  # imported here: prompts imports views, which imports this module
    now_s = now_iso(now or datetime.now(UTC))
    agents = conn.execute("SELECT id, connected FROM agents WHERE status != 'revoked'").fetchall()
    needs_you = _ids(conn, "SELECT requester FROM approvals WHERE status = 'pending' "
                           "UNION SELECT agent_id FROM questions WHERE status = 'pending'")
    # A pause left over on a finished task doesn't count.
    recharging = _ids(conn,
        "SELECT ta.agent_id FROM task_agents ta JOIN tasks t ON t.id = ta.task_id "
        "WHERE ta.paused_until > ? AND t.state NOT IN ('done', 'cancelled', 'rejected')", now_s)
    running: dict[str, str] = {}
    for r in conn.execute(
            "SELECT r.agent_id, ta.stage FROM task_runs r "
            "LEFT JOIN task_agents ta ON ta.task_id = r.task_id AND ta.agent_id = r.agent_id "
            "WHERE r.ended_at IS NULL"):
        # Several active runs (on different tasks): any review run makes it "reviewing".
        if running.get(r["agent_id"]) != "reviews":
            running[r["agent_id"]] = r["stage"]
    # A prompt session going: working on a single prompt, talking it over in a group prompt's discussion.
    prompting = {r["agent_id"]: "talking" if r["mode"] == "group" else "working" for r in conn.execute(
        "SELECT r.agent_id, p.mode FROM prompt_recipients r JOIN prompts p ON p.id = r.prompt_id "
        "WHERE r.status = 'running' ORDER BY p.mode")}  # 'single' after 'group': a single prompt's session wins
    waiting = _ids(conn, "SELECT from_agent FROM asks WHERE status = 'pending' AND expires_at > ?", now_s)
    # "failed": the latest unit of work the agent finished failed. That is its latest ended task run, if it errored
    # on a task that is still live and no prompt session finished after it; or its latest finished prompt session,
    # if it failed and no task run ended after it. A withdrawn recipient never ran a session, so it doesn't count.
    failed = _ids(conn,
        "SELECT r.agent_id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.outcome = 'error' AND t.state NOT IN ('done', 'cancelled') AND r.id = "
        "(SELECT MAX(id) FROM task_runs WHERE agent_id = r.agent_id AND ended_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM prompt_recipients WHERE agent_id = r.agent_id AND note != ? "
        "AND status NOT IN ('pending', 'running', 'skipped') AND updated_at > r.ended_at) "
        "UNION SELECT r.agent_id FROM prompt_recipients r WHERE r.status = 'failed' AND r.note != ? "
        "AND r.updated_at = (SELECT MAX(updated_at) FROM prompt_recipients WHERE agent_id = r.agent_id "
        "AND status NOT IN ('pending', 'running', 'skipped')) "
        "AND NOT EXISTS (SELECT 1 FROM task_runs WHERE agent_id = r.agent_id AND ended_at > r.updated_at)",
        prompts.WITHDRAWN, prompts.WITHDRAWN)
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
        elif aid in prompting:
            pose = prompting[aid]
        elif aid in waiting:
            pose = "waiting"
        elif aid in failed:
            pose = "failed"
        elif aid in part_done:
            pose = "part_done"
        else:
            pose = "idle"
        out[aid] = pose
    return out


def url(version: int, seed: int, pose: str, *, scene: bool) -> str:
    return f"/ui/avatars/{version}/{seed}/{'scene' if scene else 'mini'}/{pose}.svg"


def board(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """What templates need to draw each non-revoked agent: both image URLs, alt text, pose and its label."""
    current = poses(conn, now)
    out = {}
    for r in conn.execute("SELECT id, name, avatar_version, avatar_seed FROM agents WHERE status != 'revoked'"):
        pose = current.get(r["id"], "idle")
        v, s = r["avatar_version"], r["avatar_seed"]
        out[r["id"]] = {"pose": pose, "scene": url(v, s, pose, scene=True), "mini": url(v, s, pose, scene=False),
                        "alt": r["name"], "label": avatar_art.POSE_LABELS[pose]}
    return out
