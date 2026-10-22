"""PermissionRequest hook: let the operator grant permissions live to headless task sessions.

I/O per code.claude.com/docs/en/hooks, PermissionRequest section (fetched
2026-10-21, Claude Code 2.1.306): it fires when a tool call needs a permission
decision. Stdin has ``tool_name`` and ``tool_input``; stdout
``hookSpecificOutput.decision`` is ``{"behavior": "allow"}`` or
``{"behavior": "deny", "message": ...}``. It is only reached when nothing
earlier (PreToolUse hooks, deny/ask/allow rules) resolved the call, so it can
never override agenthub's PreToolUse gate or the never-allow deny rules.

Only runner sessions (``AGENTHUB_TASK_ID`` set by the runner) ask the hub. A runner
session on an operator's prompt without a task (``AGENTHUB_PROMPT_ID`` only) has
nothing to ask on, so the call is denied with a pointer to ``message_operator``.
In interactive sessions the hook stays silent and Claude Code prompts the person
at the terminal as usual.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from typing import Any

from agenthub.client import HubClient
from agenthub.config import AgentConfig

DEFAULT_WAIT_SECONDS = 600
POLL_SECONDS = 3.0
MAX_INLINE_INPUT = 4000  # larger inputs are sent as a preview plus a hash
# Said to a prompt session without a task (in its system prompt and when the hook denies a tool).
NO_TASK_GRANTS = ("This session has no task, so tools outside your permissions are unavailable in it. If you need "
                  "one, tell the operator with message_operator what you would need and why.")


def _decision(behavior: str, message: str | None = None) -> dict[str, Any]:
    decision: dict[str, Any] = {"behavior": behavior}
    if message:
        decision["message"] = f"[agenthub] {message}"
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def compact_input(tool_input: Any) -> Any:
    """Keep approvals small but still bound to the exact input."""
    raw = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    if len(raw) <= MAX_INLINE_INPUT:
        return tool_input
    return {"preview": raw[:MAX_INLINE_INPUT], "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "size": len(raw)}


def _default_client() -> HubClient:
    return HubClient.from_config(AgentConfig.load(), timeout=15)


def _thread_summary(approval: dict[str, Any]) -> str:
    notes = [m["body"] for m in approval.get("discussion", []) if m["author"].startswith("human:")]
    if approval.get("decision_note"):
        notes.append(approval["decision_note"])
    return (" Operator said: " + " / ".join(notes)) if notes else ""


async def _ask_operator(
    client: HubClient, task_id: int, tool_name: str, tool_input: Any, wait_seconds: float, poll: float
) -> dict[str, Any]:
    async with client:
        consumed = await client.consume_tool_permission(task_id, tool_name, tool_input)
        if consumed["allowed"]:
            return _decision("allow")
        approval = await client.request_tool_permission(
            task_id, tool_name, tool_input, reason=f"headless task #{task_id} needs {tool_name}"
        )
        deadline = time.monotonic() + wait_seconds
        while approval["status"] == "pending" and time.monotonic() < deadline:
            await asyncio.sleep(poll)
            approval = await client.get_approval(approval["id"])
        if approval["status"] == "approved":
            consumed = await client.consume_tool_permission(task_id, tool_name, tool_input)
            if consumed["allowed"]:
                return _decision("allow")
            return _decision("deny", f"approval #{approval['id']} was approved but already used or expired.")
        if approval["status"] == "rejected":
            return _decision("deny", f"The operator rejected this {tool_name} call (approval "
                                     f"#{approval['id']}).{_thread_summary(approval)} Choose another approach.")
    return _decision(
        "deny",
        f"No operator decision within {int(wait_seconds)}s; approval #{approval['id']} is still pending. "
        "You can call wait_for_approval with that id and retry this exact action once it is approved.",
    )


def handle(
    payload: dict[str, Any],
    *,
    client_factory: Callable[[], HubClient] = _default_client,
    wait_seconds: float | None = None,
    poll: float = POLL_SECONDS,
) -> dict[str, Any] | None:
    task_id = os.environ.get("AGENTHUB_TASK_ID")
    if not task_id:
        if os.environ.get("AGENTHUB_PROMPT_ID"):  # a headless prompt session: nobody at the terminal to ask
            return _decision("deny", NO_TASK_GRANTS)
        return None  # interactive session: let Claude Code prompt as usual
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return _decision("deny", "could not read the tool name")
    wait = wait_seconds if wait_seconds is not None else float(
        os.environ.get("AGENTHUB_PERMISSION_WAIT_SECONDS", DEFAULT_WAIT_SECONDS))
    try:
        return asyncio.run(_ask_operator(client_factory(), int(task_id), tool_name,
                                         compact_input(payload.get("tool_input")), wait, poll))
    except Exception as exc:  # noqa: BLE001 - no decision means no permission
        return _decision("deny", f"could not reach the hub to ask the operator ({exc.__class__.__name__}).")
