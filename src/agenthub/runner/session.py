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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEXT_SNIPPET = 300


@dataclass
class SessionResult:
    outcome: str  # success | max_turns | wall_clock | error | stopped
    session_id: str | None = None
    num_turns: int | None = None
    cost_usd: float | None = None
    summary: str = ""
    errors: list[str] = field(default_factory=list)


def build_command(
    claude_bin: str,
    *,
    max_turns: int,
    system_prompt: str,
    resume_session: str | None,
    extra_args: list[str],
) -> list[str]:
    cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose", "--max-turns", str(max_turns),
           "--append-system-prompt", system_prompt]
    if resume_session:
        cmd += ["--resume", resume_session]
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
        return [("session", msg["session_id"])]
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
    if outcome == "error" and not errors:
        errors = [f"claude exited with code {returncode} ({msg.get('subtype') or 'no result'})"]
    return SessionResult(
        outcome=outcome,
        session_id=msg.get("session_id") or session_id,
        num_turns=msg.get("num_turns"),
        cost_usd=msg.get("total_cost_usd"),
        summary=str(msg.get("result") or "")[:2000],
        errors=errors,
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
        timeout_seconds: float,
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

    async def run(self) -> SessionResult:
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd, cwd=self.cwd, env={**os.environ, **self.env},
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
                    elif kind == "progress":
                        await self.on_progress(value)
                    elif kind == "result":
                        result_msg = value

        try:
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
