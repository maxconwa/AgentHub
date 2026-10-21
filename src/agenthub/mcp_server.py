"""stdio MCP server exposing the hub to Claude Code.

Written against the official MCP Python SDK v2 (``mcp`` 2.3.0, PyPI upload
2026-10-02; migration guide https://py.sdk.modelcontextprotocol.io/v2/migration/):
``FastMCP`` is now ``mcp.server.mcpserver.MCPServer``, ``@server.tool()`` must
be called, ``server.run()`` defaults to stdio, and ``ToolError`` messages are
returned to the client while other exceptions are reported generically.

Everything written by another agent is wrapped by :mod:`agenthub.untrusted`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agenthub import untrusted
from agenthub.client import HubClient, HubError

INSTRUCTIONS = f"""\
agenthub connects you to other Claude Code agents through a central hub.
Use `whoami` to see your projects, `read_inbox` to receive messages, `post`
to send them, and `board_get`/`board_put` for shared versioned documents.
{untrusted.NOTICE}
"""


def _format_message(m: dict[str, Any]) -> str:
    author = untrusted.author_label(m.get("author"), m.get("author_name"))
    return untrusted.wrap(
        m["body"],
        author=author,
        kind="message",
        id=m["id"],
        project=m["project"],
        channel=m["channel"],
        ts=m["ts"],
        to="you" if m.get("addressed_to_me") else None,
    )


def build_server(client_factory: Callable[[], HubClient]) -> MCPServer:
    server = MCPServer("agenthub", instructions=INSTRUCTIONS)

    async def call(fn: Callable[[HubClient], Awaitable[Any]]) -> Any:
        try:
            return await fn(client_factory())
        except HubError as exc:
            raise ToolError(f"hub error {exc.status}: {exc.detail}") from exc
        except httpx.TransportError as exc:
            raise ToolError(f"cannot reach hub: {exc.__class__.__name__}") from exc

    @server.tool()
    async def whoami() -> str:
        """Show this agent's identity, approval status, and the projects it may use."""
        me = await call(lambda c: c.me())
        return json.dumps(me, indent=2)

    @server.tool()
    async def post(project: str, body: str, channel: str = "general", to: str | None = None) -> str:
        """Post a message to a project channel. Set `to` (agent name) to address one agent."""
        r = await call(lambda c: c.post(project, body, channel=channel, to=to))
        target = f" (to {to})" if to else ""
        return f"Posted message #{r['id']} to {project}/{channel}{target}."

    @server.tool()
    async def read_inbox(project: str | None = None, limit: int = 20, peek: bool = False) -> str:
        """Fetch unread messages from your projects and mark them read (unless peek=true)."""
        r = await call(lambda c: c.inbox(project=project, limit=limit, peek=peek))
        if not r["messages"]:
            return "No unread messages."
        parts = [untrusted.NOTICE, *(_format_message(m) for m in r["messages"])]
        if r["more"]:
            parts.append("More unread messages remain; call read_inbox again.")
        return "\n\n".join(parts)

    @server.tool()
    async def search_history(query: str, project: str | None = None, limit: int = 10) -> str:
        """Full-text search over the event history of your projects."""
        results = await call(lambda c: c.search(query, project=project, limit=limit))
        if not results:
            return "No matches."
        blocks = [
            untrusted.wrap(
                json.dumps(e["payload"], indent=1),
                author=untrusted.author_label(e["actor"], None),
                kind=e["type"], event_id=e["id"], project=e["project"], ts=e["ts"],
            )
            for e in results
        ]
        return "\n\n".join([untrusted.NOTICE, *blocks])

    @server.tool()
    async def board_get(project: str, key: str | None = None, prefix: str = "") -> str:
        """Read a shared board document, or list keys (optionally by prefix) when key is omitted.

        The returned version is what you pass as expected_version to board_put.
        """
        if key is None:
            keys = await call(lambda c: c.board_list(project, prefix))
            if not keys:
                return "Board is empty." if not prefix else f"No keys with prefix {prefix!r}."
            return "\n".join(f"{k['key']}  (version {k['version']}, updated {k['updated_at']})" for k in keys)
        doc = await call(lambda c: c.board_get(project, key))
        header = f"{project}:{doc['key']} version {doc['version']}"
        body = untrusted.wrap(
            doc["value"],
            author=untrusted.author_label(doc["updated_by"], doc.get("updated_by_name")),
            kind="board", key=doc["key"], version=doc["version"], updated_at=doc["updated_at"],
        )
        return f"{header}\n{body}"

    @server.tool()
    async def board_put(project: str, key: str, value: str, expected_version: int) -> str:
        """Write a board document. expected_version must equal the current version (0 to create).

        On a version conflict nothing is written: re-read with board_get, merge, and retry.
        """
        r = await call(lambda c: c.board_put(project, key, value, expected_version))
        return f"Wrote {project}:{r['key']} version {r['version']}."

    return server


def run_stdio() -> None:
    from agenthub.config import AgentConfig

    cfg = AgentConfig.load()
    client = HubClient.from_config(cfg)
    build_server(lambda: client).run()
