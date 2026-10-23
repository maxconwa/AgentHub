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

Input is stream-json with stdin kept open (``--input-format stream-json --replay-user-messages``), so the runner can
hand a running session more user messages (:meth:`Session.deliver`). Measured on Claude Code 2.1.309 (2026-10-22):
  * a message written mid-turn is taken in at the next tool boundary, inside the same turn: it is echoed back as a
    ``user`` line with ``isReplay`` right after the running tool's result, and the model acts on it;
  * a message written while the model writes a text-only reply waits for that turn's ``result`` and then starts a
    new turn on the same process (a second ``result``); nothing is lost;
  * with stdin open the process waits after a ``result`` and exits cleanly once stdin closes; ``num_turns`` is per
    ``result``, ``total_cost_usd`` adds up over the process.
So a session ends (stdin is closed) at the first ``result`` by which every message written to it has been echoed,
or at a failed ``result`` (an error, the usage limit): messages it hasn't taken in by then may never be, so the
session ends anyway and returns them (``SessionResult.undelivered``) for the runner to hand to the next session.
The prompt and every message are written to stdin, never put in argv or the environment, so no flag can swallow
them and no length limit cuts them: an operator's message of any length reaches the session in full. Output is read
in lines of any length (:func:`lines`), since the session echoes each message back whole. A system prompt too long
for one argument (``ARGV_TEXT_MAX``) goes in a private file instead (``--append-system-prompt-file``).

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
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
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
    pause_until: datetime | None = None  # usage_limit only: when the runner goes on (set by the runner)
    # Tags of the messages delivered to the session (Session.deliver) that it never took in: deliver them elsewhere.
    undelivered: list[Any] = field(default_factory=list)


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
    cmd = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--replay-user-messages",
           *(["--max-turns", str(max_turns)] if max_turns is not None else []),
           "--permission-mode", permission_mode, "--append-system-prompt", system_prompt]
    if resume_session:
        cmd += ["--resume", resume_session] + (["--fork-session"] if fork_session else [])
    return cmd + list(extra_args)


# The longest text put in one argument. Linux refuses a single argument over 128 KiB (MAX_ARG_STRLEN) and macOS all of
# argv and the environment together over 1 MiB (ARG_MAX); longer text goes through a file or stdin.
ARGV_TEXT_MAX = 32_000


def spill_system_prompt(cmd: list[str]) -> tuple[list[str], Path | None]:
    """``cmd`` with an ``--append-system-prompt`` longer than ``ARGV_TEXT_MAX`` bytes moved into a private file
    (``--append-system-prompt-file``), and that file, which the caller removes once the session has ended."""
    try:
        i = cmd.index("--append-system-prompt")
    except ValueError:
        return cmd, None
    if i + 1 >= len(cmd) or len(cmd[i + 1].encode()) <= ARGV_TEXT_MAX:
        return cmd, None
    fd, name = tempfile.mkstemp(prefix="agenthub-system-", suffix=".md")  # 0600
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(cmd[i + 1])
    return [*cmd[:i], "--append-system-prompt-file", name, *cmd[i + 2:]], Path(name)


async def lines(stream: asyncio.StreamReader, chunk: int = 1 << 16) -> AsyncIterator[bytes]:
    """The stream's lines (with their newline; the last one may lack it), however long: ``StreamReader.readline``
    gives up on a line longer than the reader's limit, and an echoed message can be longer than any limit."""
    buffer = bytearray()
    while True:
        data = await stream.read(chunk)
        if not data:
            if buffer:
                yield bytes(buffer)
            return
        start = 0
        while (end := data.find(b"\n", start)) != -1:
            buffer += data[start:end + 1]
            yield bytes(buffer)
            buffer.clear()
            start = end + 1
        buffer += data[start:]


def _tool_summary(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "description"):
        if isinstance(tool_input.get(key), str):
            return f"{name}: {tool_input[key][:200]}"
    return name


TOOL_LINE = 300
_FILE_TOOLS = ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit")


def tool_line(name: str, tool_input: Any) -> str:
    """A compact chat line for one tool call: "Bash: pytest -q", "Edit: views.py", "agenthub: chat_post"."""
    if name.startswith("mcp__"):
        server, _, tool = name[len("mcp__"):].partition("__")
        return f"{server}: {tool}"[:TOOL_LINE] if tool else name[:TOOL_LINE]
    if not isinstance(tool_input, dict):
        return name[:TOOL_LINE]
    if name in _FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(path, str) and path:
            return f"{name}: {path.rstrip('/').rsplit('/', 1)[-1]}"[:TOOL_LINE]
    for key in ("command", "pattern", "query", "url", "description", "path", "file_path", "skill"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            first = value.strip().splitlines()[0]
            return f"{name}: {first}"[:TOOL_LINE]
    return name[:TOOL_LINE]


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
        items: list[tuple[str, Any]] = [("session", msg["session_id"])]
        for key in ("model", "effort"):
            value = msg.get(key)
            if isinstance(value, str) and value:
                items.append((key, value))
        return items
    if mtype == "system" and msg.get("subtype") == "permission_denied":
        return [("progress", {"kind": "denied", "text": str(msg.get("tool_name") or msg)[:TEXT_SNIPPET]})]
    if mtype == "assistant":
        out: list[tuple[str, Any]] = []
        for block in (msg.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                # ``text`` is the whole block (a chat shows it); task.progress keeps a TEXT_SNIPPET of it.
                out.append(("progress", {"kind": "text", "text": block["text"].strip()}))
            elif block.get("type") == "tool_use":
                name, tool_input = str(block.get("name") or "?"), block.get("input")
                out.append(("progress", {"kind": "tool", "text": _tool_summary(name, tool_input),
                                         "line": tool_line(name, tool_input)}))
        return out
    if mtype == "user" and msg.get("isReplay"):  # a message we wrote, now taken into the conversation
        return [("replay", None)]
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


def user_line(text: str) -> bytes:
    """One stream-json input message."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode()


class Session:
    """Runs claude, streams progress to ``on_progress``, enforces an optional wall-clock limit, and takes more
    messages while it runs (:meth:`deliver`).

    ``on_replay(tag)`` is called when a message written to the session is taken into the conversation: ``tag`` is
    what :meth:`deliver` was given (None for the first prompt). ``on_turn_end(result_line)`` is called at each
    ``result``."""

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
        on_replay: Callable[[Any], Awaitable[None]] | None = None,
        on_turn_end: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        kill_grace_seconds: float = 10,
    ) -> None:
        self.cmd, self.prompt, self.cwd, self.env = cmd, prompt, cwd, env
        self.timeout_seconds = timeout_seconds
        self.on_progress, self.on_session = on_progress, on_session
        self.on_replay, self.on_turn_end = on_replay, on_turn_end
        self.kill_grace_seconds = kill_grace_seconds
        self._proc: asyncio.subprocess.Process | None = None
        self._stopped = False
        self._accepting = False  # whether deliver() may still write to stdin
        self._unechoed: deque[Any] = deque()  # tags of the messages written and not yet taken in, oldest first
        self._echoes = 0  # messages echoed back so far (0 after a result: this CLI doesn't echo)
        self.turns = 0  # summed over this process's results
        self.model: str | None = None  # from the session's init line, once it has started
        self.effort: str | None = None  # likewise, if the init line reports one

    @property
    def accepting(self) -> bool:
        """Whether :meth:`deliver` would take a message now."""
        proc = self._proc
        return (self._accepting and proc is not None and proc.returncode is None and proc.stdin is not None
                and not proc.stdin.is_closing())

    def _write(self, text: str, tag: Any) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(user_line(text))
        self._unechoed.append(tag)

    async def deliver(self, text: str, tag: Any = None) -> bool:
        """Hand the running session another user message: it is taken in at the next tool boundary, or starts the
        next turn. False when the session is no longer taking messages (it is ending): deliver it elsewhere."""
        if not self.accepting:
            return False
        self._write(text, tag)
        assert self._proc and self._proc.stdin
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            if self._unechoed and self._unechoed[-1] is tag:
                self._unechoed.pop()
            return False
        return True

    def _close_input(self) -> None:
        self._accepting = False
        proc = self._proc
        if proc is not None and proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()

    async def run(self) -> SessionResult:
        cmd, system_file = spill_system_prompt(self.cmd)
        try:
            return await self._run(cmd)
        finally:
            if system_file is not None:
                system_file.unlink(missing_ok=True)

    async def _run(self, cmd: list[str]) -> SessionResult:
        env = session_env(self.env)
        log.debug("session environment variable names: %s", " ".join(sorted(env)))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=self.cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert self._proc.stdin and self._proc.stdout
        # Not drained here: a long prompt goes out while the output is read, so neither side waits on a full pipe.
        self._write(self.prompt, None)
        self._accepting = not self._stopped

        result_msg: dict[str, Any] | None = None
        session_id: str | None = None
        timed_out = False

        async def read() -> None:
            nonlocal result_msg, session_id
            assert self._proc and self._proc.stdout
            async for raw in lines(self._proc.stdout):
                for kind, value in parse_line(raw.decode(errors="replace")):
                    if kind == "session":
                        session_id = value
                        await self.on_session(value)
                    elif kind == "model":
                        self.model = value
                    elif kind == "effort":
                        self.effort = value
                    elif kind == "progress":
                        await self.on_progress(value)
                    elif kind == "replay":
                        self._echoes += 1
                        tag = self._unechoed.popleft() if self._unechoed else None
                        if self.on_replay is not None:
                            await self.on_replay(tag)
                    elif kind == "result":
                        result_msg = value
                        self.turns += value.get("num_turns") or 0
                        failed = bool(value.get("is_error")) or value.get("subtype") != "success"
                        if self.on_turn_end is not None:
                            await self.on_turn_end(value)
                        if failed:
                            # A failed turn (or the usage limit): take nothing more, and don't wait for messages
                            # not taken in yet (they may never be); run() returns them as undelivered.
                            self._close_input()
                        elif not self._unechoed or self._echoes == 0:
                            if self._echoes == 0 and len(self._unechoed) > 1:
                                log.warning("this claude does not echo input; %d delivered message(s) may be lost",
                                            len(self._unechoed) - 1)
                            self._close_input()  # every message taken in: the session ends with this turn

        try:
            if self.timeout_seconds is None:  # no time limit: run until the session ends
                await read()
            else:
                await asyncio.wait_for(read(), timeout=max(self.timeout_seconds, 1))
        except TimeoutError:
            timed_out = True
            await self.terminate()
        finally:
            self._accepting = False
        returncode = await self._proc.wait()
        result = result_from(result_msg, returncode=returncode, timed_out=timed_out, stopped=self._stopped,
                             session_id=session_id)
        if self.turns:
            result.num_turns = self.turns
        # Delivered (tag not None) and never echoed. Without echoes at all, nothing can be told apart: none.
        result.undelivered = [tag for tag in self._unechoed if tag is not None] if self._echoes else []
        return result

    async def terminate(self) -> None:
        """SIGTERM the session's process group (keeps the transcript resumable), then SIGKILL."""
        self._accepting = False
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
