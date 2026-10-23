"""The Devices pages' usage meters (views.device_meters, _meters.html) and the device broadcast box (task #40)."""

import re
from types import SimpleNamespace

import pytest

from agenthub.hub import db, device_stats, views

from .conftest import OP_LOGIN
from .test_pages import get, place

GiB = 2**30
TS = "2026-10-23T16:30:00.000Z"


def sample(cpu=37.5, mem=(12 * GiB, 32 * GiB), gpus=(), ts=TS):
    return {"ts": ts, "cpu_pct": cpu, "mem_used": mem[0] if mem else None, "mem_total": mem[1] if mem else None,
            "gpus": [{"index": i, "name": n, "mem_used": u, "mem_total": t, "util_pct": p}
                     for i, (n, u, t, p) in enumerate(gpus)]}


# --- views.device_meters ---------------------------------------------------------------------

def test_no_sample_is_no_meters():
    assert views.device_meters(None) is None and views.device_meters({}) is None


def test_cpu_ram_and_one_vram_meter_per_gpu():
    u = views.device_meters(sample(gpus=[("RTX A", 2 * GiB, 8 * GiB, 12), ("RTX B", 7 * GiB, 8 * GiB, None)]))
    assert u["cpu"]["pct"] == 37.5 and u["cpu"]["value"] == "38%" and u["cpu"]["level"] == "ok"
    assert u["mem"]["pct"] == 37.5 and u["mem"]["detail"] == "12.0 / 32.0 GiB"
    assert [(g["gpu"], g["name"], g["value"], g["util"], g["level"]) for g in u["gpus"]] == [
        ("GPU 0", "RTX A", "25%", "12%", "ok"), ("GPU 1", "RTX B", "88%", None, "warn")]
    assert [(m["label"], m["value"], m["spark"]) for m in u["vram"]] == [("VRAM 0", "25%", None), ("VRAM 1", "88%", None)]
    assert u["ts"] == TS and u["stale"] is False


def test_levels_clamp_and_a_zero_reading_stays_a_meter():
    assert views.device_meters(sample(cpu=0))["cpu"]["pct"] == 0  # a 0 % reading is a meter, not "no data"
    assert views.device_meters(sample(cpu=74.9))["cpu"]["level"] == "ok"
    assert views.device_meters(sample(cpu=75))["cpu"]["level"] == "warn"
    assert views.device_meters(sample(cpu=90))["cpu"]["level"] == "high"
    assert views.device_meters(sample(cpu=140))["cpu"]["pct"] == 100
    assert views.device_meters(sample(mem=(300 * 2**20, 512 * 2**20)))["mem"]["detail"] == "300.0 / 512.0 MiB"


def test_missing_values_are_none_and_no_gpu_is_empty():
    u = views.device_meters(sample(cpu=None, mem=None))
    assert u["cpu"] is None and u["mem"] is None and u["gpus"] == [] and u["vram"] == []
    assert views.device_meters(sample(mem=(1, 0)))["mem"] is None  # a zero total can't make a percentage


def test_sparklines_follow_history_per_series_and_gpu():
    past = [sample(cpu=c, mem=(m * GiB, 32 * GiB), gpus=[("g", v * GiB, 8 * GiB, 1)]) for c, m, v in
            [(10, 8, 0), (50, 16, 4), (100, 32, 8)]]
    u = views.device_meters(past[-1], past)
    cpu = u["cpu"]["spark"]
    assert cpu["n"] == 3 and cpu["lo"] == "10%" and cpu["hi"] == "100%"
    ys = [float(p.split(",")[1]) for p in cpu["points"].split()]
    assert ys[0] > ys[1] > ys[2] == 1.0  # 100 % at the top of the 24-high box
    assert [float(p.split(",")[0]) for p in cpu["points"].split()] == [0, 60, 120]
    assert u["mem"]["spark"]["lo"] == "25%" and u["gpus"][0]["spark"]["hi"] == "100%"
    assert u["vram"][0]["label"] == "VRAM" and u["vram"][0]["spark"] is None  # one GPU; the card has no sparklines
    assert views.device_meters(past[0], past[:1])["cpu"]["spark"] is None  # one reading is no line


def test_many_gpus_share_one_card_row_coloured_by_the_fullest():
    u = views.device_meters(sample(gpus=[(f"g{i}", (7.5 if i == 3 else 1) * GiB, 8 * GiB, 0) for i in range(6)]))
    assert len(u["gpus"]) == 6 and len(u["vram"]) == 1
    row = u["vram"][0]
    assert row["label"] == "VRAM ×6" and row["value"] == "26%" and row["level"] == "high"  # GPU 3 is at 94 %
    assert row["detail"] == "12.5 / 48.0 GiB; fullest GPU 3 94%"


def test_stale_is_passed_through():
    assert views.device_meters(sample(), stale=True)["stale"] is True


# --- pages ---------------------------------------------------------------------------------

@pytest.fixture
def stats(monkeypatch):
    """Canned samples: ``stats.latest[node_id] = sample`` and ``stats.history[node_id] = [...]``."""
    s = SimpleNamespace(latest={}, history={}, stale=set())
    monkeypatch.setattr(device_stats, "latest", lambda conn: dict(s.latest))
    monkeypatch.setattr(device_stats, "history", lambda node_id: list(s.history.get(node_id, [])))
    monkeypatch.setattr(device_stats, "get", lambda conn, node_id: s.latest.get(node_id))
    monkeypatch.setattr(device_stats, "is_stale", lambda smp, now=None: smp is None or smp["ts"] in s.stale)
    return s


@pytest.fixture
def box(hub):
    a, b = hub.enroll("alpha"), hub.enroll("beta")
    place(hub, a, "n1", "box", True, TS)
    place(hub, b, "n1", "box", True, TS)
    return a, b


def meters(html):
    return re.findall(r'<div class="meter[^"]*" role="meter" aria-label="([^"]*)"[^>]*aria-valuenow="([^"]*)"', html)


def test_devices_list_has_compact_meters_from_the_latest_sample(hub, box, stats):
    stats.latest["n1"] = sample(cpu=0, gpus=[("RTX", 6 * GiB, 8 * GiB, 3)])
    stats.history["n1"] = [sample(cpu=5), sample(cpu=9)]
    html = get(hub, "/ui/devices").text
    assert meters(html) == [("box CPU", "0"), ("box RAM", "37.5"), ("box VRAM RTX", "75.0")]
    assert 'aria-valuetext="0%"' in html and 'aria-valuetext="75%, 6.0 / 8.0 GiB"' in html
    assert '<rect class="meter-fill" width="75.0" height="1"/>' in html  # drawn by the server: no JS, no inline style
    assert 'class="spark"' not in html and "style=" not in html
    assert '<div class="usage">' in html and "sampled" in html and "stale:" not in html


def test_device_without_data_or_gpu_says_so(hub, box, stats):
    html = get(hub, "/ui/devices").text
    assert "No usage data yet" in html and 'role="meter"' not in html
    stats.latest["n1"] = sample(cpu=None)
    html = get(hub, "/ui/devices").text
    assert meters(html) == [("box RAM", "37.5")] and ">no data<" in html and ">no GPU<" in html


def test_device_page_full_meters_one_per_gpu_with_sparklines_and_live_refresh(hub, box, stats):
    gpus = [("RTX 5070", 11 * GiB, 12 * GiB, 97), ("RTX 3060", 1 * GiB, 12 * GiB, None)]
    stats.latest["n1"] = sample(cpu=92.4, gpus=gpus)
    stats.history["n1"] = [sample(cpu=40, gpus=gpus), sample(cpu=92.4, gpus=gpus)]
    html = get(hub, "/ui/devices/n1").text
    assert meters(html) == [("CPU", "92.4"), ("RAM", "37.5"), ("VRAM, GPU 0 RTX 5070", "91.7"),
                            ("VRAM, GPU 1 RTX 3060", "8.3")]
    assert 'class="meter lvl-high"' in html and 'aria-valuetext="92%, high"' in html and ">high</span>" in html
    assert "GPU 0 · RTX 5070 · 97% busy" in html and "11.0 / 12.0 GiB" in html
    assert html.count('<svg class="spark"') == 4 and "last 2 readings: 40% to 92%" in html
    assert 'id="live" hx-get="/ui/devices/n1"' in html  # the live region polls the meters
    live = hub.client.get("/ui/devices/n1", headers={**hub.op_headers(), "hx-request": "true", "hx-target": "live"})
    assert live.text.lstrip().startswith('<section id="live"') and meters(live.text) == meters(html)
    assert "<form" not in live.text  # the broadcast box sits outside the polled region


def test_stale_or_offline_sample_is_dimmed_and_aged(hub, box, stats):
    old = "2026-10-23T15:00:00.000Z"
    stats.latest["n1"] = sample(ts=old)
    stats.stale.add(old)
    html = get(hub, "/ui/devices/n1").text
    assert 'class="usage full stale"' in html and "<strong>Stale:</strong> sampled" in html and "ago" in html
    stats.stale.clear()
    conn = hub.conn()
    with db.transaction(conn):
        conn.execute("UPDATE agents SET connected = 0")
    assert 'class="usage stale"' in get(hub, "/ui/devices").text  # offline: its last sample is stale too


def test_runner_text_is_escaped(hub, box, stats):
    stats.latest["n1"] = sample(gpus=[('<img src=x onerror=alert(1)>"', GiB, 2 * GiB, 1)])
    for page in ("/ui/devices/n1", "/ui/panel?item=device:n1"):
        html = get(hub, page).text
        assert "<img src=x" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_device_panel_shows_meters_and_points_to_the_broadcast_box(hub, box, stats):
    stats.latest["n1"] = sample()
    html = get(hub, "/ui/panel?item=device:n1").text
    assert meters(html) == [("CPU", "37.5"), ("RAM", "37.5")]
    assert 'href="/ui/devices/n1#broadcast-title"' in html


# --- the broadcast box ------------------------------------------------------------------------

@pytest.fixture
def sends(monkeypatch):
    """broadcasts.send stubbed (its own tests are hub-builder-2's): records calls, returns ``sends.result``."""
    from agenthub.hub import broadcasts, store

    calls = SimpleNamespace(calls=[], error=None, result=None)

    def send(conn, node_id, body, *, actor):
        calls.calls.append((node_id, body, actor))
        if calls.error:
            raise calls.error
        if not body.strip():
            raise store.Invalid("the message is empty")
        return calls.result

    monkeypatch.setattr(broadcasts, "send", send)
    calls.result = {"message_id": 7, "sent": 2, "woke": 1, "recipients": [
        {"agent_id": "a1", "name": "alpha", "delivered": True, "woke": True, "reason": None},
        {"agent_id": "b1", "name": "<b>beta</b>", "delivered": True, "woke": False, "reason": "paused"}]}
    return calls


def test_box_names_who_it_reaches_and_carries_csrf(hub, box, stats):
    html = get(hub, "/ui/devices/n1").text
    form = html.split('<section class="panel broadcast"')[1].split("</section>")[0]
    assert 'action="/ui/devices/n1/broadcast"' in form and 'hx-post="/ui/devices/n1/broadcast"' in form
    assert f'name="csrf" value="{hub.csrf()}"' in form and 'name="body"' in form
    assert "Goes to 2 agents (alpha, beta)" in form and "Send to all 2" in form


def test_box_without_active_agents_has_no_form(hub, stats):
    place(hub, hub.enroll("waiting", approve=False), "n2", "spare", False, TS)
    html = get(hub, "/ui/devices/n2").text
    assert "nobody to message" in html and "/broadcast" not in html


def test_htmx_send_shows_sent_and_woke(hub, box, stats, sends):
    r = hub.client.post("/ui/devices/n1/broadcast", data={"csrf": hub.csrf(), "body": "Pull main, please"},
                        headers={**hub.op_headers(), "hx-request": "true"})
    assert r.status_code == 200 and "Sent to 2 agents, woke 1." in r.text
    assert "alpha: woken" in r.text and "&lt;b&gt;beta&lt;/b&gt;: delivered (paused)" in r.text
    assert sends.calls == [("n1", "Pull main, please", f"human:{OP_LOGIN}")]


def test_plain_form_send_comes_back_to_the_page_with_counts(hub, box, stats, sends):
    r = hub.client.post("/ui/devices/n1/broadcast", data={"csrf": hub.csrf(), "body": "hi"},
                        headers=hub.op_headers(), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/devices/n1?sent=2&woke=1"
    html = get(hub, r.headers["location"]).text
    assert "Sent to 2 agents, woke 1." in html and 'id="live" hx-get="/ui/devices/n1"' in html


def test_send_needs_csrf(hub, box, stats, sends):
    r = hub.client.post("/ui/devices/n1/broadcast", data={"body": "hi"}, headers=hub.op_headers())
    assert r.status_code == 403 and sends.calls == []


def test_refusals_show_the_hubs_reason(hub, box, stats, sends):
    from agenthub.hub import store

    hx = {**hub.op_headers(), "hx-request": "true"}
    r = hub.client.post("/ui/devices/n1/broadcast", data={"csrf": hub.csrf(), "body": "  "}, headers=hx)
    assert r.status_code == 200 and 'role="alert">Not sent: the message is empty' in r.text
    sends.error = store.Conflict("no agents on this device")
    r = hub.client.post("/ui/devices/n1/broadcast", data={"csrf": hub.csrf(), "body": "x"}, headers=hx)
    assert "Not sent: no agents on this device" in r.text
    r = hub.client.post("/ui/devices/n1/broadcast", data={"csrf": hub.csrf(), "body": "x"}, headers=hub.op_headers())
    assert r.status_code == 409 and "no agents on this device" in r.text  # a plain form gets the error page


def test_real_samples_from_device_stats_draw_meters_and_sparklines(hub, box, monkeypatch):
    """No canned stats: two heartbeat samples through device_stats.record (#42) reach both pages."""
    monkeypatch.setattr(device_stats, "MIN_INTERVAL_SECONDS", 0)
    conn = hub.conn()
    for cpu in (20.0, 60.0):
        with db.transaction(conn):
            device_stats.record(conn, "n1", box[0].id, {"cpu_pct": cpu, "mem_used": 4 * GiB, "mem_total": 16 * GiB,
                                                        "gpus": []})
    html = get(hub, "/ui/devices/n1").text
    assert meters(html) == [("CPU", "60.0"), ("RAM", "25.0")] and ">no GPU<" in html
    assert html.count('<svg class="spark"') == 2 and "last 2 readings: 20% to 60%" in html
    assert 'class="usage full"' in html  # just sampled: not stale
    assert meters(get(hub, "/ui/devices").text) == [("box CPU", "60.0"), ("box RAM", "25.0")]


def test_real_broadcast_reaches_both_agents_and_is_listed_on_the_page(hub, box):
    """No stub: the box's form route through broadcasts.send (#41), then the page lists it with each delivery."""
    r = hub.client.post("/ui/devices/n1/broadcast", data={"csrf": hub.csrf(), "body": "Pull main <now>"},
                        headers={**hub.op_headers(), "hx-request": "true"})
    assert r.status_code == 200 and re.search(r"Sent to 2 agents, woke \d\.", r.text), r.text
    assert "alpha: " in r.text and "beta: " in r.text
    html = get(hub, "/ui/devices/n1").text
    log = html.split("<h2>Sent to this device</h2>")[1].split("</ul>")[0]
    assert "Pull main &lt;now&gt;" in log and "alpha: " in log and "beta: " in log
    assert '<section id="live"' in html.split("<h2>Sent to this device</h2>")[0]  # in the live region: it updates
    r = hub.client.post("/ui/devices/n1/broadcast", data={"csrf": hub.csrf(), "body": "   "},
                        headers={**hub.op_headers(), "hx-request": "true"})
    assert 'role="alert">Not sent: ' in r.text
    assert get(hub, "/ui/devices/n1").text.count('<p class="summary">') == 1  # the refused one isn't stored


def test_device_page_without_broadcasts_has_no_log(hub, box):
    assert "Sent to this device" not in get(hub, "/ui/devices/n1").text
