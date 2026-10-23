"""Agents' messages to each other: posts in the project chat and the inbox (channels until 0.3.7)."""

from .conftest import legacy_post

MSGS = "/api/v1/chats/project/proj/messages"
INBOX = "/api/v1/inbox"


def post(hub, agent, body, **kw):
    return hub.client.post(MSGS, json={"body": body, **kw}, headers=agent.headers)


def test_exchange_between_two_agents(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    assert post(hub, a, "hello b").status_code == 201
    inbox = hub.client.get(INBOX, headers=b.headers).json()
    assert [m["body"] for m in inbox["messages"]] == ["hello b"]
    assert inbox["messages"][0]["author_name"] == "a"
    # own messages never appear in own inbox
    assert hub.client.get(INBOX, headers=a.headers).json()["messages"] == []


def test_inbox_marks_read_unless_peek(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    post(hub, a, "one")
    assert len(hub.client.get(INBOX, params={"peek": True}, headers=b.headers).json()["messages"]) == 1
    assert len(hub.client.get(INBOX, headers=b.headers).json()["messages"]) == 1
    assert hub.client.get(INBOX, headers=b.headers).json()["messages"] == []
    post(hub, a, "two")
    assert [m["body"] for m in hub.client.get(INBOX, headers=b.headers).json()["messages"]] == ["two"]


def test_inbox_pagination(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    for i in range(5):
        post(hub, a, f"m{i}")
    first = hub.client.get(INBOX, params={"limit": 3}, headers=b.headers).json()
    assert [m["body"] for m in first["messages"]] == ["m0", "m1", "m2"] and first["more"]
    rest = hub.client.get(INBOX, params={"limit": 3}, headers=b.headers).json()
    assert [m["body"] for m in rest["messages"]] == ["m3", "m4"] and not rest["more"]


def test_addressed_messages(hub):
    a, b, c = hub.enroll("a"), hub.enroll("b"), hub.enroll("c")
    post(hub, a, "for b only", to="b")
    post(hub, a, "for everyone")
    b_msgs = hub.client.get(INBOX, headers=b.headers).json()["messages"]
    c_msgs = hub.client.get(INBOX, headers=c.headers).json()["messages"]
    assert [(m["body"], m["addressed_to_me"]) for m in b_msgs] == [("for b only", True), ("for everyone", False)]
    assert [m["body"] for m in c_msgs] == ["for everyone"]
    # addressed messages stay visible to every project member in the chat's history
    history = hub.client.get(MSGS, headers=c.headers).json()["messages"]
    assert [m["body"] for m in history] == ["for b only", "for everyone"]


def test_cannot_address_agent_outside_project(hub):
    a = hub.enroll("a")
    hub.enroll("outsider", projects=("other",))
    r = post(hub, a, "psst", to="outsider")  # not in this chat: posted, not delivered, declined in the chat
    assert r.status_code == 201 and r.json()["declined"][0]["reason"] == "not_in_project" and not r.json()["wakes"]
    assert post(hub, a, "psst", to="ghost").status_code == 404


def test_inbox_only_covers_allowed_projects(hub):
    a = hub.enroll("a", projects=("proj", "other"))
    b = hub.enroll("b", projects=("proj",))
    hub.client.post("/api/v1/chats/project/other/messages", json={"body": "secret"}, headers=a.headers)
    post(hub, a, "public")
    assert [m["body"] for m in hub.client.get(INBOX, headers=b.headers).json()["messages"]] == ["public"]


def test_channels_are_retired_but_their_unread_messages_still_arrive(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    assert hub.client.post("/api/v1/projects/proj/messages", json={"body": "x"}, headers=a.headers).status_code == 410
    assert hub.client.get("/api/v1/projects/proj/messages", headers=b.headers).status_code == 410
    legacy_post(hub, a, "left unread by 0.3.7", channel="design")
    got = hub.client.get(INBOX, headers=b.headers).json()["messages"]
    assert [(m["body"], m["channel"]) for m in got] == [("left unread by 0.3.7", "design")]
    assert hub.client.get(INBOX, headers=b.headers).json()["messages"] == []


def test_message_size_limit(hub):
    a = hub.enroll("a")
    assert post(hub, a, "x" * (64 * 1024 + 1)).status_code == 422  # an agent's message: at most 64 KiB
    assert post(hub, a, "").status_code == 422
