"""ntfy push notifications for things that need the operator.

Publishing per docs.ntfy.sh/publish (fetched 2026-10-21): JSON POST to the
server root with ``topic``, ``title``, ``message``, ``click``, ``priority``,
``tags``; access tokens go in ``Authorization: Bearer``. JSON avoids header
encoding problems with non-ASCII titles.

Privacy: ntfy.sh topics are public to anyone who knows the name, and any
server is a third party. By default a notification carries only ids, the
approval kind and the requester's name. Commands, task text, and messages
stay on the hub unless ``ntfy_include_details`` is enabled.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from agenthub.config import HubConfig

log = logging.getLogger("agenthub.notify")

KIND_LABELS = {
    "command": "Risky command",
    "task_create": "New task proposed",
    "task_reassign": "Task reassignment",
    "budget_exceeded": "Budget exceeded",
    "agent_enrollment": "New agent enrolled",
    "tool_permission": "Permission needed",
}


def build_notification(event: dict[str, Any], names: dict[str, str], cfg: HubConfig) -> dict[str, Any] | None:
    """Turn an event into an ntfy message, or None if the operator needn't be pinged."""
    p, etype, base = event["payload"], event["type"], cfg.public_url.rstrip("/")
    who = names.get(event["actor"], event["actor"])
    if etype == "approval.requested":
        kind = p.get("kind", "")
        msg: dict[str, Any] = {
            "title": f"{KIND_LABELS.get(kind, kind)} · #{p['approval_id']}",
            "message": f"{who} is waiting for your decision.",
            "priority": 4,
            "tags": ["warning"],
            "click": f"{base}/ui/approvals/{p['approval_id']}" if base else None,
        }
        if cfg.ntfy_include_details:
            details = p.get("details", {})
            text = details.get("command") or details.get("title") or details.get("tool_name") or ""
            msg["message"] += f"\n{text}"[:500]
        return msg
    if etype == "approval.discussed" and not event["actor"].startswith("human:"):
        return {"title": f"Reply on approval #{p['approval_id']}", "message": f"{who} answered you.",
                "priority": 3, "tags": ["speech_balloon"],
                "click": f"{base}/ui/approvals/{p['approval_id']}" if base else None}
    if etype == "task.transitioned" and p.get("to") in ("review", "blocked"):
        return {"title": f"Task #{p['task_id']} is {p['to']}", "message": f"{who} moved it to {p['to']}.",
                "priority": 3, "tags": ["white_check_mark" if p["to"] == "review" else "construction"],
                "click": f"{base}/ui/tasks/{p['task_id']}" if base else None}
    return None


class Notifier:
    def __init__(self, cfg: HubConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.enabled = bool(cfg.ntfy_url and cfg.ntfy_topic)
        self._transport = transport
        self._pending: set[asyncio.Task] = set()

    def _token(self) -> str | None:
        if not self.cfg.ntfy_token_file:
            return None
        return Path(self.cfg.ntfy_token_file).expanduser().read_text().strip() or None

    def submit(self, event: dict[str, Any], names: dict[str, str]) -> None:
        """Fire and forget, so a slow ntfy server never delays the event bus."""
        if not self.enabled:
            return
        msg = build_notification(event, names, self.cfg)
        if msg is None:
            return
        task = asyncio.create_task(self.send(msg))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def send(self, msg: dict[str, Any]) -> bool:
        payload = {"topic": self.cfg.ntfy_topic, **{k: v for k, v in msg.items() if v is not None}}
        headers = {}
        try:
            token = self._token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                resp = await client.post(self.cfg.ntfy_url.rstrip("/") + "/", json=payload, headers=headers)
            resp.raise_for_status()
            return True
        except (httpx.HTTPError, OSError) as exc:
            log.warning("ntfy notification failed: %s", exc.__class__.__name__)
            return False
