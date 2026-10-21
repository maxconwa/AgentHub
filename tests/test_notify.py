import asyncio
import json

import httpx

from agenthub.config import HubConfig
from agenthub.hub.notify import Notifier, build_notification

CFG = HubConfig(public_url="https://hub.example.ts.net", ntfy_url="https://ntfy.example", ntfy_topic="ops-x1")


def ev(etype, actor="ag1", **payload):
    return {"id": 1, "ts": "", "type": etype, "project": "p", "actor": actor, "device": None, "payload": payload}


def test_approval_notification_is_minimal_by_default():
    e = ev("approval.requested", approval_id=7, kind="command",
           details={"command": "git push --force origin main  # token=SECRET123", "cwd": "/w"})
    msg = build_notification(e, {"ag1": "builder"}, CFG)
    assert msg["title"] == "Risky command · #7" and msg["click"] == "https://hub.example.ts.net/ui/approvals/7"
    assert "builder" in msg["message"]
    assert "SECRET123" not in json.dumps(msg) and "git push" not in json.dumps(msg)


def test_details_only_when_opted_in():
    cfg = HubConfig(**{**CFG.__dict__, "ntfy_include_details": True})
    e = ev("approval.requested", approval_id=7, kind="command", details={"command": "rm -rf build"})
    assert "rm -rf build" in build_notification(e, {}, cfg)["message"]


def test_which_events_notify():
    assert build_notification(ev("approval.discussed", approval_id=3, body="x"), {}, CFG)["title"].startswith("Reply")
    assert build_notification(ev("approval.discussed", actor="human:op", approval_id=3), {}, CFG) is None
    assert build_notification(ev("task.transitioned", task_id=4, to="review"), {}, CFG)["click"].endswith("/ui/tasks/4")
    assert build_notification(ev("task.transitioned", task_id=4, to="in_progress"), {}, CFG) is None
    assert build_notification(ev("message.posted", body="hi"), {}, CFG) is None


async def test_send_posts_json_with_token(tmp_path):
    token = tmp_path / "ntfy.token"
    token.write_text("tk_abc\n")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    cfg = HubConfig(**{**CFG.__dict__, "ntfy_token_file": str(token)})
    ok = await Notifier(cfg, transport=httpx.MockTransport(handler)).send({"title": "t", "message": "m", "click": None})
    assert ok
    (req,) = seen
    assert str(req.url) == "https://ntfy.example/" and req.headers["authorization"] == "Bearer tk_abc"
    assert json.loads(req.content) == {"topic": "ops-x1", "title": "t", "message": "m"}


async def test_send_failure_is_swallowed():
    n = Notifier(CFG, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert await n.send({"title": "t", "message": "m"}) is False


async def test_bus_submits_new_approvals(hub):
    sent = []

    class Recorder:
        def submit(self, event, names):
            sent.append((event["type"], names.get(event["actor"])))

    agent = hub.enroll("worker")
    bus = hub.state.bus
    bus.notifier = Recorder()
    bus._last_id = hub.conn().execute("SELECT MAX(id) FROM events").fetchone()[0]
    hub.client.post("/api/v1/approvals/command", json={"command": "rm -rf x", "cwd": "/"}, headers=agent.headers)
    await bus.pump()
    assert ("approval.requested", "worker") in sent


def test_disabled_without_config():
    assert Notifier(HubConfig()).enabled is False
