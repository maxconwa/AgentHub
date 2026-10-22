"""The Usage page's numbers: what task runs and prompt sessions used over a window.

A task run is a row of ``task_runs`` (the runner records it around each task session); a prompt session is a row of
``prompt_runs`` (the runner reports each one when it ends). Both carry turns and the CLI's est. cost; wall-clock is
``ended_at - started_at`` (a task run still going counts once it ends). A session stopped by the subscription usage
limit is a *limit hit*: it is listed and counted as such, and its turns, minutes and cost count like any other.

A run belongs to the window it started in. Days are UTC days, like every stored timestamp.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthub.hub.db import now_iso

WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
WINDOW_LABELS = {"24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days", "all": "All time"}
DEFAULT_WINDOW = "7d"
STRIP_DAYS = {"24h": 7, "7d": 7, "30d": 30, "all": 30}  # the per-day strip's length for each window
RECENT = 30  # runs listed under "Recent runs"
LIMIT = "usage_limit"
KINDS = ("task", "prompt")


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _minutes(r: dict[str, Any]) -> float:
    if not r["ended_at"]:
        return 0.0
    return max((_ts(r["ended_at"]) - _ts(r["started_at"])).total_seconds(), 0.0) / 60


def _runs(conn: sqlite3.Connection, since: str | None) -> list[dict[str, Any]]:
    """Every task run and prompt session that started at or after ``since`` (all of them for None), newest first."""
    where, args = ("WHERE r.started_at >= ?", (since,)) if since else ("", ())
    task_rows = conn.execute(
        "SELECT 'task' AS kind, r.id, r.agent_id, a.name AS agent, t.project, r.task_id, t.title AS task_title, "
        "NULL AS prompt_id, NULL AS target, r.started_at, r.ended_at, r.outcome, r.num_turns, r.cost_usd "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id JOIN agents a ON a.id = r.agent_id " + where, args)
    prompt_rows = conn.execute(
        "SELECT 'prompt' AS kind, r.id, r.agent_id, a.name AS agent, r.project, NULL AS task_id, NULL AS task_title, "
        "r.prompt_id, p.target, r.started_at, r.ended_at, r.outcome, r.num_turns, r.cost_usd "
        "FROM prompt_runs r JOIN prompts p ON p.id = r.prompt_id JOIN agents a ON a.id = r.agent_id " + where, args)
    runs = [dict(r) for r in [*task_rows, *prompt_rows]]
    for r in runs:
        r["minutes"] = _minutes(r)
        r["limit_hit"] = r["outcome"] == LIMIT
        # A prompt about a task counts on that task's line too (as prompt usage).
        if r["kind"] == "prompt" and (r["target"] or "").startswith("task:"):
            r["task_id"] = int(r["target"].removeprefix("task:"))
    return sorted(runs, key=lambda r: (r["started_at"], r["kind"], r["id"]), reverse=True)


def _empty() -> dict[str, Any]:
    return {"runs": 0, "limit_hits": 0, "running": 0, "turns": 0, "minutes": 0.0, "cost": 0.0, "costed": 0}


def _add(total: dict[str, Any], r: dict[str, Any]) -> None:
    total["runs"] += 1
    total["limit_hits"] += r["limit_hit"]
    total["running"] += r["ended_at"] is None
    total["turns"] += r["num_turns"] or 0
    total["minutes"] += r["minutes"]
    if r["cost_usd"] is not None:
        total["cost"] += r["cost_usd"]
        total["costed"] += 1


def _pair() -> dict[str, dict[str, Any]]:
    """Task and prompt totals side by side, plus their sum."""
    return {"task": _empty(), "prompt": _empty(), "all": _empty()}


def _add_pair(pair: dict[str, dict[str, Any]], r: dict[str, Any]) -> None:
    _add(pair[r["kind"]], r)
    _add(pair["all"], r)


def _ranked(groups: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
    """Heaviest first: by est. cost, then turns, then minutes."""
    return sorted(groups.values(), key=lambda g: (-g["all"]["cost"], -g["all"]["turns"], -g["all"]["minutes"],
                                                  str(g["label"])))


def summary(conn: sqlite3.Connection, window: str = DEFAULT_WINDOW, *, now: datetime | None = None) -> dict[str, Any]:
    """Totals over ``window`` (a key of :data:`WINDOWS`), by agent, project and task, per day, and the recent runs."""
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    now = now or datetime.now(UTC)
    span = WINDOWS[window]
    runs = _runs(conn, now_iso(now - span) if span else None)

    totals = _pair()
    by_agent: dict[str, dict[str, Any]] = {}
    by_project: dict[str | None, dict[str, Any]] = {}
    by_task: dict[int, dict[str, Any]] = {}
    for r in runs:
        _add_pair(totals, r)
        _add_pair(by_agent.setdefault(r["agent_id"], {"label": r["agent"], "agent_id": r["agent_id"], **_pair()}), r)
        _add_pair(by_project.setdefault(r["project"], {"label": r["project"] or "(no project)",
                                                       "project": r["project"], **_pair()}), r)
        if r["task_id"] is not None:
            _add_pair(by_task.setdefault(r["task_id"], {"label": r["task_id"], "task_id": r["task_id"],
                                                        "title": None, "project": None, **_pair()}), r)
    if by_task:
        marks = ",".join("?" * len(by_task))
        for t in conn.execute(f"SELECT id, title, project FROM tasks WHERE id IN ({marks})", list(by_task)):
            by_task[t["id"]].update(title=t["title"], project=t["project"])

    return {"window": window, "label": WINDOW_LABELS[window], "windows": list(WINDOWS), "labels": WINDOW_LABELS,
            "totals": totals, "by_agent": _ranked(by_agent), "by_project": _ranked(by_project),
            "by_task": _ranked(by_task), "days": per_day(conn, STRIP_DAYS[window], now=now),
            "recent": runs[:RECENT]}


def per_day(conn: sqlite3.Connection, days: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """The last ``days`` UTC days, oldest first: each day's task and prompt totals and ``height`` (0..100, its share
    of the busiest day's turns) for the strip's bars."""
    now = now or datetime.now(UTC)
    first = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out = {(first + timedelta(days=i)).date().isoformat(): _pair() for i in range(days)}
    for r in _runs(conn, now_iso(first)):
        day = r["started_at"][:10]
        if day in out:
            _add_pair(out[day], r)
    peak = max((d["all"]["turns"] for d in out.values()), default=0) or 1
    return [{"day": day, **pair,
             "height": {k: round(100 * pair[k]["turns"] / peak, 1) for k in KINDS}}
            for day, pair in out.items()]
