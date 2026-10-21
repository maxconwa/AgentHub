MSGS = "/api/v1/projects/proj/messages"
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
    # addressed messages stay visible to every project member in channel history
    history = hub.client.get(MSGS, headers=c.headers).json()["messages"]
    assert [m["body"] for m in history] == ["for b only", "for everyone"]


def test_cannot_address_agent_outside_project(hub):
    a = hub.enroll("a")
    hub.enroll("outsider", projects=("other",))
    assert post(hub, a, "psst", to="outsider").status_code == 422
    assert post(hub, a, "psst", to="ghost").status_code == 404


def test_inbox_only_covers_allowed_projects(hub):
    a = hub.enroll("a", projects=("proj", "other"))
    b = hub.enroll("b", projects=("proj",))
    hub.client.post("/api/v1/projects/other/messages", json={"body": "secret"}, headers=a.headers)
    post(hub, a, "public")
    assert [m["body"] for m in hub.client.get(INBOX, headers=b.headers).json()["messages"]] == ["public"]


def test_channels_are_created_and_named(hub):
    a, b = hub.enroll("a"), hub.enroll("b")
    post(hub, a, "x", channel="design")
    assert post(hub, a, "x", channel="Bad Name!").status_code == 422
    msgs = hub.client.get(MSGS, params={"channel": "design"}, headers=b.headers).json()["messages"]
    assert [m["channel"] for m in msgs] == ["design"]


def test_message_size_limit(hub):
    a = hub.enroll("a")
    assert post(hub, a, "x" * 16_001).status_code == 422
    assert post(hub, a, "").status_code == 422
