"""Prompts from the operator: the runner wakes for them and runs a session on each.

* ``sync`` fetches the prompts this agent still has to act on (``GET /prompts/pending``) and starts one flow
  per prompt. A prompt runs as soon as a session slot is free and before any new task session
  (:class:`Slots`); a running session is never interrupted.
* Where it runs: a task target in that project's checkout, resuming this agent's latest session on the task
  when there is one; a project target in that project's checkout; an agent or device target in the checkout
  of the agent's current task's project, else its first configured checkout. Without a checkout the prompt is
  finished as failed with a note.
* The operator's text reaches the session outside any untrusted block, under
  ``From the operator (prompt #N, verified by the hub):``. A single recipient acts on it and answers with
  ``message_operator``; a group discusses it in the prompt's thread, then each claims a share (a task it picks
  up next) or declines.
* Budget: ``prompt_max_turns`` and ``prompt_wall_clock_minutes`` (runner.toml). The subscription usage limit
  pauses the prompt until the reset, then resumes the same session once; the hub's prompt stays ``running``
  meanwhile. Prompt sessions record no task runs: the outcome is reported with ``finish``.
* Each prompt's claude session id is kept on disk (:class:`PromptSessions`): a prompt a previous runner process
  left ``running`` is resumed once after a restart (started afresh with a note if its session id was never
  seen). If the runner restarts again, or the resumed session fails, the prompt is finished as failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from agenthub import paths, untrusted
from agenthub.client import HubError
from agenthub.runner.session import Session, SessionResult, build_command

if TYPE_CHECKING:
    from agenthub.runner.daemon import Runner

log = logging.getLogger("agenthub.runner")

OPEN = ("pending", "running")  # a recipient's statuses that still need its runner
MAX_NOTE_CHARS = 4000
# An agent or device prompt runs in the checkout of the agent's current task: a task with a session here first,
# then by state.
_STATE_RANK = {"in_progress": 0, "review": 1, "claimed": 2, "open": 3, "blocked": 4}


class Slots:
    """The runner's session slots. A queued prompt takes the next free slot before any new task session."""

    def __init__(self, n: int) -> None:
        self._sem = asyncio.Semaphore(n)
        self.queued: set[int] = set()
        self._no_prompt_queued = asyncio.Event()
        self._no_prompt_queued.set()

    def queue(self, prompt_id: int) -> None:
        """New task sessions wait from now on until this prompt has a slot (or is dropped)."""
        self.queued.add(prompt_id)
        self._no_prompt_queued.clear()

    def unqueue(self, prompt_id: int) -> None:
        self.queued.discard(prompt_id)
        if not self.queued:
            self._no_prompt_queued.set()

    @contextlib.asynccontextmanager
    async def task(self) -> AsyncIterator[None]:
        while True:
            await self._no_prompt_queued.wait()
            await self._sem.acquire()
            if self._no_prompt_queued.is_set():
                break
            self._sem.release()  # a prompt was queued while this waited: it goes first
        try:
            yield
        finally:
            self._sem.release()

    @contextlib.asynccontextmanager
    async def prompt(self, prompt_id: int) -> AsyncIterator[None]:
        self.queue(prompt_id)
        try:
            await self._sem.acquire()
        finally:
            self.unqueue(prompt_id)
        try:
            yield
        finally:
            self._sem.release()


class PromptSessions:
    """Prompt id -> ``{session_id, restarts}`` for this agent's prompt sessions, in a private file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        try:
            paths.ensure_private_dir(self.path.parent)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError as exc:  # a prompt still runs; only a restart could not resume it
            log.warning("could not save prompt sessions to %s: %s", self.path, exc)

    def get(self, prompt_id: int) -> dict[str, Any] | None:
        return self._load().get(str(prompt_id))

    def update(self, prompt_id: int, **fields: Any) -> None:
        data = self._load()
        data[str(prompt_id)] = {**data.get(str(prompt_id), {}), **fields}
        self._save(data)

    def drop(self, prompt_id: int) -> None:
        data = self._load()
        if data.pop(str(prompt_id), None) is not None:
            self._save(data)


def heading(prompt_id: int) -> str:
    return f"From the operator (prompt #{prompt_id}, verified by the hub):"


def _about(p: dict[str, Any]) -> str:
    kind, _, key = p["target"].partition(":")
    if kind == "task":
        return f"It is about task #{key} (project {p['project']})."
    if kind == "project":
        return f"It is about project {p['project']}."
    if kind == "device":
        return "It went to the agents on your machine."
    return "It went to you."


def _thread(p: dict[str, Any]) -> tuple[str, str]:
    """Where a group discusses its prompt, and how to post there."""
    t = p.get("thread")
    if t and t["kind"] == "channel":
        return (f"the project channel '{t['channel']}' in project '{t['project']}'",
                f"post(project='{t['project']}', channel='{t['channel']}', body=...)")
    return "your machine's device board", "post_device(body=...)"


def operator_prompt(p: dict[str, Any], *, restarted: bool = False) -> str:
    """The first message of a prompt's session: the operator's text (verified by the hub, so not wrapped as
    untrusted), then what to do with it."""
    pid = p["id"]
    text = f"{heading(pid)}\n{p['body']}\n\n"
    if restarted:
        text += ("(You are resuming after a runner restart: an earlier session on this prompt was cut off, so part "
                 "of it may be done already. Check before redoing anything.)\n\n")
    text += _about(p) + " "
    if p["mode"] != "group":
        return text + ("Act on it in this session. When you are done, tell the operator what you did, or answer "
                       "them, with message_operator.")
    where, how = _thread(p)
    return text + (
        f"Several agents got this prompt. Discuss it with them as equals in {where}: call "
        f"prompt_status(prompt_id={pid}) to read the prompt, the other recipients (who is free and who is busy, and "
        f"on which machine) and the thread so far, and post there with {how}. Weigh who is free and who is busy and "
        "what each machine has, and agree on a split; read the thread again until you have agreed. Then call "
        f"claim_share(prompt_id={pid}, title=..., description=...) with a clear title and description for your "
        "part: it becomes a task assigned to you, already approved. If you take no part, call "
        f"decline_share(prompt_id={pid}, reason=...) instead. Do not start the work in this session: your share "
        "becomes a task you'll pick up next."
    )


def resume_prompt(p: dict[str, Any], reason: str) -> str:
    pid = p["id"]
    if reason == "limit":
        text = f"The usage limit has reset. Continue with the operator's prompt #{pid} where you left off."
    else:
        text = (f"The runner restarted while you were working on the operator's prompt #{pid}. Continue where you "
                "left off.")
    if p["mode"] == "group":
        text += f" End with claim_share or decline_share for prompt #{pid} if you haven't yet."
    return text


def system_prompt(agent_name: str, p: dict[str, Any]) -> str:
    others = " (with other agents)" if p["mode"] == "group" else ""
    return (
        f"You are the agenthub agent '{agent_name}', running headless (no human at this terminal). The operator "
        f"woke you with prompt #{p['id']}{others}; work on that prompt only.\n"
        "Coordinate through the agenthub MCP tools: read_inbox/post for messages, ask_agent for direct "
        "questions, board_get/board_put for shared notes.\n"
        "Risky shell commands and tools outside your permissions need operator approval: follow the "
        "instructions the hooks give you.\n" + untrusted.NOTICE
    )


class PromptRunner:
    """Runs this agent's prompts for a :class:`Runner` (its client, config, slots and task work)."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.store = PromptSessions(paths.data_dir() / "runner" / f"prompt-sessions-{runner.agent.agent_id}.json")
        self.flows: dict[int, asyncio.Task] = {}
        self.sessions: dict[int, Session] = {}
        # prompt id -> (ok, note) of a finish the hub didn't take; sent again on the next sync
        self._unreported: dict[int, tuple[bool, str]] = {}

    async def sync(self) -> None:
        """Start a flow for each prompt this agent still has to act on and isn't handling yet (oldest first)."""
        pending = {p["id"]: p for p in await self.runner.client.pending_prompts()}
        for pid in [pid for pid, flow in self.flows.items() if flow.done() and pid not in pending]:
            del self.flows[pid]
            self._unreported.pop(pid, None)
        for pid, p in pending.items():
            flow = self.flows.get(pid)
            if flow is None:
                self.runner.slots.queue(pid)  # before any task loop can take the next slot
                self.flows[pid] = asyncio.create_task(self._flow(p), name=f"prompt-{pid}")
            elif flow.done() and pid in self._unreported:
                await self._report(pid, *self._unreported[pid])

    async def _flow(self, summary: dict[str, Any]) -> None:
        pid = summary["id"]
        try:
            await self._run(pid, restarted=summary["my_status"] == "running")
        except Exception:  # noqa: BLE001 - one prompt's failure must not kill the runner
            log.exception("prompt %s failed in the runner", pid)
            await self._report(pid, False, "runner error; see runner logs")
        finally:
            self.runner.slots.unqueue(pid)

    async def _run(self, pid: int, *, restarted: bool) -> None:
        client, cfg = self.runner.client, self.runner.cfg
        p = await client.get_prompt(pid)
        if p["my_status"] not in OPEN:
            return
        record = self.store.get(pid) or {}
        if restarted:  # a previous runner process started it and stopped
            if record.get("restarts", 0) >= 1:
                await self._report(pid, False, "the runner restarted again during this prompt; it is not resumed "
                                               "a second time")
                return
            self.store.update(pid, restarts=record.get("restarts", 0) + 1)
            log.info("prompt %s: resuming after a runner restart", pid)
        project, cwd = self._checkout(p)
        if cwd is None:
            await self._report(pid, False, f"no checkout configured for {project or 'any project'}")
            return
        kind, _, key = p["target"].partition(":")
        task_id = int(key) if kind == "task" else None
        if restarted and record.get("session_id"):
            resume, text = record["session_id"], resume_prompt(p, "restart")
        else:
            resume, text = None, operator_prompt(p, restarted=restarted)
        turns, seconds = cfg.prompt_max_turns, cfg.prompt_wall_clock_minutes * 60
        started, waited_for_limit = restarted, False
        while True:
            async with self.runner.slots.prompt(pid):
                if not started:  # the session starts now, when a slot is free
                    if not await self._start(pid):
                        return
                    started = True
                    if task_id is not None:
                        resume = await self._task_session(task_id)
                began = time.monotonic()
                result = await self._session(p, text, resume=resume, cwd=cwd, project=project, task_id=task_id,
                                             turns=turns, seconds=seconds)
            turns -= result.num_turns or 0
            seconds -= time.monotonic() - began
            resume = result.session_id or resume
            if result.outcome == "usage_limit" and not waited_for_limit:
                waited_for_limit = True
                self.runner.slots.queue(pid)  # no new task session starts meanwhile; this prompt goes first after
                await self.runner.wait_for_limit_reset(result.resets_at, what=f"prompt {pid}")
                text = resume_prompt(p, "limit")
                continue
            break
        await self._finish(p, result)

    async def _start(self, pid: int) -> bool:
        try:
            await self.runner.client.start_prompt(pid)
        except HubError as exc:
            if exc.status not in (404, 409):
                raise
            log.info("prompt %s changed meanwhile (%s); not starting it", pid, exc.detail)
            self.store.drop(pid)
            return False
        return True

    def _checkout(self, p: dict[str, Any]) -> tuple[str | None, Path | None]:
        """The project the prompt's session runs in and its checkout here (None when there is none)."""
        cfg = self.runner.cfg

        def usable(project: str) -> Path | None:
            path = cfg.project_dir(project)
            return path if path is not None and path.is_dir() else None

        if p["project"] is not None:  # task and project targets
            return p["project"], usable(p["project"])
        me = next((r for r in p["recipients"] if r["agent_id"] == self.runner.agent.agent_id), None)
        running = {tid for tid, work in self.runner.work.items() if work.session is not None}
        mine = sorted(me["tasks"] if me else [],
                      key=lambda t: (t["id"] not in running, _STATE_RANK.get(t["state"], len(_STATE_RANK)), t["id"]))
        for project in [t["project"] for t in mine] + list(cfg.projects):
            if (path := usable(project)) is not None:
                return project, path
        return (mine[0]["project"] if mine else None), None

    async def _task_session(self, task_id: int) -> str | None:
        """This agent's latest session on the task, to resume, unless a task session runs on it right now."""
        work = self.runner.work.get(task_id)
        if work is not None and work.session is not None:
            return None
        try:
            usage = (await self.runner.client.task_runs(task_id, agent=self.runner.agent.agent_id))["usage"]
        except HubError as exc:
            log.warning("could not read the runs of task %s: %s", task_id, exc)
            return None
        return usage["last_session_id"]

    async def _session(self, p: dict[str, Any], text: str, *, resume: str | None, cwd: Path, project: str | None,
                       task_id: int | None, turns: int, seconds: float) -> SessionResult:
        cfg, pid = self.runner.cfg, p["id"]
        cmd = build_command(cfg.claude_bin, max_turns=max(turns, 1),
                            system_prompt=system_prompt(self.runner.agent.name, p), resume_session=resume,
                            extra_args=cfg.extra_args, permission_mode=cfg.permission_mode)
        env = {
            "AGENTHUB_PROMPT_ID": str(pid),
            "AGENTHUB_CONFIG_DIR": str(paths.config_dir()),
            "AGENTHUB_PERMISSION_WAIT_SECONDS": str(cfg.permission_wait_seconds),
        }
        if project is not None:
            env["AGENTHUB_PROJECT"] = project
        if task_id is not None:  # the operator can grant permissions live on the task the prompt is about
            env["AGENTHUB_TASK_ID"] = str(task_id)

        async def on_progress(item: dict[str, str]) -> None:
            pass  # a prompt session has no task to stream progress to

        async def on_session(session_id: str) -> None:
            self.store.update(pid, session_id=session_id)

        session = Session(cmd, prompt=text, cwd=cwd, env=env, timeout_seconds=max(seconds, 0),
                          on_progress=on_progress, on_session=on_session)
        self.sessions[pid] = session
        self.runner._session_dirs[session] = str(cwd)
        try:
            return await session.run()
        finally:
            self.sessions.pop(pid, None)
            self.runner._session_dirs.pop(session, None)

    async def _finish(self, p: dict[str, Any], result: SessionResult) -> None:
        cfg = self.runner.cfg
        if result.outcome == "stopped":
            return  # the runner is stopping: the prompt stays running and is resumed after the restart
        if result.outcome == "success" and p["mode"] == "group":
            # Changes nothing on the hub once this agent has claimed or declined a share.
            ok, note = False, "the discussion session ended without claiming or declining a share"
        elif result.outcome == "success":
            ok, note = True, result.summary or "done"
        elif result.outcome == "max_turns":
            ok, note = False, f"the prompt session used all of its {cfg.prompt_max_turns} turns"
        elif result.outcome == "wall_clock":
            ok, note = False, f"the prompt session ran out of its {cfg.prompt_wall_clock_minutes} minutes"
        elif result.outcome == "usage_limit":
            ok, note = False, "the usage limit was hit again after waiting for its reset"
        else:
            ok, note = False, "prompt session failed: " + ("; ".join(result.errors) or "session failed")
        await self._report(p["id"], ok, note)

    async def _report(self, pid: int, ok: bool, note: str) -> None:
        """Tell the hub how the prompt ended; a finish the hub didn't take is sent again on the next sync."""
        try:
            await self.runner.client.finish_prompt(pid, ok, note[:MAX_NOTE_CHARS])
        except (HubError, httpx.HTTPError) as exc:
            if isinstance(exc, HubError) and exc.status in (403, 404):  # no longer this agent's prompt
                self._unreported.pop(pid, None)
                self.store.drop(pid)
                return
            log.warning("prompt %s: could not report how it ended: %s", pid, exc)
            self._unreported[pid] = (ok, note)
            return
        self._unreported.pop(pid, None)
        self.store.drop(pid)

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.stop()
        for flow in self.flows.values():
            flow.cancel()
        for flow in list(self.flows.values()):
            with contextlib.suppress(BaseException):
                await flow
