"""Agents wake agents (0.3.9.1): wake chains, loop protection, the pause switches and wake records.

An agent's message in a chat is delivered to its recipients the way the operator's is (:func:`chats.post`): one
``chat_deliveries`` row each, which the recipient's runner takes into a running session or wakes it for. This module
holds what is particular to agents waking agents:

* **Chains.** Every delivery of an agent's message carries a chain id. An agent's post joins the chain of the latest
  message handed to it, when that was another agent's (less than :data:`CHAIN_TTL` ago); otherwise it starts a new
  chain. The operator's messages belong to no chain, so work the operator starts begins fresh.
* **Loop protection.** ``wake_chains.wakes`` counts the waking deliveries since the chain last *landed* (an agent in
  it made, moved, finished or reviewed a task, or declined a request: :data:`LANDING_EVENTS`, hooked in
  :func:`events.emit`). More than ``[wakes] loop_limit`` (hub.toml; 0 = off) pauses the chain and asks the operator
  with a ``wake_loop`` approval. Approving resumes it; only the operator decides.
* **Pause switches.** Per agent (``agents.wakes_paused``) and hub-wide (``hub_settings`` ``wakes_paused``), set by the
  operator only (routes under ``/ui/api``). A paused agent still receives messages (into a running session, or
  through read_inbox) but no agent's message wakes it. The operator's messages always wake, except (0.3.10) one to
  every agent on a device (a device chat, :mod:`broadcasts`): that one respects both switches too.
* **Records.** A wake record is a delivery of an agent's message (or one of the operator's that woke its agent): who
  woke whom, in which chat and chain, and when. The chat shows a status line when an agent's message wakes another.

Whether a delivery may wake is worked out when it is read (:func:`chats.pending_deliveries` reports it as ``wake``),
so lifting a pause lets what waited wake then.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from agenthub.config import DEFAULT_WAKE_LOOP_LIMIT
from agenthub.hub import db, events, stalls
from agenthub.hub.dashboard import Conn, Op, PostOp  # the dashboard's operator checks
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Forbidden, NotFound, find_agent

SYSTEM = "system:hub"
CHAIN_TTL = timedelta(hours=2)
# Status lines only the hub writes (a runner's stream may not: it could forge "X woke Y").
HUB_STATES = ("woke", "wake_declined", "loop_paused", "loop_resumed", "reopened", "no_verdict")
DECLINE_REASONS = ("not_in_project", "not_in_chat", "pending", "revoked")
# What an agent does that counts as its chain landing: real progress, or an answer (checked in events.emit).
LANDING_EVENTS = events.LANDING_EVENTS
LOOP_LAST_MESSAGES = 5
MAX_RECORDS = 200

# Set from hub.toml by the app (``[wakes] loop_limit``); 0 turns loop protection off.
_loop_limit = DEFAULT_WAKE_LOOP_LIMIT


def configure(loop_limit: int) -> None:
    global _loop_limit
    _loop_limit = loop_limit


def loop_limit() -> int:
    return _loop_limit


# --- pause switches ----------------------------------------------------------------------------------------------


def hub_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM hub_settings WHERE key = 'wakes_paused'").fetchone()
    return row is not None and row["value"] == "1"


def set_hub_paused(conn: sqlite3.Connection, paused: bool, *, actor: str) -> dict[str, Any]:
    """The operator pauses (or resumes) every agent-to-agent wake on the hub."""
    _operator_only(actor)
    conn.execute("INSERT INTO hub_settings (key, value, updated_by, updated_at) VALUES ('wakes_paused', ?, ?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, "
                 "updated_at = excluded.updated_at", ("1" if paused else "0", actor, now_iso()))
    events.emit(conn, type="wakes.paused", actor=actor, payload={"agent_id": None, "paused": paused})
    if not paused:
        _redeliver(conn)
    return settings(conn)


def set_agent_paused(conn: sqlite3.Connection, ref: str, paused: bool, *, actor: str) -> dict[str, Any]:
    """The operator pauses (or resumes) other agents' wakes of one agent."""
    _operator_only(actor)
    agent = find_agent(conn, ref)
    conn.execute("UPDATE agents SET wakes_paused = ? WHERE id = ?", (int(paused), agent.id))
    events.emit(conn, type="wakes.paused", actor=actor, payload={"agent_id": agent.id, "paused": paused})
    if not paused:
        _redeliver(conn, "d.agent_id = ?", [agent.id])
    return agent_view(conn, agent.id)


def _operator_only(actor: str) -> None:
    if not actor.startswith("human:"):
        raise Forbidden("only a human operator can switch wakes")


def settings(conn: sqlite3.Connection) -> dict[str, Any]:
    paused = [chain_view(conn, r["id"]) for r in
              conn.execute("SELECT id FROM wake_chains WHERE status = 'paused' ORDER BY paused_at DESC LIMIT 50")]
    return {"paused": hub_paused(conn), "loop_limit": _loop_limit, "chains": paused}


# SQL for whether a delivery (d, its message m) may wake its agent now: the operator's always do, but for one to a
# whole device; an agent's (or that) not when it is a notice, its agent's or the hub's wakes are paused, or its chain is
# paused (an operator's message has no chain).
DEVICE_SQL = "EXISTS (SELECT 1 FROM chats dc WHERE dc.id = m.chat_id AND dc.kind = 'device')"
WAKE_SQL = (
    f"CASE WHEN m.author LIKE 'human:%' AND NOT {DEVICE_SQL} THEN 1 "
    "WHEN d.wake = 0 OR (SELECT wakes_paused FROM agents WHERE id = d.agent_id) = 1 "
    "OR EXISTS (SELECT 1 FROM hub_settings WHERE key = 'wakes_paused' AND value = '1') "
    "OR EXISTS (SELECT 1 FROM wake_chains w WHERE w.id = d.chain_id AND w.status = 'paused') THEN 0 ELSE 1 END"
)


def may_wake(conn: sqlite3.Connection, agent_id: str, chain_id: str | None) -> bool:
    """Whether a new delivery of an agent's message (or the operator's to a device) to ``agent_id`` in ``chain_id``
    would wake it."""
    row = conn.execute("SELECT wakes_paused FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None or row["wakes_paused"] or hub_paused(conn):
        return False
    if chain_id is None:
        return True
    chain = conn.execute("SELECT status FROM wake_chains WHERE id = ?", (chain_id,)).fetchone()
    return chain is None or chain["status"] != "paused"


def _redeliver(conn: sqlite3.Connection, where: str = "1 = 1", params: list[Any] | None = None) -> int:
    """Tell runners again about pending deliveries that a pause held back (agents' messages, the operator's to a device)
    and that may wake now (after a pause lifts)."""
    from agenthub.hub import chats

    rows = conn.execute(
        "SELECT d.id, d.agent_id, d.message_id, m.chat_id FROM chat_deliveries d "
        "JOIN chat_messages m ON m.id = d.message_id "
        f"WHERE d.status = 'pending' AND (m.author NOT LIKE 'human:%' OR {DEVICE_SQL}) AND ({WAKE_SQL}) = 1 "
        f"AND {where} ORDER BY d.id",
        params or []).fetchall()
    for r in rows:
        chats.announce_delivery(conn, r["chat_id"], r["message_id"], r["id"], r["agent_id"])
    return len(rows)


# --- chains ------------------------------------------------------------------------------------------------------


def current_chain(conn: sqlite3.Connection, agent_id: str) -> str | None:
    """The chain ``agent_id`` is working in: that of the latest message handed to it, if it was another agent's and
    recent; None after the operator's (or nothing recent)."""
    since = now_iso(datetime.now(UTC) - CHAIN_TTL)
    row = conn.execute(
        "SELECT d.chain_id, m.author FROM chat_deliveries d JOIN chat_messages m ON m.id = d.message_id "
        "WHERE d.agent_id = ? AND d.status = 'delivered' AND d.delivered_at >= ? "
        "ORDER BY d.delivered_at DESC, d.id DESC LIMIT 1", (agent_id, since)).fetchone()
    if row is None or row["author"].startswith("human:") or not row["chain_id"]:
        return None
    exists = conn.execute("SELECT 1 FROM wake_chains WHERE id = ?", (row["chain_id"],)).fetchone()
    return row["chain_id"] if exists else None


def chain_for(conn: sqlite3.Connection, agent: Agent) -> str:
    """The chain ``agent``'s new message belongs to: its current one, else a new one it starts."""
    found = current_chain(conn, agent.id)
    if found is not None:
        return found
    chain_id = "w" + secrets.token_hex(8)
    conn.execute("INSERT INTO wake_chains (id, started_by, started_at) VALUES (?, ?, ?)",
                 (chain_id, agent.id, now_iso()))
    return chain_id


def counted(conn: sqlite3.Connection, chain_id: str, n: int, *, chat_row: dict[str, Any], author: Agent) -> bool:
    """``n`` deliveries of ``author``'s new message in ``chat_row`` wake agents in ``chain_id``. Returns True when
    that took the chain over the loop limit and paused it (an approval asks the operator)."""
    if n <= 0:
        return False
    conn.execute("UPDATE wake_chains SET wakes = wakes + ?, total = total + ? WHERE id = ?", (n, n, chain_id))
    chain = conn.execute("SELECT * FROM wake_chains WHERE id = ?", (chain_id,)).fetchone()
    if not _loop_limit or chain["status"] != "open" or chain["wakes"] <= _loop_limit:
        return False
    _pause_chain(conn, dict(chain), chat_row=chat_row, author=author)
    return True


def _pause_chain(conn: sqlite3.Connection, chain: dict[str, Any], *, chat_row: dict[str, Any],
                 author: Agent) -> None:
    from agenthub.hub import approvals, chats

    chain_id = chain["id"]
    now = now_iso()
    conn.execute("UPDATE wake_chains SET status = 'paused', paused_at = ? WHERE id = ?", (now, chain_id))
    agents: list[str] = []
    for r in conn.execute("SELECT m.author, d.agent_id FROM chat_deliveries d JOIN chat_messages m "
                          "ON m.id = d.message_id WHERE d.chain_id = ? ORDER BY d.id", (chain_id,)):
        for a in (r["author"], r["agent_id"]):
            if a not in agents and not a.startswith(("human:", "system:")):
                agents.append(a)
    last = [r["message_id"] for r in conn.execute(
        "SELECT DISTINCT message_id FROM chat_deliveries WHERE chain_id = ? ORDER BY message_id DESC LIMIT ?",
        (chain_id, LOOP_LAST_MESSAGES))]
    title = chats.chat_title(conn, chat_row["kind"], chat_row["ref"])
    details: dict[str, Any] = {
        "chain_id": chain_id, "chat_id": chat_row["id"],
        "chat": {"kind": chat_row["kind"], "ref": chat_row["ref"], "title": title},
        "agents": agents, "wakes": chain["wakes"], "limit": _loop_limit, "messages": last,
    }
    if chat_row["kind"] == "task":
        details["task_id"] = int(chat_row["ref"])
    approval = approvals.create(
        conn, kind="wake_loop", requester=author.id, project=chat_row["project"], details=details,
        reason=(f"Agents woke each other {chain['wakes']} times in chain {chain_id} with nothing landing "
                f"(loop_limit {_loop_limit}). Approve to let the chain wake agents again; reject to keep it "
                "paused."))
    conn.execute("UPDATE wake_chains SET approval_id = ? WHERE id = ?", (approval["id"], chain_id))
    chats.hub_status(conn, chat_row, {"state": "loop_paused", "chain_id": chain_id, "approval_id": approval["id"],
                                      "wakes": chain["wakes"], "limit": _loop_limit})


def on_decision(conn: sqlite3.Connection, approval: dict[str, Any], approve: bool, actor: str) -> None:
    """The operator decided a ``wake_loop`` approval: approve resumes the chain from zero; reject leaves it paused."""
    from agenthub.hub import chats

    if not approve:
        return
    details = approval["details"]
    chain_id = details.get("chain_id")
    cur = conn.execute("UPDATE wake_chains SET status = 'open', wakes = 0, paused_at = NULL WHERE id = ? "
                       "AND status = 'paused'", (chain_id,))
    if not cur.rowcount:
        return
    chat_id = details.get("chat_id")
    if isinstance(chat_id, int) and conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone():
        chats.hub_status(conn, chats.chat_row(conn, chat_id),
                         {"state": "loop_resumed", "chain_id": chain_id, "approval_id": approval["id"]})
    events.emit(conn, type="wake.chain", actor=actor, payload={"chain_id": chain_id, "status": "open"})
    _redeliver(conn, "d.chain_id = ?", [chain_id])


def land(conn: sqlite3.Connection, agent_id: str) -> None:
    """``agent_id`` did something that lands: its chain's wake count starts again (a paused chain stays paused)."""
    chain_id = current_chain(conn, agent_id)
    if chain_id is not None:
        conn.execute("UPDATE wake_chains SET wakes = 0, landed_at = ? WHERE id = ?", (now_iso(), chain_id))


def chain_view(conn: sqlite3.Connection, chain_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM wake_chains WHERE id = ?", (chain_id,)).fetchone()
    if row is None:
        raise NotFound(f"no wake chain {chain_id!r}")
    return dict(row)


def store_run_chains(conn: sqlite3.Connection, table: str, run_id: int, chain_ids: list[str] | None) -> None:
    """The wake chains a session took in, as its runner reported them, on its run row (chat_runs or task_runs)."""
    if not chain_ids or table not in ("chat_runs", "task_runs"):
        return
    known = [c for c in dict.fromkeys(chain_ids)
             if conn.execute("SELECT 1 FROM wake_chains WHERE id = ?", (c,)).fetchone()]
    if known:
        conn.execute(f"UPDATE {table} SET chain_ids = ? WHERE id = ?", (json.dumps(known), run_id))


# --- records -----------------------------------------------------------------------------------------------------


def records(conn: sqlite3.Connection, *, agent_id: str | None = None, chain_id: str | None = None,
            woke_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    """Wake records, newest first: deliveries of agents' messages, and the operator's that woke their agent."""
    from agenthub.hub import chats

    where = ["(m.author NOT LIKE 'human:%' OR d.mode = 'woke')", "m.author != ?"]
    params: list[Any] = [SYSTEM]
    if agent_id is not None:
        where.append("d.agent_id = ?")
        params.append(agent_id)
    if chain_id is not None:
        where.append("d.chain_id = ?")
        params.append(chain_id)
    if woke_only:
        where.append("d.mode = 'woke'")
    rows = conn.execute(
        "SELECT d.id, d.message_id, d.chain_id, d.agent_id, a.name AS agent_name, m.author, wa.name AS author_name, "
        "c.kind, c.ref, d.created_at, d.mode, d.delivered_at, d.wake FROM chat_deliveries d "
        "JOIN chat_messages m ON m.id = d.message_id JOIN chats c ON c.id = m.chat_id "
        "JOIN agents a ON a.id = d.agent_id LEFT JOIN agents wa ON wa.id = m.author "
        f"WHERE {' AND '.join(where)} ORDER BY d.id DESC LIMIT ?",
        [*params, max(1, min(limit, MAX_RECORDS))]).fetchall()
    return [{"delivery_id": r["id"], "message_id": r["message_id"], "chain_id": r["chain_id"],
             "waker": {"id": r["author"], "name": r["author_name"]},
             "woken": {"id": r["agent_id"], "name": r["agent_name"]},
             "chat": {"kind": r["kind"], "ref": r["ref"], "title": chats.chat_title(conn, r["kind"], r["ref"])},
             "created_at": r["created_at"], "mode": r["mode"], "delivered_at": r["delivered_at"],
             "notice": not r["wake"], "operator": r["author"].startswith("human:")} for r in rows]


AWAKE_STATES = stalls.AWAKE_STATES  # a status line's states while its session runs


def awake(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Whether the agent has a session running, as its latest status line (in any chat) says."""
    row = conn.execute("SELECT meta FROM chat_messages WHERE author = ? AND kind = 'status' ORDER BY id DESC LIMIT 1",
                       (agent_id,)).fetchone()
    return row is not None and json.loads(row["meta"]).get("state") in AWAKE_STATES


def agent_view(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    """What the dashboard shows of one agent's wakes: its switch, the hub's, whether it is awake, who woke it."""
    agent = find_agent(conn, ref)
    paused = conn.execute("SELECT wakes_paused FROM agents WHERE id = ?", (agent.id,)).fetchone()["wakes_paused"]
    woke = records(conn, agent_id=agent.id, woke_only=True, limit=1)
    return {"agent_id": agent.id, "name": agent.name, "paused": bool(paused), "hub_paused": hub_paused(conn),
            "awake": awake(conn, agent.id), "woken_by": woke[0] if woke else None,
            "recent": records(conn, agent_id=agent.id, limit=20)}


# --- operator routes ---------------------------------------------------------------------------------------------


class PauseBody(BaseModel):
    paused: bool


def build_router() -> APIRouter:
    """Operator-only JSON for the dashboard, under /ui so the dashboard's rules cover it (Tailscale identity, never
    an agent token, CSRF on writes, the agents' guard hook)."""
    router = APIRouter(prefix="/ui/api")

    @router.get("/wakes")
    def get_settings(operator: Op, conn: Conn) -> dict[str, Any]:
        return settings(conn)

    @router.put("/wakes")
    def put_settings(body: PauseBody, operator: PostOp, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return set_hub_paused(conn, body.paused, actor=operator.actor)

    @router.get("/wakes/records")
    def get_records(operator: Op, conn: Conn, agent: str | None = None, chain: str | None = None,
                    limit: Annotated[int, Query(ge=1, le=MAX_RECORDS)] = 50) -> dict[str, Any]:
        agent_id = find_agent(conn, agent).id if agent else None
        return {"wakes": records(conn, agent_id=agent_id, chain_id=chain, limit=limit)}

    @router.get("/agents/{name}/wakes")
    def get_agent(name: str, operator: Op, conn: Conn) -> dict[str, Any]:
        return agent_view(conn, name)

    @router.put("/agents/{name}/wakes")
    def put_agent(name: str, body: PauseBody, operator: PostOp, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            return set_agent_paused(conn, name, body.paused, actor=operator.actor)

    return router

