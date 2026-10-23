"""Reports agents publish for the operator (the ``publish_report`` tool), listed on the Reports page.

A report is one of two kinds:

* a **Claude page**: a link to a page the agent made on claude.ai (a Claude Doc, an artifact). The hub keeps the
  link, not the page: claude.ai pages need the operator's claude.ai login and won't render in another site's frame,
  so the hub shows a card that opens it there. Only https links on claude.ai and claude.site are taken.
* an **HTML page**: a self-contained page the agent wrote (inline CSS and scripts, inline SVG or script-drawn charts,
  ``data:`` images). The hub stores it and the report page shows it in a sandboxed frame served from its own URL with
  a CSP of its own (:data:`FRAME_HEADERS`): its scripts run in an opaque origin, so they can't read or act on the hub,
  and nothing is fetched from the network.

Each has a title, an optional Markdown summary and an optional task in the same project. Its author may update it
(same id); nobody else can. ``report.*`` events carry the id, title, kind and task, never the HTML.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlsplit

from agenthub.hub import events, tasks
from agenthub.hub.db import now_iso
from agenthub.hub.store import Agent, Conflict, Forbidden, Invalid, NotFound, require_project_name

MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 4000
MAX_URL_CHARS = 2000
MAX_HTML_CHARS = 2_000_000  # room for a few data: images; a bigger page is better as a Claude page
MAX_REQUEST_BYTES = 5 * 1024 * 1024  # the publish request's own body limit (JSON-escaped HTML grows)
MAX_REPORTS_PER_AGENT = 500
# Hosts a Claude page link may point to: claude.ai (and its subdomains, e.g. preview.claude.ai) and claude.site.
CLAUDE_HOSTS = ("claude.ai", "claude.site")

# The HTML page's own response. The CSP ``sandbox`` directive gives it an opaque origin even when opened on its
# own (not in the report page's frame), so its scripts never run as the hub: no cookies, no hub DOM, no same-origin
# requests. ``default-src 'none'`` lets nothing load from anywhere (no fetch, no external images, fonts, scripts or
# frames): only what the page carries inline and as data: URLs. ``'unsafe-eval'`` adds nothing inline scripts can't
# already do inside that box, and some chart libraries need it. Links open in a new tab, outside the sandbox.
SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox"
FRAME_HEADERS = {
    "content-security-policy": (
        f"sandbox {SANDBOX}; default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; "
        "frame-ancestors 'self'; form-action 'none'; base-uri 'none'"
    ),
    "x-frame-options": "SAMEORIGIN",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
}

# Everything but the HTML, which only the page's frame (and the agent API's get) reads.
_COLUMNS = ("r.id, r.project, r.task_id, r.agent_id, r.title, r.summary, r.url, r.html IS NOT NULL AS has_html, "
            "r.created_at, r.updated_at, a.name AS agent_name, t.title AS task_title")
_FROM = "FROM reports r LEFT JOIN agents a ON a.id = r.agent_id LEFT JOIN tasks t ON t.id = r.task_id "


def _row(row: sqlite3.Row) -> dict[str, Any]:
    r = dict(row)
    r["kind"] = "html" if r.pop("has_html") else "claude"
    r["host"] = urlsplit(r["url"]).hostname if r["url"] else None  # what the link card says it opens
    return r


def check_url(url: str) -> str:
    """A Claude page link: https, on claude.ai or claude.site (or a subdomain), no credentials or odd ports."""
    url = url.strip()
    if not url or len(url) > MAX_URL_CHARS or any(c.isspace() or ord(c) < 32 for c in url):
        raise Invalid(f"a Claude page link must be one https URL of at most {MAX_URL_CHARS} characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise Invalid(f"not a valid URL: {exc}") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or port not in (None, 443):
        raise Invalid("a Claude page link must be a plain https URL")
    if not any(host == h or host.endswith("." + h) for h in CLAUDE_HOSTS):
        raise Invalid(f"a Claude page link must be on {' or '.join(CLAUDE_HOSTS)}, not {host or '?'}; "
                      "for anything else, publish a self-contained HTML page")
    return url


def get(conn: sqlite3.Connection, rid: int) -> dict[str, Any]:
    """A report without its HTML (``kind`` says whether it has one)."""
    row = conn.execute(f"SELECT {_COLUMNS} {_FROM} WHERE r.id = ?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"no report {rid}")
    return _row(row)


def html(conn: sqlite3.Connection, rid: int) -> str:
    """An HTML report's page; NotFound for a Claude page link."""
    row = conn.execute("SELECT html FROM reports WHERE id = ?", (rid,)).fetchone()
    if row is None or row["html"] is None:
        raise NotFound(f"report {rid} has no HTML page")
    return row["html"]


def get_for_agent(conn: sqlite3.Connection, rid: int, agent: Agent) -> dict[str, Any]:
    """A report in one of ``agent``'s projects, with its HTML; NotFound otherwise (others' aren't revealed)."""
    r = get(conn, rid)
    if r["project"] not in agent.allowed_projects:
        raise NotFound(f"no report {rid}")
    r["html"] = html(conn, rid) if r["kind"] == "html" else None
    return r


def list_reports(
    conn: sqlite3.Connection, *, projects: list[str] | None = None, task_id: int | None = None,
) -> list[dict[str, Any]]:
    """Reports (without HTML), newest first: all, those in ``projects``, or those on one task."""
    where, args = [], []
    if projects is not None:
        where.append(f"r.project IN ({','.join('?' * len(projects))})")
        args += projects
    if task_id is not None:
        where.append("r.task_id = ?")
        args.append(task_id)
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    rows = conn.execute(f"SELECT {_COLUMNS} {_FROM}{clause}ORDER BY r.created_at DESC, r.id DESC", args)
    return [_row(r) for r in rows]


def by_project(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The Reports page: one group per project, the project with the newest report first, newest first inside."""
    groups: dict[str, dict[str, Any]] = {}
    for r in list_reports(conn):
        groups.setdefault(r["project"], {"project": r["project"], "reports": []})["reports"].append(r)
    return list(groups.values())


def publish(
    conn: sqlite3.Connection, agent: Agent, *, project: str, title: str, summary: str = "", url: str | None = None,
    html: str | None = None, task_id: int | None = None, report_id: int | None = None, device: str | None = None,
) -> dict[str, Any]:
    """Publish a report, or (``report_id``) replace one ``agent`` published: its title, summary and page (a link
    or HTML); its task too when ``task_id`` is given. Returns the report without its HTML."""
    if agent.project(project) is None:
        raise Forbidden(f"no access to project {project!r}")
    project = require_project_name(conn, project)
    title = title.strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise Invalid(f"a report title must be 1 to {MAX_TITLE_CHARS} characters")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise Invalid(f"a report summary must be at most {MAX_SUMMARY_CHARS} characters")
    if (url is None) == (html is None):
        raise Invalid("give a report exactly one of: a Claude page link (url) or a self-contained HTML page (html)")
    if url is not None:
        url = check_url(url)
    elif not html.strip() or len(html) > MAX_HTML_CHARS:
        raise Invalid(f"a report's HTML page must be 1 to {MAX_HTML_CHARS} characters")
    if task_id is not None and tasks.get_task(conn, task_id)["project"] != project:
        raise Invalid(f"task {task_id} is not in project {project!r}")

    now = now_iso()
    if report_id is None:
        n = conn.execute("SELECT COUNT(*) FROM reports WHERE agent_id = ?", (agent.id,)).fetchone()[0]
        if n >= MAX_REPORTS_PER_AGENT:
            raise Conflict(f"you already have {n} reports; update one (report_id) instead of adding more")
        cur = conn.execute(
            "INSERT INTO reports (project, task_id, agent_id, title, summary, url, html, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (project, task_id, agent.id, title, summary, url, html, now, now))
        rid, kind = cur.lastrowid, "report.published"
    else:
        old = get(conn, report_id)
        if old["project"] not in agent.allowed_projects:
            raise NotFound(f"no report {report_id}")
        if old["agent_id"] != agent.id:
            raise Forbidden(f"report {report_id} was published by another agent; only its author can update it")
        if old["project"] != project:
            raise Invalid(f"report {report_id} is in project {old['project']!r}; a report can't move projects")
        conn.execute(
            "UPDATE reports SET task_id = ?, title = ?, summary = ?, url = ?, html = ?, updated_at = ? WHERE id = ?",
            (old["task_id"] if task_id is None else task_id, title, summary, url, html, now, report_id))
        rid, kind = report_id, "report.updated"
    r = get(conn, rid)
    events.emit(conn, type=kind, actor=agent.id, project=project, device=device,
                payload={"report_id": rid, "title": r["title"], "kind": r["kind"], "task_id": r["task_id"]})
    return r
