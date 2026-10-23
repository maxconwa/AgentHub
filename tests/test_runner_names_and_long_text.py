"""The runner's half of 0.3.9.0: names with capitals and operator messages of any length.

* A name that leaves the hub for a system that can't take capitals (systemd units, launchd labels, config folders,
  git branches) goes through ``agenthub.names.slug``; a lowercase name is its own slug, so nothing existing moves.
* runner.toml's project keys match ignoring case, and two keys that differ only in case are a config error.
* An operator's message reaches the Claude session byte for byte however long it is: on the first turn, mid-turn,
  and into a resumed session.
"""

from __future__ import annotations

import asyncio
import plistlib
import stat
from pathlib import Path

import pytest

from agenthub import join, paths
from agenthub.runner import service
from agenthub.runner.chat import chat_system_prompt
from agenthub.runner.config import RunnerConfigError, load_runner_config
from agenthub.runner.daemon import system_prompt
from agenthub.runner.session import ARGV_TEXT_MAX, Session, build_command, lines, spill_system_prompt

from .test_chat_runner import chat_runner, heading, mode, running, send, states, turns_log
from .test_runner import fake_claude, live_hub, stop, wait_for  # noqa: F401 - fixtures

LONG = 200_000


def long_text(n: int = LONG) -> str:
    """``n`` characters of everything a pasted message may hold: several scripts, emoji, quotes, backslashes, tabs,
    blank lines, JSON and markup, with a unique marker at each end so a cut anywhere shows."""
    piece = ('Line {i}: naïve café — 漢字かな 🙂👍🏽 "quoted" \\back\\slash\\ \t{{"json": [1, 2]}} <b>&amp;</b>\n\n')
    out, i = [], 0
    while sum(map(len, out)) < n:
        out.append(piece.format(i=i))
        i += 1
    body = "BEGIN>" + "".join(out)
    return body[:n - 4] + "<END"


# --- names with capitals ---------------------------------------------------------------------------------------------

def test_a_capitalised_agent_gets_lowercase_unit_label_log_and_folder(tmp_path, monkeypatch):
    inst = service.instance(tmp_path / "agenthub-hub-builder-1", "Hub-Builder-1")
    assert inst == "hub-builder-1"
    assert service.unit_name(inst) == "agenthub-runner-hub-builder-1.service"
    assert service.write_unit("/opt/agenthub", tmp_path / "cfg", directory=tmp_path, inst=inst).name == \
        "agenthub-runner-hub-builder-1.service"
    assert service.launchd_label(inst) == "com.agenthub.runner.hub-builder-1"
    plist = service.write_plist("/opt/agenthub", tmp_path / "cfg", directory=tmp_path, inst=inst)
    data = plistlib.loads(plist.read_bytes())
    assert plist.name == "com.agenthub.runner.hub-builder-1.plist"
    assert data["Label"] == "com.agenthub.runner.hub-builder-1"
    assert data["StandardOutPath"].endswith("/agenthub-runner-hub-builder-1.log")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.agenthub.runner.hub-builder-1")
    assert service.revoked_exit_code() == 0
    # `agenthub join` points a second agent at its own config folder, by slug
    paths.config_dir().mkdir(parents=True)
    paths.agent_config_path().write_text("")
    with pytest.raises(join.JoinError, match="--config-dir ~/.config/agenthub-hub-builder-1 join"):
        asyncio.run(join.enroll("https://hub.example", "CODE", "Hub-Builder-1"))


def test_lowercase_names_keep_their_unit_label_and_folder(tmp_path):
    assert service.instance(paths.default_config_dir(), "Hub-Builder-1") is None  # the first agent: plain names
    inst = service.instance(tmp_path / "agenthub-hub-builder-1", "hub-builder-1")
    assert inst == "hub-builder-1"
    assert service.unit_name(inst) == "agenthub-runner-hub-builder-1.service"
    assert service.launchd_label(inst) == "com.agenthub.runner.hub-builder-1"
    assert service.instance(tmp_path / "x", "tester_2") == "tester_2"


def test_sessions_name_git_branches_by_the_agents_slug():
    task = {"id": 7, "project": "Agent-Hub"}
    works = system_prompt("Hub-Builder-1", task, {"role": "", "stage": "works"})
    assert "You are the agenthub agent 'Hub-Builder-1'" in works  # shown as written
    assert "agent/hub-builder-1-<what it is for>" in works and "agent/hub-builder-1-task7" in works
    assert "agent/Hub-Builder-1" not in works
    chat = chat_system_prompt("Hub-Builder-1", {"Agent-Hub": "/src/agent-hub"}, Path("/src/agent-hub"))
    assert "agent/hub-builder-1-<what it is for>" in chat and "Agent-Hub: /src/agent-hub" in chat
    assert "agent/worker-task3" in system_prompt("worker", {"id": 3, "project": "p"}, {"role": "", "stage": "works"})


# --- runner.toml project keys ----------------------------------------------------------------------------------------

def test_runner_toml_project_keys_match_ignoring_case(tmp_path):
    (tmp_path / "hub").mkdir()
    path = tmp_path / "runner.toml"
    path.write_text(f'[projects]\nAgent-Hub = "{tmp_path / "hub"}"\nsite = "~/site"\n')
    cfg = load_runner_config(path)
    assert cfg.projects == {"Agent-Hub": str(tmp_path / "hub"), "site": "~/site"}  # kept as written
    for spelling in ("Agent-Hub", "agent-hub", "AGENT-HUB", "agent-HUB"):
        assert cfg.project_dir(spelling) == tmp_path / "hub"
    assert cfg.project_dir("Site") == Path("~/site").expanduser()
    assert cfg.project_dir("agent-hub-2") is None


def test_runner_toml_keys_differing_only_in_case_are_a_config_error(tmp_path):
    path = tmp_path / "runner.toml"
    path.write_text('[projects]\nagent-hub = "/a"\nother = "/o"\nAgent-Hub = "/b"\n')
    with pytest.raises(RunnerConfigError, match="both 'agent-hub' and 'Agent-Hub'.*ignore case"):
        load_runner_config(path)


# --- text of any length ----------------------------------------------------------------------------------------------

async def test_output_lines_of_any_length_are_read_whole():
    reader = asyncio.StreamReader(limit=1024)
    huge = b"x" * (20 * 1024 * 1024)  # longer than any StreamReader limit the runner ever had (16 MiB)
    for part in (b'{"a":1}\n' + huge[:5000], huge[5000:], b"\n\n", b"tail"):
        reader.feed_data(part)
    reader.feed_eof()
    got = [line async for line in lines(reader)]
    assert got == [b'{"a":1}\n', huge + b"\n", b"\n", b"tail"]


def test_a_long_system_prompt_goes_in_a_private_file():
    cmd = build_command("claude", max_turns=None, system_prompt="short", resume_session=None, extra_args=["-x"])
    assert spill_system_prompt(cmd) == (cmd, None)
    text = "é" * (ARGV_TEXT_MAX // 2 + 1)  # over the limit in bytes, not in characters
    cmd = build_command("claude", max_turns=None, system_prompt=text, resume_session="s1", extra_args=["-x"])
    spilled, file = spill_system_prompt(cmd)
    try:
        i = spilled.index("--append-system-prompt-file")
        assert spilled[i + 1] == str(file) and "--append-system-prompt" not in spilled and text not in spilled
        assert spilled[:i] == cmd[:cmd.index("--append-system-prompt")] and spilled[-3:] == ["--resume", "s1", "-x"]
        assert file.read_text(encoding="utf-8") == text
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    finally:
        file.unlink()


async def test_a_session_gets_any_amount_of_text_whole(fake_claude, tmp_path, monkeypatch):
    """The first prompt, a message delivered mid-turn (longer than the old 16 MiB output-line limit, which its echo
    would have broken) and a system prompt too long for argv all reach claude whole; the system prompt's file is
    gone afterwards."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hold")
    release = tmp_path / "release"
    monkeypatch.setenv("FAKE_RELEASE", str(release))
    turns = turns_log(tmp_path, monkeypatch)
    claude_bin, calls = fake_claude
    prompt, system, later = long_text(), long_text(150_000), "y" * (17 * 1024 * 1024)
    cmd = build_command(claude_bin, max_turns=None, system_prompt=system, resume_session=None, extra_args=[])
    replays = []

    async def on_replay(tag):
        replays.append(tag)

    async def nothing(_):
        pass

    session = Session(cmd, prompt=prompt, cwd=tmp_path, env={}, timeout_seconds=60, on_progress=nothing,
                      on_session=nothing, on_replay=on_replay)
    run = asyncio.create_task(session.run())
    await wait_for(lambda: session.accepting and calls())
    assert await session.deliver(later, tag="later")
    await asyncio.sleep(0.3)
    release.touch()
    result = await run
    assert result.outcome == "success" and result.undelivered == []
    assert replays == [None, "later"]
    (call,) = calls()
    assert call["stdin"] == prompt and call["system"] == system
    assert "--append-system-prompt-file" in call["argv"]
    assert not Path(call["argv"][call["argv"].index("--append-system-prompt-file") + 1]).exists()
    (turn,) = turns()
    assert turn["mid_turn"] and turn["text"] == later


async def test_a_200k_operator_message_reaches_the_chat_session_and_its_resume_byte_for_byte(
        live_hub, fake_claude, tmp_path):
    claude_bin, calls = fake_claude
    runner, agent_cfg = chat_runner(live_hub, tmp_path, claude_bin)
    me = agent_cfg.agent_id
    first_body, second_body = long_text(), long_text()[::-1]
    assert len(first_body) == len(second_body) == LONG
    first = send(live_hub, "agent", me, first_body)
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(lambda: states(live_hub, "agent", me) == ["started", "finished"])
        second = send(live_hub, "agent", me, second_body)
        await wait_for(lambda: states(live_hub, "agent", me).count("finished") == 2)
    finally:
        await stop(main)
    one, two = calls()
    assert mode(live_hub, first) == mode(live_hub, second) == "woke"
    assert "--resume" not in one["argv"] and two["argv"][two["argv"].index("--resume") + 1] == one["session"]
    for call, message, body in ((one, first, first_body), (two, second, second_body)):
        stdin = call["stdin"].encode()
        expected = (heading({**message, "body": body}, "your chat with the operator") + "\n\n").encode()
        assert expected in stdin and stdin.count(body.encode()) == 1
        assert all(len(arg) < 20_000 for arg in call["argv"])  # none of it went through argv ...
        assert all(len(v) < 1000 for v in call["env"].values())  # ... or the environment


async def test_a_200k_operator_message_reaches_a_running_task_session_mid_turn_byte_for_byte(
        live_hub, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hold")
    release = tmp_path / "release"
    monkeypatch.setenv("FAKE_RELEASE", str(release))
    turns = turns_log(tmp_path, monkeypatch)
    claude_bin, calls = fake_claude
    runner, _ = chat_runner(live_hub, tmp_path, claude_bin)
    task_id = live_hub.op_task("Add health check", assignee="worker")
    body = long_text()
    main = asyncio.create_task(runner.run_forever())
    try:
        await wait_for(lambda: running(runner, task_id))
        msg = send(live_hub, "task", str(task_id), body)
        await wait_for(lambda: mode(live_hub, msg) == "live")
        await asyncio.sleep(0.3)
        release.touch()
        await wait_for(lambda: "finished" in states(live_hub, "task", str(task_id)))
    finally:
        await stop(main)
    assert len(calls()) == 1
    (turn,) = turns()
    expected = heading({**msg, "body": body}, f"the chat of task #{task_id} (project proj)") + "\n\n"
    assert turn["mid_turn"] and expected.encode() in turn["text"].encode()
