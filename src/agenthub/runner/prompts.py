"""Prompts from the operator: the runner wakes for them and runs a session on each.

* ``sync`` fetches the prompts this agent still has to act on (``GET /prompts/pending``) and starts one flow
  per prompt. A busy agent (any session running) runs a prompt once all of its running sessions have ended,
  before any new task session, and the prompt's session runs alone (:class:`Slots`); a running session is never
  interrupted.
* Where it runs: a task target in that project's checkout, resuming this agent's latest session on the task
  when there is one (forked, so a task session never shares the id); a project target in that project's
  checkout; an agent or device target in the checkout of the agent's current task's project, else its first
  configured checkout. Without a checkout the prompt is finished as failed with a note.
* The operator's text reaches the session outside any untrusted block, under
  ``From the operator (prompt #N, verified by the hub):``. A single recipient acts on it and answers with
  ``message_operator``; a group discusses it in the prompt's thread, then each claims a share (a task it picks
  up next) or declines.
* Budget: ``prompt_max_turns`` and ``prompt_wall_clock_minutes`` (runner.toml). The subscription usage limit
  pauses the prompt until the reset, then resumes the same session once, unless the prompt was settled meanwhile;
  the hub's prompt stays ``running`` meanwhile. Prompt sessions record no task runs: the outcome is reported with
  ``finish``. A transient hub error before the prompt started leaves it for the next sync.
* Each prompt's claude session id is kept on disk (:class:`PromptSessions`): a prompt a previous runner process
  left ``running`` is resumed once after a restart (started afresh with a note if its session id was never
  seen). If the runner restarts again, or the resumed session fails, the prompt is finished as failed.
* On its first sync the runner fails, instead of running, pending prompts older than :data:`STALE_HOURS` (left
  while it was down or too old to run prompts): the hub checks their age and notes "too old when the runner
  picked it up".
* The heartbeat says ``"prompts": 1``: the hub wakes only agents whose runner does.
* A queued prompt the operator withdrew (no longer pending on the next sync) is dropped at once; one that got
  past the queue is refused by the hub's ``start`` and dropped too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from agenthub import paths, untrusted
from agenthub.client import HubError
from agenthub.config import write_private
from agenthub.hooks.permission import NO_TASK_GRANTS
from agenthub.runner.session import Session, SessionResult, build_command

if TYPE_CHECKING:
    from agenthub.runner.daemon import Runner

log = logging.getLogger("agenthub.runner")

OPEN = ("pending", "running")  # a recipient's statuses that still need its runner
STALE_HOURS = 6  # on its first sync the runner fails pending prompts older than this (the hub checks too)
MAX_NOTE_CHARS = 4000
# An agent or device prompt runs in the checkout of the agent's current task: a task with a session here first,
# then by state.
_STATE_RANK = {"in_progress": 0, "review": 1, "claimed": 2, "open": 3, "blocked": 4}


class Slots:
    """The runner's session slots (``concurrency``). A prompt runs alone, once every running session has ended:
    from the moment it is queued no new task session starts, and none starts while it runs (drain, then run).
    Several queued prompts run one after the other, before any new task session."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.queued: set[int] = set()
        self.running = 0  # task sessions holding a slot
        self.prompt_running = False
        self._changed = asyncio.Event()

    def _notify(self) -> None:
        """Wake everything waiting in :meth:`_until` to re-check its condition."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def _until(self, ready) -> None:
        while not ready():
            await self._changed.wait()

    def queue(self, prompt_id: int) -> None:
        """New task sessions wait from now on until this prompt has run (or is dropped)."""
        self.queued.add(prompt_id)

    def unqueue(self, prompt_id: int) -> None:
        self.queued.discard(prompt_id)
        self._notify()

    @contextlib.asynccontextmanager
    async def task(self) -> AsyncIterator[None]:
        await self._until(lambda: not self.queued and not self.prompt_running and self.running < self.n)
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._notify()

    @contextlib.asynccontextmanager
    async def prompt(self, prompt_id: int) -> AsyncIterator[None]:
        self.queue(prompt_id)
        try:
            await self._until(lambda: self.running == 0 and not self.prompt_running)
            self.prompt_running = True
        finally:
            self.unqueue(prompt_id)
        try:
            yield
        finally:
            self.prompt_running = False
            self._notify()


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
            tmp = self.path.with_name(self.path.name + ".tmp")
            write_private(tmp, json.dumps(data))  # 0600 before it gets its name
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
        "part: it becomes a task assigned to you, already approved"
        + (" (give project=... too if you have no current task)" if p["project"] is None else "")
        + ". If you take no part, call "
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
    if p["target"].startswith("task:"):  # the permission hook asks the operator on that task
        permissions = ("Risky shell commands and tools outside your permissions need operator approval: follow the "
                       "instructions the hooks give you.\n")
    else:
        permissions = ("Risky shell commands need operator approval: follow the instructions the hooks give you. "
                       + NO_TASK_GRANTS + "\n")
    return (
        f"You are the agenthub agent '{agent_name}', running headless (no human at this terminal). The operator "
        f"woke you with prompt #{p['id']}{others}; work on that prompt only.\n"
        "Coordinate through the agenthub MCP tools: read_inbox/post for messages, ask_agent for direct "
        "questions, board_get/board_put for shared notes.\n" + permissions + untrusted.NOTICE + "\n"
        + untrusted.HUMAN_ONLY
    )


def _transient(exc: Exception) -> bool:
    """A hub that is unreachable or failing for now (not a refusal)."""
    return isinstance(exc, httpx.HTTPError) or (isinstance(exc, HubError) and exc.status >= 500)


class PromptRunner:
    """Runs this agent's prompts for a :class:`Runner` (its client, config, slots and task work)."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.store = PromptSessions(paths.data_dir() / "runner" / f"prompt-sessions-{runner.agent.agent_id}.json")
        self.flows: dict[int, asyncio.Task] = {}
        self.sessions: dict[int, Session] = {}
        # prompt id -> (ok, note) of a finish the hub didn't take; sent again on the next sync
        self._unreported: dict[int, tuple[bool, str]] = {}
        # Prompts whose session this process has started (until then, a transient hub error is retried).
        self._launched: set[int] = set()
        # Prompts whose flow waits for the slot to start its first session (dropped if withdrawn meanwhile).
        self._waiting: set[int] = set()
        self._first_sync = True

    async def sync(self) -> None:
        """Start a flow for each prompt this agent still has to act on and isn't handling yet (oldest first). The
        first sync fails pending prompts too old to run instead (:meth:`_fail_stale`)."""
        pending = {p["id"]: p for p in await self.runner.client.pending_prompts()}
        if self._first_sync:
            for pid in await self._fail_stale(pending):
                del pending[pid]
            self._first_sync = False
        for pid in [pid for pid, flow in self.flows.items() if flow.done() and pid not in pending]:
            del self.flows[pid]
            self._unreported.pop(pid, None)
        for pid in [pid for pid in self._waiting if pid not in pending]:  # e.g. withdrawn by the operator
            log.info("prompt %s is no longer pending for this agent; dropping it", pid)
            self._waiting.discard(pid)
            self.flows[pid].cancel()  # its flow unqueues it, so new task sessions stop waiting for it
        for pid, p in pending.items():
            flow = self.flows.get(pid)
            if flow is None:
                self.runner.slots.queue(pid)  # before any task loop can take the next slot
                self.flows[pid] = asyncio.create_task(self._flow(p), name=f"prompt-{pid}")
            elif flow.done() and pid in self._unreported:
                await self._report(pid, *self._unreported[pid])

    async def _fail_stale(self, pending: dict[int, dict[str, Any]]) -> list[int]:
        """Fail the pending prompts older than :data:`STALE_HOURS`; returns those the hub failed. One the hub
        finds not that old (its clock decides) is left to run."""
        cutoff = datetime.now(UTC) - timedelta(hours=STALE_HOURS)
        failed = []
        for pid, p in pending.items():
            if p["my_status"] != "pending" or datetime.fromisoformat(p["created_at"]) >= cutoff:
                continue
            try:
                await self.runner.client.finish_prompt(pid, False, stale=True)
            except HubError as exc:
                if exc.status not in (404, 409):
                    raise
                log.info("prompt %s: not failed as too old (%s)", pid, exc.detail)
                continue
            log.info("prompt %s was pending for more than %s hours; failed instead of run", pid, STALE_HOURS)
            failed.append(pid)
        return failed

    async def _flow(self, summary: dict[str, Any]) -> None:
        pid = summary["id"]
        try:
            await self._run(pid, restarted=summary["my_status"] == "running")
        except Exception as exc:  # noqa: BLE001 - one prompt's failure must not kill the runner
            if pid not in self._launched and _transient(exc):
                log.warning("prompt %s: hub unavailable before it started (%s); trying again on the next sync",
                            pid, exc)
                self.flows.pop(pid, None)  # the next sync starts it afresh
                return
            log.exception("prompt %s failed in the runner", pid)
            await self._report(pid, False, "runner error; see runner logs")
        finally:
            self._launched.discard(pid)
            self._waiting.discard(pid)
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
        waited_for_limit, fork = False, False
        while True:
            if pid not in self._launched:
                self._waiting.add(pid)
            async with self.runner.slots.prompt(pid):
                self._waiting.discard(pid)
                if pid not in self._launched:  # the first session starts now, when a slot is free
                    if restarted:
                        self.store.update(pid, restarts=record.get("restarts", 0) + 1)
                        log.info("prompt %s: resuming after a runner restart", pid)
                    elif not await self._start(pid):
                        return
                    self._launched.add(pid)
                    if not restarted and task_id is not None:
                        resume = await self._task_session(task_id)
                        fork = resume is not None  # the task's session stays the task's
                began = time.monotonic()
                result = await self._session(p, text, resume=resume, fork=fork, cwd=cwd, project=project,
                                             task_id=task_id, turns=turns, seconds=seconds)
                fork = False
            turns -= result.num_turns or 0
            seconds -= time.monotonic() - began
            resume = result.session_id or resume
            if result.outcome == "usage_limit" and not waited_for_limit:
                waited_for_limit = True
                self.runner.slots.queue(pid)  # no new task session starts meanwhile; this prompt goes first after
                await self.runner.wait_for_limit_reset(result.resets_at, what=f"prompt {pid}")
                if (await client.get_prompt(pid))["my_status"] not in OPEN:
                    log.info("prompt %s was settled while waiting for the usage limit; not resuming it", pid)
                    self.store.drop(pid)
                    return
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
        """This agent's latest session on the task, to resume (forked), if any. None while a run of this agent on
        the task is in progress (here or as the hub sees it): its session is never forked while in use."""
        work = self.runner.work.get(task_id)
        if work is not None and work.session is not None:
            return None
        me = self.runner.agent.agent_id
        try:
            found = await self.runner.client.task_runs(task_id, agent=me)
        except (HubError, httpx.HTTPError) as exc:
            log.warning("could not read the runs of task %s: %s", task_id, exc)
            return None
        if any(r["agent_id"] == me and r["ended_at"] is None for r in found["runs"]):
            log.info("task %s has a run in progress; its prompt starts a new session", task_id)
            return None
        return found["usage"]["last_session_id"]

    async def _session(self, p: dict[str, Any], text: str, *, resume: str | None, fork: bool, cwd: Path,
                       project: str | None, task_id: int | None, turns: int, seconds: float) -> SessionResult:
        cfg, pid = self.runner.cfg, p["id"]
        cmd = build_command(cfg.claude_bin, max_turns=max(turns, 1),
                            system_prompt=system_prompt(self.runner.agent.name, p), resume_session=resume,
                            extra_args=cfg.extra_args, permission_mode=cfg.permission_mode, fork_session=fork)
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

        async def on_session(session_id: str) -> None:  # the prompt's own id (a new one when forked)
            self.store.update(pid, session_id=session_id)

        session = Session(cmd, prompt=text, cwd=cwd, env=env, timeout_seconds=max(seconds, 0),
                          on_progress=on_progress, on_session=on_session)
        self.sessions[pid] = session
        self.runner._session_dirs[session] = str(cwd)
        try:
            try:
                return await session.run()
            finally:
                self.runner.last_model = session.model or self.runner.last_model
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
