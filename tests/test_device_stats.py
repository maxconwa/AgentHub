"""Device usage stats (0.3.10, task #42): the runner's collector, the heartbeat's `stats`, the hub's validation,
its one row per device plus in-memory ring, and the operator JSON."""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from agenthub.hub import db, device_stats, views
from agenthub.runner import stats
from agenthub.runner.config import RunnerConfigError, load_runner_config
from agenthub.runner.stats import Collector, parse_nvidia_smi

from .test_runner import _FakeWs, fake_claude, live_hub, runner_for, wait_for  # noqa: F401 - fixtures

GOOD = {"cpu_pct": 37.5, "mem_used": 12 << 30, "mem_total": 32 << 30,
        "gpus": [{"index": 0, "name": "NVIDIA GeForce RTX 5070", "mem_used": 2 << 30, "mem_total": 12 << 30,
                  "util_pct": 12.0}]}


# --- collector: nvidia-smi ---------------------------------------------------------------------------------------


def test_parse_nvidia_smi_two_gpus_commas_in_names_and_na_utilisation():
    out = ("0, NVIDIA GeForce RTX 5070, 2048, 12227, 34\n"
           "1, Tesla V100-SXM2-16GB, Rev, A, 0, 16384, [N/A]\n")
    assert parse_nvidia_smi(out) == [
        {"index": 0, "name": "NVIDIA GeForce RTX 5070", "mem_used": 2048 * 2**20, "mem_total": 12227 * 2**20,
         "util_pct": 34.0},
        {"index": 1, "name": "Tesla V100-SXM2-16GB, Rev, A", "mem_used": 0, "mem_total": 16384 * 2**20,
         "util_pct": None},
    ]


@pytest.mark.parametrize("out", [
    "", "garbage\n", "No devices were found\n", "a, b, c, d, e\n", "0, gpu, [N/A], [N/A], 5\n",
    "0, gpu, 9000, 100, 5\n",  # used > total
    "0, gpu, nan, 100, 5\n", "0, gpu, 10, inf, 5\n", "0, gpu, 10, 0, 5\n", "-1, gpu, 1, 2, 3\n",
    "0, bad\x07name, 1, 2, 3\n", "\x00\xff\n",
])
def test_parse_nvidia_smi_skips_garbage(out):
    assert parse_nvidia_smi(out) == []


def test_parse_nvidia_smi_keeps_good_lines_among_bad_and_caps_count_and_utilisation():
    lines = ["junk"] + [f"{i}, gpu{i}, 1, 2, 150" for i in range(40)]
    gpus = parse_nvidia_smi("\n".join(lines))
    assert len(gpus) == stats.MAX_GPUS and gpus[0]["util_pct"] is None  # 150 % isn't a percentage


def _fake_smi(tmp_path, body: str):
    script = tmp_path / "nvidia-smi"
    script.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_no_nvidia_smi_means_no_gpus_and_no_subprocess(monkeypatch):
    looked = []
    monkeypatch.setattr(stats.shutil, "which", lambda name: looked.append(name))
    monkeypatch.setattr(stats.subprocess, "run", lambda *a, **k: pytest.fail("ran a subprocess"))
    c = Collector()
    assert c.gpus() == [] and c.gpus() == []
    assert looked == ["nvidia-smi"]  # looked up once; "not present" is remembered


def test_nvidia_smi_is_parsed(tmp_path):
    c = Collector(nvidia_smi=_fake_smi(tmp_path, 'print("0, NVIDIA RTX 5070, 100, 1000, 7")'))
    assert c.gpus() == [{"index": 0, "name": "NVIDIA RTX 5070", "mem_used": 100 << 20, "mem_total": 1000 << 20,
                         "util_pct": 7.0}]


def test_failing_nvidia_smi_gives_no_gpus_and_backs_off(tmp_path):
    calls = tmp_path / "calls"
    now = [100.0]
    c = Collector(nvidia_smi=_fake_smi(tmp_path, f'open({str(calls)!r}, "a").write("x"); sys.exit(1)'),
                  clock=lambda: now[0])
    assert c.gpus() == [] and c.gpus() == []
    assert calls.read_text() == "x"  # the second call was within the back-off
    now[0] += stats.GPU_BACKOFF_SECONDS + 1
    assert c.gpus() == []
    assert calls.read_text() == "xx"


def test_garbage_from_nvidia_smi_gives_no_gpus(tmp_path):
    c = Collector(nvidia_smi=_fake_smi(tmp_path, 'sys.stdout.buffer.write(b"\\xff\\xfe garbage, , ,\\n")'))
    assert c.gpus() == []


def test_hung_nvidia_smi_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "NVIDIA_SMI_TIMEOUT", 0.3)
    c = Collector(nvidia_smi=_fake_smi(tmp_path, "time.sleep(30)"))
    started = time.monotonic()
    assert c.gpus() == []
    assert time.monotonic() - started < 5


def test_nvidia_smi_that_vanished_gives_no_gpus(tmp_path):
    assert Collector(nvidia_smi=str(tmp_path / "gone")).gpus() == []


def test_nvidia_smi_call_has_a_timeout_and_no_shell(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs, args=args)
        return subprocess.CompletedProcess(args, 0, stdout="0, g, 1, 2, 3\n", stderr="")

    monkeypatch.setattr(stats.subprocess, "run", fake_run)
    assert len(Collector(nvidia_smi="/usr/bin/nvidia-smi").gpus()) == 1
    assert seen["timeout"] == stats.NVIDIA_SMI_TIMEOUT <= 2 and not seen.get("shell")
    assert seen["args"][0] == "/usr/bin/nvidia-smi" and "--format=csv,noheader,nounits" in seen["args"]


# --- collector: CPU and memory -----------------------------------------------------------------------------------


def _proc(tmp_path, *, busy: int, idle: int, total_kb=32 * 1024 * 1024, available_kb=8 * 1024 * 1024):
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    # user nice system idle iowait irq softirq steal guest guest_nice
    (proc / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 2 3 4\nintr 1\n")
    (proc / "meminfo").write_text(f"MemTotal:       {total_kb} kB\nMemFree:  1 kB\n"
                                  f"MemAvailable:   {available_kb} kB\n")
    return proc


def test_proc_cpu_is_a_delta_between_samples_and_memory_is_total_minus_available(tmp_path):
    proc = _proc(tmp_path, busy=100, idle=100)
    c = Collector(proc=proc, nvidia_smi=None, platform="linux")
    assert c.cpu_pct() is None  # the first call reads twice, 0.2 s apart; no time passed in this fake /proc
    _proc(tmp_path, busy=175, idle=125)
    assert c.cpu_pct() == 75.0
    _proc(tmp_path, busy=175, idle=225)
    assert c.memory() == (24 << 30, 32 << 30)
    sample = c.sample()
    assert sample == {"cpu_pct": 0.0, "mem_used": 24 << 30, "mem_total": 32 << 30, "gpus": []}
    assert device_stats.validate(sample) == sample  # what the runner sends, the hub accepts


@pytest.mark.parametrize("stat, meminfo", [
    (None, None), ("", ""), ("cpu x y z\n", "MemTotal: lots\n"), ("intr 1\n", "MemTotal: 10 kB\n"),
    ("cpu 1 2\n", "MemTotal: 10 kB\nMemAvailable: 20 kB\n"),
])
def test_unreadable_proc_means_unavailable_not_an_error(tmp_path, stat, meminfo):
    proc = tmp_path / "proc"
    proc.mkdir()
    if stat is not None:
        (proc / "stat").write_text(stat)
        (proc / "meminfo").write_text(meminfo)
    sample = Collector(proc=proc, nvidia_smi=None, platform="linux").sample()
    assert sample == {"cpu_pct": None, "mem_used": None, "mem_total": None, "gpus": []}


def test_without_proc_or_psutil_cpu_and_memory_are_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)  # makes `import psutil` fail
    sample = Collector(nvidia_smi=None, platform="darwin").sample()
    assert sample == {"cpu_pct": None, "mem_used": None, "mem_total": None, "gpus": []}


def test_psutil_is_used_off_linux_when_installed(monkeypatch):
    class VM:
        total, available = 16 << 30, 4 << 30

    class FakePsutil:
        calls = 0

        @classmethod
        def cpu_percent(cls, interval=None):
            assert interval is None  # never a blocking interval
            cls.calls += 1
            return 0.0 if cls.calls == 1 else 42.25

        @staticmethod
        def virtual_memory():
            return VM

    monkeypatch.setitem(sys.modules, "psutil", FakePsutil)
    sample = Collector(nvidia_smi=None, platform="darwin").sample()
    assert sample == {"cpu_pct": 42.2, "mem_used": 12 << 30, "mem_total": 16 << 30, "gpus": []}


def test_the_real_machine_sample_is_valid():
    sample = Collector(nvidia_smi=None).sample()
    assert device_stats.validate(sample) == sample


# --- runner: the heartbeat -----------------------------------------------------------------------------------------


async def test_heartbeat_sends_the_latest_sample_only_while_it_is_fresh(live_hub, fake_claude, tmp_path):
    runner, _ = runner_for(live_hub, tmp_path, fake_claude[0])
    assert "stats" not in runner._heartbeat_frame()  # nothing sampled yet: the heartbeat doesn't wait
    runner._stats = (time.monotonic(), GOOD)
    assert runner._heartbeat_frame()["stats"] == GOOD
    runner._stats = (time.monotonic() - 3 * runner.cfg.heartbeat_seconds, GOOD)
    assert "stats" not in runner._heartbeat_frame()  # its sampler is stuck: send nothing rather than old figures


async def test_a_slow_sample_never_holds_up_the_heartbeat(live_hub, fake_claude, tmp_path):
    runner, _ = runner_for(live_hub, tmp_path, fake_claude[0])

    class Slow:
        def sample(self):
            time.sleep(1.5)  # e.g. nvidia-smi hanging up to its timeout
            return GOOD

    runner.stats = Slow()
    fake = _FakeWs()
    sampler = asyncio.create_task(runner._stats_loop())
    beat = asyncio.create_task(runner._heartbeat(fake))
    started = time.monotonic()
    await wait_for(lambda: len(fake.frames) >= 3)
    assert time.monotonic() - started < 1.2 and all("stats" not in f for f in fake.frames)
    await wait_for(lambda: "stats" in fake.frames[-1])
    assert fake.frames[-1]["stats"] == GOOD
    beat.cancel()
    sampler.cancel()


async def test_a_failing_sample_is_skipped_and_the_loop_goes_on(live_hub, fake_claude, tmp_path):
    runner, _ = runner_for(live_hub, tmp_path, fake_claude[0])
    calls = []

    class Flaky:
        def sample(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return GOOD

    runner.stats = Flaky()
    sampler = asyncio.create_task(runner._stats_loop())
    await wait_for(lambda: runner._stats is not None)
    assert runner._stats[1] == GOOD and len(calls) >= 2
    sampler.cancel()


async def test_runner_reports_stats_to_the_hub_end_to_end(live_hub, fake_claude, tmp_path):
    runner, _ = runner_for(live_hub, tmp_path, fake_claude[0])
    run = asyncio.create_task(runner.run_forever())
    conn = db.connect(live_hub.state.config.db_path)
    await wait_for(lambda: conn.execute("SELECT sample FROM device_stats").fetchone())
    assert json.loads(conn.execute("SELECT sample FROM device_stats").fetchone()[0]) == {
        "cpu_pct": 1.0, "mem_used": 1, "mem_total": 2, "gpus": []}  # conftest's QuietCollector
    runner._stopping = True
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


def test_device_stats_can_be_turned_off(tmp_path):
    path = tmp_path / "runner.toml"
    path.write_text("device_stats = false\n")
    assert load_runner_config(path).device_stats is False
    path.write_text('device_stats = "no"\n')
    with pytest.raises(RunnerConfigError, match="device_stats"):
        load_runner_config(path)


async def test_with_device_stats_off_no_sampler_runs(live_hub, fake_claude, tmp_path, monkeypatch):
    runner, _ = runner_for(live_hub, tmp_path, fake_claude[0])
    runner.cfg.device_stats = False
    monkeypatch.setattr(runner, "_stats_loop", lambda: pytest.fail("sampler started"))
    run = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.5)
    assert runner._stats is None
    runner._stopping = True
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


# --- hub: validation -----------------------------------------------------------------------------------------------


def _with(**changes):
    return {**GOOD, **changes}


def _gpu(**changes):
    return _with(gpus=[{**GOOD["gpus"][0], **changes}])


def test_validate_accepts_the_pinned_shape_and_drops_unknown_keys():
    assert device_stats.validate(GOOD) == GOOD
    assert device_stats.validate({**GOOD, "node_id": "nother", "extra": 1}) == GOOD
    nulls = {"cpu_pct": None, "mem_used": None, "mem_total": None, "gpus": []}
    assert device_stats.validate(nulls) == nulls
    assert device_stats.validate({}) == nulls  # every figure unavailable
    assert device_stats.validate(_with(cpu_pct=0))["cpu_pct"] == 0.0  # 0 % is a reading, not missing


@pytest.mark.parametrize("bad", [
    None, [], "x", 1,
    _with(cpu_pct=math.nan), _with(cpu_pct=math.inf), _with(cpu_pct=-0.1), _with(cpu_pct=100.5),
    _with(cpu_pct=True), _with(cpu_pct="5"),
    _with(mem_used=10, mem_total=5), _with(mem_used=-1), _with(mem_used=None), _with(mem_total=None),
    _with(mem_used=1.5), _with(mem_total=(1 << 50) + 1), _with(mem_used=True, mem_total=True),
    _with(gpus=None), _with(gpus={}), _with(gpus=["x"]), _with(gpus=[GOOD["gpus"][0]] * 17),
    _gpu(index=-1), _gpu(index=256), _gpu(index="0"), _gpu(index=False),
    _gpu(name=None), _gpu(name="x" * 101), _gpu(name="evil\nname"), _gpu(name="\x1b[31m"),
    _gpu(mem_used=None), _gpu(mem_used=None, mem_total=None), _gpu(mem_used=13 << 30),
    _gpu(util_pct=math.nan), _gpu(util_pct=101), _gpu(util_pct=-math.inf),
])
def test_validate_rejects_bad_samples_whole(bad):
    assert device_stats.validate(bad) is None


# --- hub: heartbeat, storage, throttle and ring ------------------------------------------------------------------


def ws(hub, agent):
    return hub.client.websocket_connect("/api/v1/ws", headers=agent.headers)


def beat(hub, agent, *frames):
    with ws(hub, agent) as conn:
        conn.receive_json()
        for frame in frames:
            conn.send_json({"type": "heartbeat", **frame})
            assert conn.receive_json()["type"] == "heartbeat_ack"


def enroll_on(hub, name, node_id):
    """An agent on an existing machine (same tailscale node, its own IP)."""
    from .conftest import AgentCreds

    ip = hub.new_ip()
    hub.whois.add(ip, node_id=node_id, name=f"{node_id}-host.example.ts.net")
    body = hub.client.post("/api/v1/enroll", json={"code": hub.code(), "name": name},
                           headers={"x-forwarded-for": ip}).json()
    agent = AgentCreds(id=body["agent"]["id"], name=name, token=body["token"], ip=ip)
    hub.set_status(agent, "active")
    return agent


def rows(hub):
    return [dict(r) for r in hub.conn().execute("SELECT node_id, sample, agent_id, updated_at FROM device_stats")]


def test_migration_26_adds_the_table_and_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    newest = db.MIGRATIONS
    assert len(newest) >= 26
    monkeypatch.setattr(db, "MIGRATIONS", newest[:25])
    conn = db.init_db(path)
    before = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    conn.close()
    monkeypatch.setattr(db, "MIGRATIONS", newest)
    conn = db.init_db(path)
    after = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
    assert {k: v for k, v in after.items() if k in before} == before  # nothing changed
    assert "device_stats" in set(after) - set(before)
    assert {r["name"] for r in conn.execute("PRAGMA table_info(device_stats)")} == {
        "node_id", "sample", "agent_id", "updated_at"}


def test_heartbeat_stores_one_row_per_device_with_the_device_from_the_caller(hub):
    a = hub.enroll("a")
    beat(hub, a, {"stats": {**GOOD, "node_id": "nsomeone-else"}})
    [row] = rows(hub)
    assert row["node_id"] == "na" and row["agent_id"] == a.id  # the caller's device, not the frame's
    assert json.loads(row["sample"]) == GOOD and ", " not in row["sample"]  # compact JSON
    assert device_stats.get(hub.conn(), "na") == {"ts": row["updated_at"], **GOOD}
    assert device_stats.get(hub.conn(), "nsomeone-else") is None
    assert device_stats.history("na") == [{"ts": row["updated_at"], **GOOD}]


def test_an_invalid_sample_is_dropped_and_the_rest_of_the_heartbeat_applies(hub):
    a = hub.enroll("a")
    beat(hub, a, {"checkouts": {"proj": "/w"}, "current_dir": "/w", "stats": _with(cpu_pct=math.nan)})
    assert rows(hub) == [] and device_stats.history("na") == []
    agent = hub.conn().execute("SELECT checkouts, last_heartbeat FROM agents WHERE id = ?", (a.id,)).fetchone()
    assert json.loads(agent["checkouts"]) == {"proj": "/w"} and agent["last_heartbeat"]


def test_an_old_runner_without_stats_means_no_data(hub):
    a = hub.enroll("a")
    beat(hub, a, {"checkouts": {}, "prompts": 1, "chat": 1})
    assert rows(hub) == []
    [d] = views.devices(hub.conn())
    assert d["stats"] is None and d["stats_stale"] is True


def test_runners_sharing_a_machine_make_one_write_per_interval(hub, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(device_stats, "_clock", lambda: now[0])
    agents = [enroll_on(hub, f"w{i}", "nbox") for i in range(7)]
    hub.conn().executescript(
        "CREATE TABLE stat_writes (n INTEGER);"
        "CREATE TRIGGER si AFTER INSERT ON device_stats BEGIN INSERT INTO stat_writes VALUES (1); END;"
        "CREATE TRIGGER su AFTER UPDATE ON device_stats BEGIN INSERT INTO stat_writes VALUES (1); END;")
    for agent in agents:
        beat(hub, agent, {"stats": GOOD})
    assert hub.conn().execute("SELECT COUNT(*) FROM stat_writes").fetchone()[0] == 1
    assert len(rows(hub)) == 1 and len(device_stats.history("nbox")) == 1
    now[0] += device_stats.MIN_INTERVAL_SECONDS
    later = _with(cpu_pct=90.0)
    for agent in agents:
        beat(hub, agent, {"stats": later})
    assert hub.conn().execute("SELECT COUNT(*) FROM stat_writes").fetchone()[0] == 2
    assert len(rows(hub)) == 1 and json.loads(rows(hub)[0]["sample"])["cpu_pct"] == 90.0
    assert [s["cpu_pct"] for s in device_stats.history("nbox")] == [37.5, 90.0]


def test_an_invalid_sample_does_not_use_up_the_interval(hub, monkeypatch):
    monkeypatch.setattr(device_stats, "_clock", lambda: 5.0)
    a, b = enroll_on(hub, "a", "nbox"), enroll_on(hub, "b", "nbox")
    beat(hub, a, {"stats": "garbage"})
    beat(hub, b, {"stats": GOOD})
    assert rows(hub)[0]["agent_id"] == b.id


def test_the_ring_is_bounded_per_device_and_in_devices(hub, monkeypatch):
    monkeypatch.setattr(device_stats, "MIN_INTERVAL_SECONDS", 0)
    conn = hub.conn()
    for i in range(device_stats.RING_SIZE + 15):
        device_stats.record(conn, "nbox", "ag1", _with(cpu_pct=float(i % 100)))
    ring = device_stats.history("nbox")
    assert len(ring) == device_stats.RING_SIZE and ring[-1]["cpu_pct"] == float(device_stats.RING_SIZE + 14)
    assert ring[0]["cpu_pct"] == 15.0 and [s["ts"] for s in ring] == sorted(s["ts"] for s in ring)
    assert conn.execute("SELECT COUNT(*) FROM device_stats").fetchone()[0] == 1
    monkeypatch.setattr(device_stats, "MAX_DEVICES", 3)
    for node in ("n1", "n2", "n3"):
        device_stats.record(conn, node, "ag1", GOOD)
    assert device_stats.history("nbox") == [] and device_stats.history("n3")  # the least recently reported went


def test_the_ring_starts_empty_after_a_restart_but_the_latest_sample_stays(hub):
    a = hub.enroll("a")
    beat(hub, a, {"stats": GOOD})
    device_stats.reset()  # what a hub restart does to memory
    assert device_stats.history("na") == []
    assert device_stats.get(hub.conn(), "na")["cpu_pct"] == 37.5
    assert device_stats.latest(hub.conn()).keys() == {"na"}


def test_is_stale():
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    fresh = {"ts": db.now_iso(now - timedelta(seconds=device_stats.STALE_SECONDS - 1))}
    old = {"ts": db.now_iso(now - timedelta(seconds=device_stats.STALE_SECONDS + 1))}
    assert not device_stats.is_stale(fresh, now) and device_stats.is_stale(old, now)
    assert device_stats.is_stale(None)


# --- hub: what the operator reads ------------------------------------------------------------------------------------


def test_views_devices_carry_the_latest_sample(hub):
    a = hub.enroll("a")
    hub.enroll("b")
    beat(hub, a, {"stats": GOOD})
    by_node = {d["node_id"]: d for d in views.devices(hub.conn())}
    assert by_node["na"]["stats"]["gpus"] == GOOD["gpus"] and by_node["na"]["stats_stale"] is False
    assert by_node["nb"]["stats"] is None and by_node["nb"]["stats_stale"] is True
    assert views.device(hub.conn(), "na")["stats"]["cpu_pct"] == 37.5


def test_operator_json_for_all_devices_and_one(hub):
    a = hub.enroll("a")
    hub.enroll("b")
    beat(hub, a, {"stats": GOOD})
    body = hub.client.get("/ui/api/devices/stats", headers=hub.op_headers()).json()
    assert body["stale_seconds"] == device_stats.STALE_SECONDS
    entries = {d["node_id"]: d for d in body["devices"]}
    assert entries.keys() == {"na", "nb"}
    na = entries["na"]
    assert set(na) == {"node_id", "name", "online", "stats", "stale", "history"}
    assert {k: v for k, v in na["stats"].items() if k != "ts"} == GOOD and na["stale"] is False
    assert na["history"] == [na["stats"]] and na["name"] == "a-host.example.ts.net"
    assert entries["nb"] == {"node_id": "nb", "name": "b-host.example.ts.net", "online": False, "stats": None,
                             "stale": True, "history": []}
    one = hub.client.get("/ui/api/devices/na/stats", headers=hub.op_headers()).json()
    assert one == na
    assert hub.client.get("/ui/api/devices/nnope/stats", headers=hub.op_headers()).status_code == 404


def test_agents_cannot_read_stats(hub):
    a = hub.enroll("a")
    beat(hub, a, {"stats": GOOD})
    for path in ("/ui/api/devices/stats", "/ui/api/devices/na/stats"):
        assert hub.client.get(path, headers=a.headers).status_code in (401, 403)
    assert hub.client.get("/api/v1/devices/stats", headers=a.headers).status_code == 404
