"""Prompts end to end: a real hub app, the operator on the dashboard routes, real runners driving fake_claude."""

from __future__ import annotations

import asyncio
import re

import contextvars

import httpx

from .conftest import OP_IP, OP_LOGIN, FakeWhois
from .test_runner import _connected, fake_claude, fake_wrapper, live_hub, runner_for, stop, wait_for  # noqa: F401


_as_operator = contextvars.ContextVar("as_operator", default=False)


def _enable_operator(hub, monkeypatch) -> None:
    """LiveHub runs --insecure-dev (so agents need no tailnet), which refuses operators. Let the operator checks, and
    only those, see a non-dev hub."""
    from agenthub.hub import dashboard, operator as operator_mod

    original = operator_mod.authenticate_operator

    async def authenticate_operator(conn, db):
        token = _as_operator.set(True)
        try:
            return await original(conn, db)
        finally:
            _as_operator.reset(token)

    class State(type(hub.state)):
        @property
        def insecure_dev(self):
            return not _as_operator.get()

        @insecure_dev.setter
        def insecure_dev(self, value):
            pass

    monkeypatch.setattr(operator_mod, "authenticate_operator", authenticate_operator)
    monkeypatch.setattr(dashboard, "authenticate_operator", authenticate_operator)
    hub.state.__class__ = State


class Operator:
    """The operator's browser, as tailscale serve presents it, against a LiveHub."""

    def __init__(self, hub, monkeypatch) -> None:
        _enable_operator(hub, monkeypatch)
        from agenthub.hub.operator import csrf_token

        whois = FakeWhois()
        whois.add_user(OP_IP, "nphone", OP_LOGIN)
        hub.state.whois = whois
        hub.state.config.operators = [OP_LOGIN]
        self.csrf = csrf_token(hub.state.csrf_secret, OP_LOGIN)
        self.http = httpx.Client(base_url=hub.url, follow_redirects=False, headers={
            "tailscale-user-login": OP_LOGIN, "x-forwarded-for": OP_IP, "sec-fetch-site": "same-origin"})

    def get(self, path: str) -> str:
        r = self.http.get(path)
        assert r.status_code == 200, (path, r.status_code)
        return r.text

    def prompt(self, target: str, body: str) -> int:
        r = self.http.post("/ui/prompts", data={"csrf": self.csrf, "target": target, "body": body})
        assert r.status_code in (200, 303), r.text
        return int(re.search(r"/ui/prompts/(\d+)", r.headers.get("location", "") + r.text).group(1))


def _prompt_status(hub, pid: int) -> str:
    return hub.conn().execute("SELECT status FROM prompts WHERE id = ?", (pid,)).fetchone()["status"]


async def test_group_prompt_from_the_dashboard_to_two_started_shares(live_hub, fake_claude, tmp_path, monkeypatch):
    claude_bin, calls = fake_claude
    monkeypatch.setenv("FAKE_HUB_URL", live_hub.url)
    op = Operator(live_hub, monkeypatch)
    alice, alice_cfg = runner_for(live_hub, tmp_path, claude_bin, name="alice")
    bob, bob_cfg = runner_for(live_hub, tmp_path, claude_bin, name="bob")
    for runner, name in ((alice, "alice"), (bob, "bob")):
        runner.cfg.claude_bin = fake_wrapper(tmp_path, f"claude-{name}", "prompt_share", FAKE_TOKEN=runner.token,
                                             FAKE_NAME=name)
    task_id = live_hub.op_task("Ship the release", assignee="alice")
    live_hub.add(task_id, "bob", stage="works")
    live_hub.move(task_id, "claimed", "in_progress", "blocked")  # live, but nothing for the runners to run
    mains = [asyncio.create_task(alice.run_forever()), asyncio.create_task(bob.run_forever())]
    try:
        await wait_for(lambda: _connected(live_hub, alice_cfg.agent_id) and _connected(live_hub, bob_cfg.agent_id))
        pid = op.prompt(f"task:{task_id}", "Split the release work between you.")
        await wait_for(lambda: _prompt_status(live_hub, pid) == "done")
        page = op.get(f"/ui/prompts/{pid}")
        assert f"Prompt #{pid}" in page and "Share for alice" in page and "Share for bob" in page
        tasks_page = op.get("/ui/tasks")
        assert "Share for alice" in tasks_page and "Share for bob" in tasks_page
        shares = [r["task_id"] for r in live_hub.conn().execute(
            "SELECT task_id FROM prompt_recipients WHERE prompt_id = ?", (pid,)).fetchall()]
        assert len(set(shares)) == 2
        for share in shares:
            assert f"Share of prompt #{pid}" in op.get(f"/ui/tasks/{share}")
        await wait_for(lambda: all(live_hub.task(t)["state"] == "review" for t in shares))
    finally:
        await stop(*mains)
    share_calls = [c for c in calls() if c["env"].get("AGENTHUB_TASK_ID") in {str(t) for t in shares}]
    assert len(share_calls) == 2 and "AGENTHUB_PROMPT_ID" not in share_calls[0]["env"]
    assert len([c for c in calls() if c["env"].get("AGENTHUB_PROMPT_ID") == str(pid)]) == 2


async def test_single_prompt_from_the_dashboard_is_answered_in_the_chat(live_hub, fake_claude, tmp_path, monkeypatch):
    claude_bin, calls = fake_claude
    monkeypatch.setenv("FAKE_HUB_URL", live_hub.url)
    op = Operator(live_hub, monkeypatch)
    runner, cfg = runner_for(live_hub, tmp_path, claude_bin)
    runner.cfg.claude_bin = fake_wrapper(tmp_path, "claude-solo", "prompt_reply", FAKE_TOKEN=runner.token)
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(lambda: _connected(live_hub, cfg.agent_id))
        pid = op.prompt(f"agent:{cfg.agent_id}", "Check why the nightly build is slow.")
        await wait_for(lambda: _prompt_status(live_hub, pid) == "done", timeout=10)
    finally:
        await stop(main)
    assert f"Reply to prompt {pid}: the build is slow." in op.get(f"/ui/chats/{cfg.agent_id}")
    assert "Check why the nightly build is slow." in op.get(f"/ui/prompts/{pid}")
