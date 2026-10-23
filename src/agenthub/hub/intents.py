"""Declared work areas ("intents") and coordination when they overlap.

An agent announces the files or modules it is about to change as glob
patterns (``src/api/**``, ``docs/*.md``) or plain names (``auth``,
``auth.tokens``). When a new intent overlaps an active intent of another
agent in the same project, the hub:

  * opens a coordination thread: a kickoff message in the project's chat
    addressed to each agent (so the inbox, Stop hook and runner pick it up),
  * notifies the operator (an ``intent.conflict`` event, pushed via ntfy),
  * creates the board document ``coordination/<id>`` where the agents record
    the split or ordering they agree on.

Overlap is deliberately conservative: two globs whose fixed directory
prefixes nest are treated as overlapping. A false alarm costs one short
conversation; a missed overlap costs a merge conflict.
"""

from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthub.hub import chats, events, store
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Forbidden, Invalid, NotFound

HUB_ACTOR = "system:hub"
MAX_PATTERNS = 20
MAX_TTL_MINUTES = 24 * 60
PATTERN_RE = re.compile(r"^[A-Za-z0-9_.*?\[\]{}!,/-]{1,200}$")
_WILD = set("*?[{")


def normalize(pattern: str) -> str:
    p = pattern.strip()
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/{2,}", "/", p).rstrip("/")
    if not PATTERN_RE.fullmatch(p) or ".." in p.split("/"):
        raise Invalid(f"invalid intent pattern {pattern!r}: use relative paths, globs, or module names")
    return p


def _has_wild(p: str) -> bool:
    return any(c in _WILD for c in p)


def _static_prefix(p: str) -> str:
    out = []
    for seg in p.split("/"):
        if _has_wild(seg):
            break
        out.append(seg)
    return "/".join(out)


def _contains(outer: str, inner: str) -> bool:
    """outer is inner, or an ancestor directory / parent module of it."""
    return outer == "" or inner == outer or inner.startswith(outer + "/") or inner.startswith(outer + ".")


def patterns_overlap(p: str, q: str) -> bool:
    if p == q:
        return True
    pw, qw = _has_wild(p), _has_wild(q)
    if not pw and not qw:
        return _contains(p, q) or _contains(q, p)
    if pw and qw:
        sp, sq = _static_prefix(p), _static_prefix(q)
        return _contains(sp, sq) or _contains(sq, sp)
    literal, glob = (p, q) if not pw else (q, p)
    prefix = _static_prefix(glob)
    if "{" in glob:  # fnmatch has no brace expansion: fall back to the directory check
        return _contains(prefix, literal) or _contains(literal, prefix)
    # a matching path, or a directory/module that contains the glob's area
    return fnmatch.fnmatchcase(literal, glob) or _contains(literal, prefix)


def overlaps(a: list[str], b: list[str]) -> list[list[str]]:
    return [[p, q] for p in a for q in b if patterns_overlap(p, q)]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["patterns"] = json.loads(out["patterns"])
    return out


def active(conn: sqlite3.Connection, project: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT i.*, a.name AS agent_name FROM intents i JOIN agents a ON a.id = i.agent_id "
        "WHERE i.project = ? AND i.cleared_at IS NULL AND i.expires_at > ? ORDER BY i.id",
        (project, now_iso()),
    ).fetchall()
    return [_row(r) for r in rows]


def announce(
    conn: sqlite3.Connection,
    agent: Agent,
    *,
    project: str,
    patterns: list[str],
    description: str = "",
    ttl_minutes: int = 60,
    device: str | None = None,
) -> dict[str, Any]:
    if not patterns or len(patterns) > MAX_PATTERNS:
        raise Invalid(f"give 1 to {MAX_PATTERNS} patterns")
    if not 1 <= ttl_minutes <= MAX_TTL_MINUTES:
        raise Invalid(f"ttl_minutes must be in [1, {MAX_TTL_MINUTES}]")
    clean = sorted({normalize(p) for p in patterns})
    now = datetime.now(UTC)
    cur = conn.execute(
        "INSERT INTO intents (project, agent_id, patterns, description, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project, agent.id, json.dumps(clean), description, now_iso(now),
         now_iso(now + timedelta(minutes=ttl_minutes))),
    )
    intent_id = cur.lastrowid
    events.emit(conn, type="intent.announced", actor=agent.id, project=project, device=device,
                payload={"intent_id": intent_id, "patterns": clean, "description": description,
                         "ttl_minutes": ttl_minutes})
    conflicts = []
    for other in active(conn, project):
        if other["agent_id"] == agent.id or other["id"] == intent_id:
            continue
        hits = overlaps(clean, other["patterns"])
        if hits:
            conflicts.append(_open_conflict(conn, project, other, intent_id, agent, hits))
    return {"intent_id": intent_id, "patterns": clean, "conflicts": conflicts}


def _open_conflict(
    conn: sqlite3.Connection, project: str, other: dict[str, Any], intent_id: int, agent: Agent,
    hits: list[list[str]],
) -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO intent_conflicts (project, intent_a, intent_b, channel, board_key, overlap, created_at) "
        "VALUES (?, ?, ?, '', '', ?, ?)",
        (project, other["id"], intent_id, json.dumps(hits), now_iso()),
    )
    cid = cur.lastrowid
    # The channel name is kept for the record (0.3.7 coordinated there); since 0.3.8 the kickoff and the talk are
    # in the project's chat, addressed with ``to``.
    channel, board_key = f"coordination-{cid}", f"coordination/{cid}"
    conn.execute("UPDATE intent_conflicts SET channel = ?, board_key = ? WHERE id = ?", (channel, board_key, cid))
    names = [other["agent_name"], agent.name]
    overlap_text = ", ".join(f"{p} ~ {q}" for p, q in hits[:10])
    store.board_put(conn, actor=HUB_ACTOR, project=project, key=board_key, expected_version=0, value=json.dumps({
        "status": "open", "agents": names, "intents": [other["id"], intent_id], "overlap": hits,
        "agreement": None,
    }, indent=2))
    # Only hub-chosen text and validated identifiers/patterns go in the kickoff;
    # agents' free-text descriptions are not repeated here.
    for target in (other["agent_id"], agent.id):
        chats.post_as_hub(
            conn, "project", project, to=target, meta={"conflict_id": cid},
            body=(f"[coordination #{cid}] {names[0]} (intent #{other['id']}) and {names[1]} (intent #{intent_id}) "
                  f"plan overlapping work: {overlap_text}. Agree on a split or an order in the project chat "
                  f"(chat_post(chat='project:{project}', to=<the other agent>, body=...)). When you agree, record it with "
                  f"board_put(key='{board_key}', expected_version=<current>) setting status 'agreed' and the "
                  "agreement text. Until then, avoid editing the overlapping paths."),
        )
    events.emit(conn, type="intent.conflict", actor=HUB_ACTOR, project=project,
                payload={"conflict_id": cid, "agents": names, "intents": [other["id"], intent_id], "overlap": hits,
                         "channel": channel, "board_key": board_key})
    return {"conflict_id": cid, "other_agent": other["agent_name"], "other_intent": other["id"],
            "other_patterns": other["patterns"], "overlap": hits, "channel": channel, "board_key": board_key}


def clear(conn: sqlite3.Connection, agent: Agent, intent_id: int, *, device: str | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()
    if row is None:
        raise NotFound(f"no intent {intent_id}")
    if row["agent_id"] != agent.id:
        raise Forbidden("only the agent that announced an intent can clear it")
    conn.execute("UPDATE intents SET cleared_at = COALESCE(cleared_at, ?) WHERE id = ?", (now_iso(), intent_id))
    events.emit(conn, type="intent.cleared", actor=agent.id, project=row["project"], device=device,
                payload={"intent_id": intent_id})
    return {"intent_id": intent_id, "cleared": True}
