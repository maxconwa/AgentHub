import pytest
from starlette.websockets import WebSocketDisconnect

from agenthub.hub.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_bucket_refills_over_time():
    clock = Clock()
    rl = RateLimiter(rate_per_second=2, burst=3, clock=clock)
    assert [rl.try_acquire("a") for _ in range(3)] == [0, 0, 0]
    assert rl.try_acquire("a") == pytest.approx(0.5)
    clock.t += 0.5
    assert rl.try_acquire("a") == 0
    assert rl.try_acquire("b") == 0  # keys are independent


def test_key_table_is_bounded():
    rl = RateLimiter(1, 1, max_keys=3)
    for k in "abcd":
        rl.try_acquire(k)
    assert len(rl._buckets) == 3 and "a" not in rl._buckets


def test_agent_requests_are_rate_limited(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    hub.state.agent_limiter = RateLimiter(rate_per_second=0.001, burst=5)
    codes = [hub.client.get("/api/v1/agents/me", headers=a.headers).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    r = hub.client.get("/api/v1/inbox", headers=a.headers)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert hub.client.get("/api/v1/agents/me", headers=b.headers).status_code == 200  # per agent


def test_unauthenticated_requests_do_not_drain_an_agents_bucket(hub):
    a = hub.enroll("a")
    hub.state.agent_limiter = RateLimiter(rate_per_second=0.001, burst=2)
    for _ in range(5):
        hub.client.get("/api/v1/agents/me", headers={"authorization": f"Bearer ahk_{a.id}_wrong",
                                                     "x-forwarded-for": a.ip})
    assert hub.client.get("/api/v1/agents/me", headers=a.headers).status_code == 200


def test_enrollment_guessing_is_throttled(hub):
    ip = hub.new_ip()
    hub.whois.add(ip, "nx", "x.example.ts.net")
    statuses = [hub.client.post("/api/v1/enroll", json={"code": f"AAAA-BBBB-{i:04d}", "name": "x"},
                                headers={"x-forwarded-for": ip}).status_code for i in range(7)]
    assert statuses[:5] == [403] * 5 and statuses[5:] == [429, 429]
    # even a valid code is refused while throttled
    r = hub.client.post("/api/v1/enroll", json={"code": hub.code(), "name": "x"}, headers={"x-forwarded-for": ip})
    assert r.status_code == 429


def test_websocket_frame_flood_closes_socket(hub):
    a = hub.enroll("a")
    with hub.client.websocket_connect("/api/v1/ws", headers=a.headers) as ws:
        ws.receive_json()
        for _ in range(100):
            ws.send_json({"type": "heartbeat"})
        # Unlimited, every heartbeat would be acked; limited, the socket closes first.
        with pytest.raises(WebSocketDisconnect):
            for _ in range(100):
                ws.receive_json()
