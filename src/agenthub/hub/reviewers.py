"""Project reviewers (0.3.9.1): reviewers are always there.

The operator marks some of a project's agents as its reviewers (``project_reviewers``; operator-only routes
``GET``/``PUT /ui/api/projects/{name}/reviewers``, under ``/ui`` so the dashboard's rules cover them: identity from
Tailscale, never an agent token, CSRF on the write). When a task enters review with no active reviewer, the hub adds
the project's reviewers to it (:func:`fill`, called from ``tasks.transition``): each one that is active, still allowed
into the project and not already on the task (an implementer never reviews its own task).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, StringConstraints

from agenthub.hub import db, events
from agenthub.hub.dashboard import Conn, Op, PostOp  # the dashboard's operator checks
from agenthub.hub.db import now_iso
from agenthub.hub.store import Invalid, NotFound, find_agent

ROLE = "Project reviewer"  # the role the hub gives a reviewer it adds; written by the hub, not by an agent
MAX_REVIEWERS = 50
SYSTEM = "system:hub"


def project_name(conn: sqlite3.Connection, name: str) -> str:
    """The stored spelling of a project (names ignore case); NotFound for an unknown one."""
    row = conn.execute("SELECT name FROM projects WHERE lower(name) = lower(?) ORDER BY name = ? DESC LIMIT 1",
                       (name, name)).fetchone()
    if row is None:
        raise NotFound(f"no project {name!r}")
    return row["name"]


def list_reviewers(conn: sqlite3.Connection, project: str) -> list[dict[str, Any]]:
    """``[{agent_id, name, status, allowed}]`` in the order they were marked. ``allowed``: the agent may still use
    the project (a reviewer whose access was taken away stays listed but is never added to a task)."""
    project = project_name(conn, project)
    rows = conn.execute(
        "SELECT r.agent_id, a.name, a.status, a.allowed_projects FROM project_reviewers r "
        "JOIN agents a ON a.id = r.agent_id WHERE r.project = ? ORDER BY r.added_at, r.rowid", (project,))
    return [{"agent_id": r["agent_id"], "name": r["name"], "status": r["status"],
             "allowed": project in json.loads(r["allowed_projects"])} for r in rows]


def set_reviewers(conn: sqlite3.Connection, project: str, agents: list[str], *, actor: str) -> list[dict[str, Any]]:
    """The operator replaces a project's reviewers with ``agents`` (ids or names). Each must be an agent allowed into
    the project and not revoked; an empty list clears them."""
    project = project_name(conn, project)
    if len(agents) > MAX_REVIEWERS:
        raise Invalid(f"at most {MAX_REVIEWERS} reviewers")
    ids: list[str] = []
    for ref in agents:
        agent = find_agent(conn, ref)
        if agent.status == "revoked" or project not in agent.allowed_projects:
            raise Invalid(f"agent {agent.name!r} is not an agent of project {project!r}")
        if agent.id not in ids:
            ids.append(agent.id)
    kept = {r["agent_id"] for r in conn.execute("SELECT agent_id FROM project_reviewers WHERE project = ?",
                                                (project,))}
    for gone in kept - set(ids):
        conn.execute("DELETE FROM project_reviewers WHERE project = ? AND agent_id = ?", (project, gone))
    ts = now_iso()
    for agent_id in ids:
        if agent_id not in kept:
            conn.execute("INSERT INTO project_reviewers (project, agent_id, added_by, added_at) VALUES (?, ?, ?, ?)",
                         (project, agent_id, actor, ts))
    events.emit(conn, type="project.reviewers", actor=actor, project=project,
                payload={"project": project, "agents": ids})
    return list_reviewers(conn, project)


def fill(conn: sqlite3.Connection, task: dict[str, Any]) -> list[str]:
    """A task entering review with no active reviewer gets its project's reviewers; the ids added."""
    from agenthub.hub import task_agents  # task_agents imports tasks, which calls this

    if any(m["stage"] == "reviews" and m["status"] == "active" for m in task["agents"]):
        return []
    on_task = {m["agent_id"] for m in task["agents"]}
    added = []
    for r in list_reviewers(conn, task["project"]):
        if r["status"] != "active" or not r["allowed"] or r["agent_id"] in on_task:
            continue
        task_agents.add_agent(conn, task["id"], r["agent_id"], role=ROLE, stage="reviews", actor=SYSTEM)
        added.append(r["agent_id"])
    return added


# --- operator routes ------------------------------------------------------------------------------------------------


class ReviewersRequest(BaseModel):
    reviewers: list[Annotated[str, StringConstraints(max_length=64)]] = Field(max_length=MAX_REVIEWERS)


def build_router() -> APIRouter:
    router = APIRouter(prefix="/ui/api")

    @router.get("/projects/{name}/reviewers")
    def get_reviewers(name: str, operator: Op, conn: Conn) -> dict[str, Any]:
        project = project_name(conn, name)
        return {"project": project, "reviewers": list_reviewers(conn, project)}

    @router.put("/projects/{name}/reviewers")
    def put_reviewers(name: str, body: ReviewersRequest, operator: PostOp, conn: Conn) -> dict[str, Any]:
        with db.transaction(conn):
            project = project_name(conn, name)
            found = set_reviewers(conn, project, body.reviewers, actor=operator.actor)
        return {"project": project, "reviewers": found}

    return router
