"""Nothing is lost on restart: every feature writes to the one SQLite file, so a brand-new hub
on the same file shows the same pages and answers the same API reads."""

import json
import re

from fastapi.testclient import TestClient

from agenthub.hub import db, private, store, task_agents, tasks
from agenthub.hub.app import HubState, create_app

from .conftest import OPERATOR, Hub, make_hub


def restart(hub: Hub) -> Hub:
    """A new HubState and app over the same database file (same operator, same peers)."""
    state = HubState(hub.state.config, whois=hub.whois)
    client = TestClient(create_app(state), client=("127.0.0.1", 40000))
    return Hub(state=state, whois=hub.whois, client=client)


def form(hub, path, **data):
    r = hub.client.post(path, headers=hub.op_headers(), data={"csrf": hub.csrf(), **data}, follow_redirects=False)
    assert r.status_code in (200, 303), r.text
    return r


def page(hub, path):
    r = hub.client.get(path, headers=hub.op_headers())
    assert r.status_code == 200, (path, r.text)
    return r.text


def api(hub, agent, path, **params):
    r = hub.client.get(path, params=params, headers=agent.headers)
    assert r.status_code == 200, (path, r.text)
    return r.json()


def avatar_urls(html):
    return sorted(set(re.findall(r"/ui/avatars/[^\"' )]+", html)))


def test_restart_loses_nothing(tmp_path):
    hub = make_hub(tmp_path)
    alice, bob = hub.enroll("alice"), hub.enroll("bob")
    conn = hub.conn()
    with db.transaction(conn):
        t = tasks.create_task(conn, actor=OPERATOR, project="proj", title="Build the thing", body="TASK-BODY-77")
        task_agents.add_agent(conn, t["id"], "alice", role="Implementer", stage="works", actor=OPERATOR)
        task_agents.add_agent(conn, t["id"], "bob", role="Reviewer", stage="reviews", actor=OPERATOR)
        tid = t["id"]
        # checkouts, as a heartbeat stores them
        conn.execute("UPDATE agents SET checkouts = ?, current_dir = ? WHERE id = ?",
                     (json.dumps({"proj": "/src/proj-checkout"}), "/src/proj-checkout", alice.id))
    form(hub, "/ui/projects/proj/notes", body="PROJECT-NOTE-A")
    form(hub, f"/ui/tasks/{tid}/notes", body="TASK-NOTE-B")
    with db.transaction(conn):  # the UI only prompts now; the hub still writes the operator's private messages
        private.send(conn, alice.id, author=OPERATOR, body="OP-TO-ALICE-C")
    assert hub.client.post("/api/v1/private", json={"body": "ALICE-TO-OP-D"}, headers=alice.headers).status_code < 300
    assert hub.client.post("/api/v1/chats/project/proj/messages", json={"body": "PAIR-MSG-E", "to": "bob"},
                           headers=alice.headers).status_code == 201
    ask = hub.client.post("/api/v1/projects/proj/asks", json={"to": "bob", "question": "ASK-QUESTION-F",
                                                              "timeout_seconds": 3600}, headers=alice.headers)
    assert ask.status_code == 201
    ask_id = ask.json()["id"]
    assert hub.client.post("/api/v1/device/messages", json={"body": "DEVICE-NOTE-G"},
                           headers=alice.headers).status_code < 300
    ap = hub.client.post("/api/v1/approvals/command", json={"command": "git push --force origin feature", "cwd": "/w"},
                         headers=alice.headers).json()
    apid = ap["id"]
    hub.client.post(f"/api/v1/approvals/{apid}/discussion", json={"body": "AGENT-DISCUSS-H"}, headers=alice.headers)
    form(hub, f"/ui/approvals/{apid}/discuss", body="OP-DISCUSS-I")
    q = hub.client.post("/api/v1/projects/proj/questions", json={"question": "PENDING-QUESTION-J"},
                        headers=bob.headers)
    assert q.status_code < 300
    qid = q.json()["id"]
    before_avatar = avatar_urls(page(hub, f"/ui/agents/{alice.id}"))
    form(hub, f"/ui/agents/{alice.id}/avatar")
    avatar = avatar_urls(page(hub, f"/ui/agents/{alice.id}"))
    assert avatar != before_avatar

    pages = {
        "/ui/dashboard": [],
        f"/ui/tasks/{tid}": ["Build the thing", "TASK-BODY-77", "TASK-NOTE-B", "Implementer", "Reviewer"],
        "/ui/projects/proj": ["PROJECT-NOTE-A"],
        f"/ui/chats/{alice.id}": ["OP-TO-ALICE-C"],
        f"/ui/approvals/{apid}": ["git push --force origin feature", "AGENT-DISCUSS-H", "OP-DISCUSS-I"],
        f"/ui/questions/{qid}": ["PENDING-QUESTION-J"],
        f"/ui/devices/n{alice.name}": ["DEVICE-NOTE-G"],
        f"/ui/agents/{alice.id}": ["/src/proj-checkout"],
        "/ui/approvals": ["git push --force origin feature", "PENDING-QUESTION-J"],
    }
    for path, keys in pages.items():
        html = page(hub, path)
        for key in keys:
            assert key in html, (path, key)
    api_reads = {
        "messages": (bob, "/api/v1/chats/project/proj/messages", {}),
        "my chat": (alice, "/api/v1/chats/agent/me/messages", {}),
        "ask": (bob, f"/api/v1/asks/{ask_id}", {}),
        "asks": (bob, "/api/v1/asks", {}),
        "me": (alice, "/api/v1/agents/me", {}),
    }
    api_before = {k: api(hub, a, p, **kw) for k, (a, p, kw) in api_reads.items()}
    assert "PAIR-MSG-E" in json.dumps(api_before["messages"]) and "ALICE-TO-OP-D" in json.dumps(api_before["my chat"])

    new = restart(hub)
    assert new.state is not hub.state
    for path, keys in pages.items():
        html = page(new, path)
        for key in keys:
            assert key in html, ("after restart", path, key)
    assert avatar_urls(page(new, f"/ui/agents/{alice.id}")) == avatar
    for k, (a, p, kw) in api_reads.items():
        assert api(new, a, p, **kw) == api_before[k], k
    # bob never read his inbox: the unread messages survived, and reading moves a cursor that survives too
    inbox = api(new, bob, "/api/v1/inbox")["messages"]
    assert any("PAIR-MSG-E" in m["body"] for m in inbox) and any("ASK-QUESTION-F" in m["body"] for m in inbox)
    assert api(restart(new), bob, "/api/v1/inbox")["messages"] == []
    # the agents' tokens still work and the pending items are still pending and actionable
    assert api(new, bob, f"/api/v1/asks/{ask_id}")["status"] == "pending"
    row = new.conn().execute("SELECT checkouts, current_dir FROM agents WHERE id = ?", (alice.id,)).fetchone()
    assert json.loads(row["checkouts"]) == {"proj": "/src/proj-checkout"} and row["current_dir"] == "/src/proj-checkout"
    assert new.client.post(f"/api/v1/asks/{ask_id}/answer", json={"answer": "after restart"},
                           headers=bob.headers).json()["status"] == "answered"
