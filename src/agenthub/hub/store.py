"""Domain operations over the hub database.

Each mutating function must be called inside :func:`db.transaction` and emits
an event for the change it makes. Functions raise the errors below; the API
layer maps them to HTTP status codes.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from agenthub import names, security
from agenthub.hub import avatar_art, events
from agenthub.hub.db import now_iso

ENROLLMENT_TTL = timedelta(minutes=15)

NAME_RE = names.NAME_RE
BOARD_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class Invalid(StoreError):
    pass


class Forbidden(StoreError):
    pass


@dataclass(frozen=True)
class Device:
    """A tailnet node, as reported by ``tailscale whois``."""

    node_id: str
    name: str
    ip: str


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    status: str
    device_node_id: str
    device_name: str
    device_ip: str
    allowed_projects: tuple[str, ...]
    token_hash: str

    @property
    def actor(self) -> str:
        return self.id

    def project(self, name: str | None) -> str | None:
        """The allowed project called ``name`` (ignoring case), by its stored name; None if not allowed."""
        if not isinstance(name, str):
            return None
        return next((p for p in self.allowed_projects if names.same(p, name)), None)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "device": {"node_id": self.device_node_id, "name": self.device_name, "ip": self.device_ip},
            "allowed_projects": list(self.allowed_projects),
        }


def check_name(kind: str, name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Invalid(f"invalid {kind} name {name!r}: use letters, digits, '-', '_' (max 64), starting with a letter "
                      "or digit")
    return name


def check_board_key(key: str) -> str:
    if not BOARD_KEY_RE.fullmatch(key) or ".." in key.split("/") or "//" in key:
        raise Invalid(f"invalid board key {key!r}")
    return key


# --- projects -----------------------------------------------------------------


def project_name(conn: sqlite3.Connection, name: str) -> str | None:
    """The project's name as stored, looked up ignoring case; None if there is no such project."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    row = conn.execute("SELECT name FROM projects WHERE name_key = ?", (names.key(name),)).fetchone()
    return row["name"] if row else None


def require_project_name(conn: sqlite3.Connection, name: str) -> str:
    """:func:`project_name`, or NotFound."""
    found = project_name(conn, name)
    if found is None:
        raise NotFound(f"no project {name!r}")
    return found


def ensure_project(conn: sqlite3.Connection, name: str, *, actor: str, description: str = "") -> bool:
    """Create the project if no project has this name (ignoring case). Returns True if it was created."""
    return _ensure_project(conn, name, actor=actor, description=description)[1]


def _ensure_project(conn: sqlite3.Connection, name: str, *, actor: str, description: str = "") -> tuple[str, bool]:
    """``(the project's stored name, whether it was just created)``."""
    check_name("project", name)
    cur = conn.execute(
        "INSERT INTO projects (name, name_key, description, created_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (name, names.key(name), description, now_iso()),
    )
    if cur.rowcount:
        events.emit(conn, type="project.created", actor=actor, project=name, payload={"description": description})
        return name, True
    return require_project_name(conn, name), False


def create_project(conn: sqlite3.Connection, name: str, *, actor: str, description: str = "") -> str:
    """A new project; Conflict if one with this name (ignoring case) exists."""
    stored, created = _ensure_project(conn, name, actor=actor, description=description)
    if not created:
        raise Conflict(f"project {stored!r} already exists" + ("" if stored == name else " (names ignore case)"))
    return stored


def ensure_projects(conn: sqlite3.Connection, projects: list[str], *, actor: str) -> list[str]:
    """Each project, made if missing, by its stored name (so ``Golem`` given for ``golem`` becomes ``golem``), once."""
    out: list[str] = []
    for p in projects:
        stored = _ensure_project(conn, p, actor=actor)[0]
        if stored not in out:
            out.append(stored)
    return out


def set_project_description(conn: sqlite3.Connection, project: str, description: str, *, actor: str) -> None:
    """The operator's description of a project (no length limit)."""
    project = require_project_name(conn, project)
    conn.execute("UPDATE projects SET description = ? WHERE name = ?", (description, project))
    events.emit(conn, type="project.updated", actor=actor, project=project, payload={"description_changed": True})


def list_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY name")]


# --- agents and enrollment ----------------------------------------------------


def _row_to_agent(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        device_node_id=row["device_node_id"],
        device_name=row["device_name"],
        device_ip=row["device_ip"],
        allowed_projects=tuple(json.loads(row["allowed_projects"])),
        token_hash=row["token_hash"],
    )


def get_agent(conn: sqlite3.Connection, agent_id: str) -> Agent | None:
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return _row_to_agent(row) if row else None


def find_agent(conn: sqlite3.Connection, id_or_name: str) -> Agent:
    """An agent by id, or by name ignoring case."""
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (id_or_name,)).fetchone() or conn.execute(
        "SELECT * FROM agents WHERE name_key = ?", (names.key(str(id_or_name)),)).fetchone()
    if not row:
        raise NotFound(f"no agent {id_or_name!r}")
    return _row_to_agent(row)


def list_agents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        info = _row_to_agent(r).public()
        info["created_at"] = r["created_at"]
        info["last_heartbeat"] = r["last_heartbeat"]
        out.append(info)
    return out


def create_enrollment(
    conn: sqlite3.Connection,
    *,
    created_by: str,
    projects: list[str],
    now: datetime | None = None,
) -> str:
    """Create a one-time enrollment code. Only its hash is stored."""
    now = now or datetime.now(UTC)
    projects = ensure_projects(conn, projects, actor=created_by)
    code = security.new_enrollment_code()
    expires = now + ENROLLMENT_TTL
    conn.execute(
        "INSERT INTO enrollments (code_hash, created_by, created_at, expires_at, allowed_projects) "
        "VALUES (?, ?, ?, ?, ?)",
        (security.hash_code(code), created_by, now_iso(now), now_iso(expires), json.dumps(projects)),
    )
    events.emit(
        conn,
        type="enrollment.created",
        actor=created_by,
        payload={"expires_at": now_iso(expires), "projects": projects},
    )
    return code


def redeem_enrollment(
    conn: sqlite3.Connection,
    *,
    code: str,
    name: str,
    device: Device,
    now: datetime | None = None,
) -> tuple[Agent, str]:
    """Exchange a valid, unused, unexpired code for a new pending agent and its token.

    The UPDATE is the single-use guard: only one caller can flip ``used_at``
    from NULL, even under concurrency.
    """
    check_name("agent", name)
    now = now or datetime.now(UTC)
    agent_id = security.new_agent_id()
    row = conn.execute(
        "UPDATE enrollments SET used_at = ?, used_by_agent = ? "
        "WHERE code_hash = ? AND used_at IS NULL AND expires_at > ? "
        "RETURNING allowed_projects",
        (now_iso(now), agent_id, security.hash_code(code), now_iso(now)),
    ).fetchone()
    if row is None:
        # Deliberately one message for unknown, used, and expired codes.
        raise Forbidden("invalid or expired enrollment code")
    taken = conn.execute("SELECT name FROM agents WHERE name_key = ?", (names.key(name),)).fetchone()
    if taken:
        raise Conflict(f"agent name {name!r} is already taken"
                       + ("" if taken["name"] == name else f" (by {taken['name']!r}; names ignore case)"))
    token = security.new_token(agent_id)
    conn.execute(
        "INSERT INTO agents (id, name, name_key, device_name, device_ip, device_node_id, token_hash, status, "
        "allowed_projects, created_at, avatar_version, avatar_seed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (
            agent_id,
            name,
            names.key(name),
            device.name,
            device.ip,
            device.node_id,
            security.hash_token(token),
            row["allowed_projects"],
            now_iso(now),
            avatar_art.GENERATOR_VERSION,
            avatar_art.new_seed(),
        ),
    )
    events.emit(
        conn,
        type="agent.enrolled",
        actor=agent_id,
        device=device.name,
        payload={"name": name, "device": device.__dict__, "projects": json.loads(row["allowed_projects"])},
    )
    agent = get_agent(conn, agent_id)
    assert agent is not None
    return agent, token


def set_agent_status(conn: sqlite3.Connection, id_or_name: str, status: str, *, actor: str) -> Agent:
    if status not in ("active", "revoked"):
        raise Invalid(f"invalid status {status!r}")
    agent = find_agent(conn, id_or_name)
    if agent.status == "revoked":
        raise Conflict("revoked agents cannot be changed; enroll a new agent instead")
    conn.execute("UPDATE agents SET status = ? WHERE id = ?", (status, agent.id))
    events.emit(conn, type=f"agent.{'approved' if status == 'active' else 'revoked'}", actor=actor,
                payload={"agent_id": agent.id, "name": agent.name})
    if status == "revoked":  # a revoked implementer must not hold a task in in_progress
        from agenthub.hub import task_agents

        for r in conn.execute(
            "SELECT ta.task_id FROM task_agents ta JOIN tasks t ON t.id = ta.task_id "
            "WHERE ta.agent_id = ? AND ta.stage = 'works' AND t.state = 'in_progress'", (agent.id,)
        ).fetchall():
            task_agents.maybe_advance(conn, r["task_id"], actor=actor)
    return find_agent(conn, agent.id)


def set_agent_projects(conn: sqlite3.Connection, id_or_name: str, projects: list[str], *, actor: str) -> Agent:
    agent = find_agent(conn, id_or_name)
    projects = ensure_projects(conn, projects, actor=actor)
    conn.execute("UPDATE agents SET allowed_projects = ? WHERE id = ?", (json.dumps(projects), agent.id))
    events.emit(conn, type="agent.projects_changed", actor=actor,
                payload={"agent_id": agent.id, "projects": projects})
    return find_agent(conn, agent.id)


# --- messages -----------------------------------------------------------------


def _channel_id(conn: sqlite3.Connection, project: str, channel: str, *, actor: str) -> int:
    check_name("channel", channel)
    row = conn.execute("SELECT id FROM channels WHERE project = ? AND name = ?", (project, channel)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO channels (project, name, created_by, created_at) VALUES (?, ?, ?, ?)",
        (project, channel, actor, now_iso()),
    )
    events.emit(conn, type="channel.created", actor=actor, project=project, payload={"channel": channel})
    return cur.lastrowid


def post_message(
    conn: sqlite3.Connection,
    *,
    author: Agent | str,
    project: str,
    channel: str,
    body: str,
    to: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    actor = author.actor if isinstance(author, Agent) else author
    to_agent = None
    if to is not None:
        target = find_agent(conn, to)
        if target.status != "active" or project not in target.allowed_projects:
            raise Invalid(f"agent {to!r} is not an active member of project {project!r}")
        to_agent = target.id
    channel_id = _channel_id(conn, project, channel, actor=actor)
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO messages (project, channel_id, author, to_agent, body, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (project, channel_id, actor, to_agent, body, ts),
    )
    msg_id = cur.lastrowid
    events.emit(
        conn,
        type="message.posted",
        actor=actor,
        project=project,
        device=device,
        payload={"message_id": msg_id, "channel": channel, "to": to_agent, "body": body},
    )
    return {"id": msg_id, "project": project, "channel": channel, "to": to_agent, "ts": ts}


_MESSAGE_SELECT = (
    "SELECT m.id, m.project, c.name AS channel, m.author, a.name AS author_name, "
    "m.to_agent, m.body, m.ts FROM messages m "
    "JOIN channels c ON c.id = m.channel_id LEFT JOIN agents a ON a.id = m.author "
)


def _inbox_where(agent: Agent, projects: list[str], addressed_only: bool) -> tuple[str, list[Any]]:
    placeholders = ",".join("?" * len(projects))
    addressed = "m.to_agent = ?" if addressed_only else "(m.to_agent IS NULL OR m.to_agent = ?)"
    where = (
        f"WHERE m.project IN ({placeholders}) AND m.author != ? "
        f"AND {addressed} "
        "AND m.id > COALESCE((SELECT last_read_id FROM inbox_cursors "
        "  WHERE agent_id = ? AND project = m.project), 0) "
    )
    return where, [*projects, agent.id, agent.id, agent.id]


def read_inbox(
    conn: sqlite3.Connection,
    agent: Agent,
    *,
    projects: list[str],
    limit: int = 50,
    mark_read: bool = True,
    addressed_only: bool = False,
) -> dict[str, Any]:
    """Unread messages. ``addressed_only`` (used by the Stop hook) never marks read,
    because advancing the cursor would also skip unaddressed messages."""
    if not projects:
        return {"messages": [], "more": False}
    if addressed_only:
        mark_read = False
    where, params = _inbox_where(agent, projects, addressed_only)
    rows = conn.execute(_MESSAGE_SELECT + where + "ORDER BY m.id LIMIT ?", [*params, limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    messages = [dict(r) | {"addressed_to_me": r["to_agent"] == agent.id} for r in rows]
    if mark_read and messages:
        # Rows are ordered by id and the truncated tail has higher ids than
        # everything returned, so the per-project max is a safe cursor.
        latest: dict[str, int] = {}
        for m in messages:
            latest[m["project"]] = max(latest.get(m["project"], 0), m["id"])
        for project, last_id in latest.items():
            conn.execute(
                "INSERT INTO inbox_cursors (agent_id, project, last_read_id) VALUES (?, ?, ?) "
                "ON CONFLICT (agent_id, project) DO UPDATE SET last_read_id = "
                "MAX(last_read_id, excluded.last_read_id)",
                (agent.id, project, last_id),
            )
    return {"messages": messages, "more": more}


def recent_messages(
    conn: sqlite3.Connection, *, project: str, channel: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    sql = _MESSAGE_SELECT + "WHERE m.project = ? "
    params: list[Any] = [project]
    if channel is not None:  # by channel id, so a quiet board in a busy project is one index range
        sql += "AND m.channel_id = (SELECT id FROM channels WHERE project = ? AND name = ?) "
        params += [project, channel]
    rows = conn.execute(sql + "ORDER BY m.id DESC LIMIT ?", [*params, limit]).fetchall()
    return [dict(r) for r in reversed(rows)]


# --- board --------------------------------------------------------------------


def _board_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["updated_by_name"] = out.pop("updated_by_name", None)
    return out


_BOARD_SELECT = (
    "SELECT b.project, b.key, b.version, b.value, b.updated_by, a.name AS updated_by_name, b.updated_at "
    "FROM board b LEFT JOIN agents a ON a.id = b.updated_by "
)


def board_get(conn: sqlite3.Connection, project: str, key: str) -> dict[str, Any]:
    check_board_key(key)
    row = conn.execute(_BOARD_SELECT + "WHERE b.project = ? AND b.key = ?", (project, key)).fetchone()
    if row is None:
        raise NotFound(f"no board key {key!r} in project {project!r}")
    return _board_row(row)


def board_list(conn: sqlite3.Connection, project: str, prefix: str = "") -> list[dict[str, Any]]:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT key, version, updated_by, updated_at FROM board "
        "WHERE project = ? AND key LIKE ? ESCAPE '\\' ORDER BY key",
        (project, escaped + "%"),
    ).fetchall()
    return [dict(r) for r in rows]


def board_put(
    conn: sqlite3.Connection,
    *,
    actor: str,
    project: str,
    key: str,
    value: str,
    expected_version: int,
    device: str | None = None,
) -> dict[str, Any]:
    """Optimistic concurrency: write only if the stored version equals ``expected_version``.

    ``expected_version=0`` means "create; the key must not exist yet".
    """
    check_board_key(key)
    if expected_version < 0:
        raise Invalid("expected_version must be >= 0")
    ts = now_iso()
    if expected_version == 0:
        cur = conn.execute(
            "INSERT INTO board (project, key, version, value, updated_by, updated_at) "
            "VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT DO NOTHING",
            (project, key, value, actor, ts),
        )
    else:
        cur = conn.execute(
            "UPDATE board SET version = version + 1, value = ?, updated_by = ?, updated_at = ? "
            "WHERE project = ? AND key = ? AND version = ?",
            (value, actor, ts, project, key, expected_version),
        )
    if cur.rowcount == 0:
        row = conn.execute("SELECT version FROM board WHERE project = ? AND key = ?", (project, key)).fetchone()
        current = row["version"] if row else 0
        raise Conflict(
            f"version conflict on {key!r}: expected {expected_version}, current {current}",
            current_version=current,
        )
    new_version = expected_version + 1
    events.emit(
        conn,
        type="board.updated",
        actor=actor,
        project=project,
        device=device,
        payload={"key": key, "version": new_version, "value": value},
    )
    return {"project": project, "key": key, "version": new_version, "updated_at": ts}
