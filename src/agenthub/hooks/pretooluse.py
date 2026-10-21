"""PreToolUse hook: gate risky Bash commands behind hub approvals.

I/O follows code.claude.com/docs/en/hooks (fetched 2026-10-21, Claude Code
2.1.306): stdin is JSON with ``tool_name``, ``tool_input.command``, ``cwd``,
``session_id``; stdout JSON ``hookSpecificOutput.permissionDecision`` of
"deny" (with ``permissionDecisionReason`` shown to Claude) or "allow". No
output means "no opinion" and the normal permission flow runs.

Flow for a risky command:
  1. never-list match → deny; cannot be approved.
  2. ask the hub to consume an approved approval for exactly (agent, cwd,
     command) → allow once.
  3. otherwise file (or reuse) a pending command approval → deny, telling the
     agent the approval id and to call wait_for_approval, then retry.
Any error (bad config, no token, hub unreachable, unexpected exception) → deny.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from agenthub.client import HubClient
from agenthub.config import AgentConfig
from agenthub.hooks import guard

HUB_TIMEOUT_SECONDS = 10


def _decision(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


def deny(reason: str) -> dict[str, Any]:
    return _decision("deny", f"[agenthub] {reason}")


def _default_client() -> HubClient:
    return HubClient.from_config(AgentConfig.load(), timeout=HUB_TIMEOUT_SECONDS)


async def _consult_hub(client: HubClient, command: str, cwd: str, reasons: str) -> dict[str, Any]:
    async with client:
        consumed = await client.consume_command(command, cwd)
        if consumed["allowed"]:
            return _decision(
                "allow", f"[agenthub] approved by operator (approval #{consumed['approval_id']}, one-time use)"
            )
        approval = await client.request_command(
            command, cwd, project=os.environ.get("AGENTHUB_PROJECT"), reason=f"blocked: {reasons}"
        )
    return deny(
        f"This command needs operator approval ({reasons}). Approval #{approval['id']} is "
        f"{approval['status']}. Call the agenthub `wait_for_approval` tool with approval_id={approval['id']}; "
        "if it is approved, retry the exact same command from the same directory (it is allowed once). "
        "If the operator asks you something, wait_for_approval returns early: answer with "
        "`discuss_approval`, then wait again. If it is rejected, do not retry; choose another approach."
    )


def handle(
    payload: dict[str, Any],
    *,
    client_factory: Callable[[], HubClient] = _default_client,
    branch_of: Callable[[str], str | None] = guard.current_branch,
) -> dict[str, Any] | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(command, str):
        return deny("could not read the Bash command; blocked")
    try:
        cfg = guard.load_guard_config()
    except guard.GuardConfigError as exc:
        return deny(f"guard config is invalid, so all Bash commands are blocked: {exc}")

    never, risky = guard.classify(command, cwd, cfg, branch_of=branch_of)
    if never:
        reasons = "; ".join(m.description for m in never)
        return deny(f"This command is never allowed ({reasons}). It cannot be approved; do not retry it.")
    if not risky:
        return None

    reasons = "; ".join(m.description for m in risky)
    try:
        return asyncio.run(_consult_hub(client_factory(), command, cwd, reasons))
    except Exception as exc:  # noqa: BLE001 - fail closed on anything
        return deny(
            f"This command needs operator approval ({reasons}), but the hub could not be reached "
            f"({exc.__class__.__name__}). It stays blocked until the hub is available."
        )
