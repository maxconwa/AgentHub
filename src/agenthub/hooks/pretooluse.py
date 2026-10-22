"""PreToolUse hook: refuse the Bash commands that are never allowed.

I/O follows code.claude.com/docs/en/hooks (fetched 2026-10-21, Claude Code
2.1.306): stdin is JSON with ``tool_name``, ``tool_input.command``, ``cwd``,
``session_id``; stdout JSON ``hookSpecificOutput.permissionDecision`` of
"deny" (with ``permissionDecisionReason`` shown to Claude). No output means
"no opinion" and the normal permission flow runs.

Since 0.3.6 the only never-allowed command is fetching the hub's operator
dashboard (see ``guard``); every other command goes to Claude Code's own
permission mode. The hook never contacts the hub. Unreadable input → deny.
"""

from __future__ import annotations

from typing import Any

from agenthub.hooks import guard


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


def handle(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return deny("could not read the Bash command; blocked")
    never = guard.classify(command)
    if never:
        reasons = "; ".join(m.description for m in never)
        return deny(f"This command is never allowed ({reasons}). It cannot be approved; do not retry it.")
    return None
