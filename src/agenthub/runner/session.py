"""One headless ``claude -p`` session and its stream-json output.

Flags and message shapes per code.claude.com/docs/en/headless and
cli-reference (fetched 2026-10-21, Claude Code 2.1.306):
  * ``--output-format stream-json --verbose`` emits one JSON object per line.
  * ``{"type":"system","subtype":"init","session_id":...}`` comes first.
  * ``assistant`` lines carry ``message.content`` blocks (``text`` / ``tool_use``).
  * the final ``result`` line has ``subtype`` (success, error_max_turns,
    error_during_execution, ...), ``num_turns``, ``total_cost_usd``, and
    ``result`` text on success.
  * ``--resume <session_id>`` continues a saved transcript; ``--max-turns`` is
    per invocation. SIGTERM exits 143 and keeps the transcript resumable.
The prompt is written to stdin so no flag can swallow it.

Subscription only: every session's environment drops ``BILLING_ENV_VARS`` (see there), and a result
carrying the subscription usage-limit message ends with outcome ``usage_limit`` and its reset time
(``parse_limit``), so the runner can pause instead of failing the task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("agenthub.runner")

TEXT_SNIPPET = 300

# Environment variables that make Claude Code bill something other than the operator's Claude subscription
# (an API key, a gateway, a cloud provider or a Console/federation credential). Per
# code.claude.com/docs/en/env-vars and /authentication ("Authentication precedence"), fetched 2026-10-21:
#   * cloud provider selection, which outranks every other credential: CLAUDE_CODE_USE_BEDROCK,
#     CLAUDE_CODE_USE_VERTEX, CLAUDE_CODE_USE_FOUNDRY, CLAUDE_CODE_USE_ANTHROPIC_AWS (Claude Platform on
#     AWS), CLAUDE_CODE_USE_MANTLE (Bedrock Mantle endpoint);
#   * ANTHROPIC_AUTH_TOKEN and ANTHROPIC_API_KEY ("In non-interactive mode (-p), the key is always used
#     when present ... instead of your Claude Pro, Max, Team, or Enterprise subscription");
#   * ANTHROPIC_BASE_URL: routes requests to a gateway, which allowedProviders treats as its own provider
#     ("customEndpoint");
#   * ANTHROPIC_PROFILE, and ANTHROPIC_FEDERATION_RULE_ID + ANTHROPIC_ORGANIZATION_ID: Anthropic profile /
#     Workload Identity Federation credentials that "rank above your /login credential";
#   * cloud-provider API keys and tokens: AWS_BEARER_TOKEN_BEDROCK, ANTHROPIC_AWS_API_KEY,
#     ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_AUTH_TOKEN.
# CLAUDE_CODE_OAUTH_TOKEN stays: it is a subscription token (`claude setup-token`). apiKeyHelper is a
# settings key, not a variable; `agenthub doctor` flags it.
BILLING_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_MANTLE",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
)


def session_env(extra: dict[str, str]) -> dict[str, str]:
    """The child environment: ``os.environ`` plus ``extra``, without any ``BILLING_ENV_VARS``."""
    return {k: v for k, v in {**os.environ, **extra}.items() if k not in BILLING_ENV_VARS}


# The subscription usage-limit messages, per code.claude.com/docs/en/errors (fetched 2026-10-21):
#   You've hit your session limit · resets 3:45pm
#   You've hit your weekly limit · resets Mon 12:00am
#   You've hit your Opus limit · resets 3:45pm      (and Sonnet)
# and a spend-limit message whose plan window ran out: "... · your session limit resets 3:45pm".
_LIMIT_LINE = re.compile(r"You[\'’]ve hit your (?:(?:session|weekly|Opus|Sonnet) limit\b|[^\n]*?"
                         r"·\s*your (?:session|weekly|Opus|Sonnet) limit resets\b)[^\n]*", re.IGNORECASE)
_RESET = re.compile(
    r"\bresets\s+(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?(?![\w:])"
    r"(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)*)\))?",
    re.IGNORECASE,
)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# A reset time this recently past has already happened (the message is read after the session exits):
# it is not rolled to the next day or week.
JUST_RESET = timedelta(minutes=15)


def _clock(m: re.Match[str]) -> tuple[int, int] | None:
    hour, minute, ampm = int(m["hour"]), int(m["minute"] or 0), (m["ampm"] or "").lower()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        return hour % 12 + (12 if ampm == "pm" else 0), minute
    if m["minute"] is None or hour > 23:  # a bare "3" is not a time
        return None
    return hour, minute


def parse_limit(text: str, now: datetime) -> tuple[bool, datetime | None]:
    """Whether ``text`` carries the subscription usage-limit message, and when the limit resets.

    The reset is the next occurrence (after ``now``) of the time in the message, in ``now``'s zone (naive
    means local), or in the IANA zone the message names in parentheses. A time at most ``JUST_RESET`` in the
    past has just happened, so the reset is ``now``. ``(True, None)`` means the message was recognised but
    its time could not be read."""
    line = _LIMIT_LINE.search(text or "")
    if line is None:
        return False, None
    m = _RESET.search(line.group(0))
    clock = _clock(m) if m else None
    if m is None or clock is None:
        return True, None
    zone = None
    if m["zone"]:
        try:
            zone = ZoneInfo(m["zone"])
        except (ValueError, KeyError, OSError):
            return True, None
    base = now.astimezone(zone) if zone else now
    candidate = base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    period = timedelta(days=7 if m["day"] else 1)
    if m["day"]:
        candidate += timedelta(days=(_WEEKDAYS.index(m["day"].lower()) - base.weekday()) % 7)
    if candidate <= base:
        candidate += period
    if candidate - period >= base - JUST_RESET:  # the previous occurrence was moments ago
        candidate = base
    if zone and now.tzinfo is None:
        candidate = candidate.astimezone().replace(tzinfo=None)  # back to local wall time
    return True, candidate


@dataclass
class SessionResult:
    outcome: str  # success | max_turns | wall_clock | error | stopped | usage_limit
    session_id: str | None = None
    num_turns: int | None = None
    cost_usd: float | None = None
    summary: str = ""
    errors: list[str] = field(default_factory=list)
    resets_at: datetime | None = None  # usage_limit only: when the limit resets (aware), if the message said


def build_command(
    claude_bin: str,
    *,
    max_turns: int | None,
    system_prompt: str,
    resume_session: str | None,
    extra_args: list[str],
    permission_mode: str = "auto",
    fork_session: bool = False,
) -> list[str]:
    """``max_turns`` None: no turn limit (task sessions run until they're done). ``fork_session`` (with
    ``resume_session``): continue from a copy of that transcript under a new session id (``--fork-session``),
    leaving the original to its owner."""
    cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose",
           *(["--max-turns", str(max_turns)] if max_turns is not None else []),
           "--permission-mode", permission_mode, "--append-system-prompt", system_prompt]
    if resume_session:
        cmd += ["--resume", resume_session] + (["--fork-session"] if fork_session else [])
    return cmd + list(extra_args)


def _tool_summary(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "description"):
        if isinstance(tool_input.get(key), str):
            return f"{name}: {tool_input[key][:200]}"
    return name


def parse_line(line: str) -> list[tuple[str, Any]]:
    """Turn one stream-json line into (kind, value) items; unknown lines are ignored."""
    try:
        msg = json.loads(line)
    except ValueError:
        return []
    if not isinstance(msg, dict):
        return []
    mtype = msg.get("type")
    if mtype == "system" and msg.get("subtype") == "init" and msg.get("session_id"):
        model = msg.get("model")
        return [("session", msg["session_id"])] + ([("model", model)] if isinstance(model, str) and model else [])
    if mtype == "system" and msg.get("subtype") == "permission_denied":
        return [("progress", {"kind": "denied", "text": str(msg.get("tool_name") or msg)[:TEXT_SNIPPET]})]
    if mtype == "assistant":
        out: list[tuple[str, Any]] = []
        for block in (msg.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append(("progress", {"kind": "text", "text": block["text"].strip()[:TEXT_SNIPPET]}))
            elif block.get("type") == "tool_use":
                out.append(("progress", {"kind": "tool", "text": _tool_summary(block.get("name", "?"),
                                                                                block.get("input"))}))
        return out
    if mtype == "result":
        return [("result", msg)]
    return []


def result_from(msg: dict[str, Any] | None, *, returncode: int | None, timed_out: bool, stopped: bool,
                session_id: str | None) -> SessionResult:
    if stopped:
        outcome = "stopped"
    elif timed_out:
        outcome = "wall_clock"
    elif msg is None:
        outcome = "error"
    else:
        subtype = msg.get("subtype")
        outcome = {"success": "success", "error_max_turns": "max_turns"}.get(subtype, "error")
        if subtype == "success" and msg.get("is_error"):
            outcome = "error"
    msg = msg or {}
    errors = [str(e)[:500] for e in msg.get("errors") or []]
    resets_at = None
    if outcome in ("error", "success") and msg:
        # Only a failed result's text counts: a successful run may merely quote the message in its summary.
        texts = ([str(msg.get("result") or "")] if outcome == "error" else []) + errors
        for text in texts:
            limited, when = parse_limit(text, datetime.now())  # local wall time, like the message
            if limited:
                outcome, resets_at = "usage_limit", when.astimezone() if when else None
                break
    if outcome == "error" and not errors:
        errors = [f"claude exited with code {returncode} ({msg.get('subtype') or 'no result'})"]
    return SessionResult(
        outcome=outcome,
        session_id=msg.get("session_id") or session_id,
        num_turns=msg.get("num_turns"),
        cost_usd=msg.get("total_cost_usd"),
        summary=str(msg.get("result") or "")[:2000],
        errors=errors,
        resets_at=resets_at,
    )


class Session:
    """Runs claude, streams progress to ``on_progress``, enforces a wall-clock limit."""

    def __init__(
        self,
        cmd: list[str],
        *,
        prompt: str,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float | None,
        on_progress: Callable[[dict[str, str]], Awaitable[None]],
        on_session: Callable[[str], Awaitable[None]],
        kill_grace_seconds: float = 10,
    ) -> None:
        self.cmd, self.prompt, self.cwd, self.env = cmd, prompt, cwd, env
        self.timeout_seconds = timeout_seconds
        self.on_progress, self.on_session = on_progress, on_session
        self.kill_grace_seconds = kill_grace_seconds
        self._proc: asyncio.subprocess.Process | None = None
        self._stopped = False
        self.model: str | None = None  # from the session's init line, once it has started

    async def run(self) -> SessionResult:
        env = session_env(self.env)
        log.debug("session environment variable names: %s", " ".join(sorted(env)))
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd, cwd=self.cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=16 * 1024 * 1024,
        )
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(self.prompt.encode())
        await self._proc.stdin.drain()
        self._proc.stdin.close()

        result_msg: dict[str, Any] | None = None
        session_id: str | None = None
        timed_out = False

        async def read() -> None:
            nonlocal result_msg, session_id
            assert self._proc and self._proc.stdout
            async for raw in self._proc.stdout:
                for kind, value in parse_line(raw.decode(errors="replace")):
                    if kind == "session":
                        session_id = value
                        await self.on_session(value)
                    elif kind == "model":
                        self.model = value
                    elif kind == "progress":
                        await self.on_progress(value)
                    elif kind == "result":
                        result_msg = value

        try:
            if self.timeout_seconds is None:  # no time limit: run until the session ends
                await read()
            else:
                await asyncio.wait_for(read(), timeout=max(self.timeout_seconds, 1))
        except TimeoutError:
            timed_out = True
            await self.terminate()
        returncode = await self._proc.wait()
        return result_from(result_msg, returncode=returncode, timed_out=timed_out, stopped=self._stopped,
                           session_id=session_id)

    async def terminate(self) -> None:
        """SIGTERM the session's process group (keeps the transcript resumable), then SIGKILL."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.kill_grace_seconds)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    async def stop(self) -> None:
        self._stopped = True
        await self.terminate()
