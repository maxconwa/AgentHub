"""The runner's wake paths against the hub's real wake path (``chats.post``, 0.3.9.1), with no overlay on the delivery
rows: task, project, broadcast, a direct chat with no shared project, paused wakes, and a changes verdict (one
session for the builder, not two). From hub-reviewer's checks on task #36."""
from __future__ import annotations

import asyncio
import json

from agenthub.hub import chats, db, store, task_agents
from agenthub.runner.chat import ChatState

from .test_chat_runner import chat_runner, states
from .test_runner import fake_claude, live_hub, stop, wait_for  # noqa: F401
from .test_runner_wakes import PEER, connected, deliveries, peer, spy_runs, untrusted_from_peer


def real_post(hub, author_id, kind, ref, body, to=None):
    conn = hub.conn()
    with db.transaction(conn):
        return chats.post(conn, store.find_agent(conn, author_id), kind, ref, body, to=to)


async def test_real_task_wake(live_hub, fake_claude, tmp_path):
    claude_bin, calls = fake_claude
    runner, cfg = chat_runner(live_hub, tmp_path, claude_bin)
    task_id = live_hub.op_task("Add health check", assignee="worker")
    waker = peer(live_hub, tmp_path)
    live_hub.add(task_id, PEER, "reviews")
    runs = spy_runs(runner)
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(lambda: live_hub.task(task_id)["state"] == "review"
                       and (task_id not in runner.work or runner.work[task_id].task.done()))
        msg = real_post(live_hub, waker, "task", str(task_id), "Why port 8080?", to=cfg.agent_id)
        assert msg["chain_id"] and msg["wakes"][0]["wake"]
        await wait_for(lambda: states(live_hub, "task", str(task_id)).count("finished") >= 2)
    finally:
        await stop(main)
    woke = [c for c in calls() if f"#{msg['id']}" in c["stdin"]]
    assert len(woke) == 1
    (d,) = deliveries(live_hub, msg)
    assert d["mode"] == "woke"
    assert untrusted_from_peer(woke[0]["stdin"], msg, waker) and "From the operator" not in woke[0]["stdin"]
    assert [r["chain_ids"] for r in runs if r["what"] == "chat"] == [[msg["chain_id"]]]
    stored = live_hub.conn().execute("SELECT chain_ids FROM chat_runs ORDER BY id DESC").fetchone()[0]
    assert json.loads(stored) == [msg["chain_id"]]
    lines = [r for r in chats.messages(live_hub.conn(), "task", str(task_id))
             if r["kind"] == "status" and r["meta"].get("state") == "woke"]
    assert lines and lines[-1]["meta"]["waker"] == waker


async def test_real_project_wake(live_hub, fake_claude, tmp_path):
    claude_bin, calls = fake_claude
    runner, cfg = chat_runner(live_hub, tmp_path, claude_bin)
    me = cfg.agent_id
    waker = peer(live_hub, tmp_path)
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(connected(live_hub, me))
        msg = real_post(live_hub, waker, "project", "proj", "Who takes the docs?", to=me)
        await wait_for(lambda: "finished" in states(live_hub, "project", "proj"))
    finally:
        await stop(main)
    (one,) = calls()
    assert one["env"]["AGENTHUB_CHAT"] == "project/proj" and one["cwd"] == str(tmp_path / "checkout")
    assert untrusted_from_peer(one["stdin"], msg, waker)


async def test_real_broadcast_starts_nothing(live_hub, fake_claude, tmp_path):
    claude_bin, calls = fake_claude
    runner, cfg = chat_runner(live_hub, tmp_path, claude_bin)
    waker = peer(live_hub, tmp_path)
    msg = real_post(live_hub, waker, "project", "proj", "FYI merged")
    assert msg["broadcast"]
    await runner.chats.sync()
    await runner.chats.sync()
    await asyncio.sleep(0.3)
    assert calls() == []
    (d,) = [x for x in deliveries(live_hub, msg) if x["agent_id"] == cfg.agent_id]
    assert d["status"] == "pending"


async def test_real_dm_no_shared_project(live_hub, fake_claude, tmp_path):
    claude_bin, calls = fake_claude
    runner, cfg = chat_runner(live_hub, tmp_path, claude_bin)
    me = cfg.agent_id
    waker = peer(live_hub, tmp_path, projects=("elsewhere",))
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(connected(live_hub, me))
        msg = real_post(live_hub, waker, "dm", "worker", "Can you look at my branch?")
        await wait_for(lambda: "finished" in states(live_hub, "agent", me))
    finally:
        await stop(main)
    (call,) = calls()
    assert call["env"]["AGENTHUB_CHAT"] == "agent/me" and call["cwd"] == str(tmp_path / "checkout")
    assert untrusted_from_peer(call["stdin"], msg, waker)
    assert f"chat_post(chat='dm:{PEER}', body=...)" in call["stdin"]


async def test_real_paused_agent_not_woken(live_hub, fake_claude, tmp_path):
    claude_bin, calls = fake_claude
    runner, cfg = chat_runner(live_hub, tmp_path, claude_bin)
    me = cfg.agent_id
    from agenthub.hub import wakes
    conn = live_hub.conn()
    with db.transaction(conn):
        wakes.set_agent_paused(conn, me, True, actor="human:op@example.com")
    waker = peer(live_hub, tmp_path)
    msg = real_post(live_hub, waker, "dm", "worker", "Wake up")
    await runner.chats.sync()
    await runner.chats.sync()
    await asyncio.sleep(0.3)
    assert calls() == []
    (d,) = deliveries(live_hub, msg)
    assert d["status"] == "pending"


async def test_real_changes_verdict_starts_one_session(live_hub, fake_claude, tmp_path):
    """A changes verdict both moves the task back and posts to the builder in its chat: the builder's runner runs
    the one "returned" session, which takes the reviewer's message in, rather than a second one for the message."""
    claude_bin, calls = fake_claude
    runner, cfg = chat_runner(live_hub, tmp_path, claude_bin)
    task_id = live_hub.op_task("Add health check", assignee="worker")
    waker = peer(live_hub, tmp_path)
    live_hub.add(task_id, PEER, "reviews")
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(lambda: live_hub.task(task_id)["state"] == "review"
                       and (task_id not in runner.work or runner.work[task_id].task.done()))
        before = len(calls())
        conn = live_hub.conn()
        with db.transaction(conn):
            task_agents.record_verdict(conn, task_id, store.find_agent(conn, waker), verdict="changes",
                                       notes="rename the flag")
        await wait_for(lambda: live_hub.task(task_id)["state"] == "review", timeout=30)
        await asyncio.sleep(3)
    finally:
        await stop(main)
    (session,) = calls()[before:]
    assert "--resume" in session["argv"] and "sent back to in_progress" in session["stdin"]
    assert "rename the flag" in session["stdin"]
    (msg,) = [r for r in chats.messages(live_hub.conn(), "task", str(task_id)) if r["meta"].get("send_back")]
    (d,) = deliveries(live_hub, msg)
    assert d["mode"] == "queued"
