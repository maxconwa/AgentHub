from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from agenthub.config import HubConfig
from agenthub.hub import approvals, db, store
from agenthub.hub.app import HubState, create_app
from agenthub.hub.store import Device
from agenthub.hub.tailscale import IdentityError, WhoisResult

OPERATOR = "human:test-operator"
OP_LOGIN = "op@example.com"
OP_IP = "100.100.0.5"


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Never touch the real ~/.config or ~/.local/share."""
    monkeypatch.setenv("AGENTHUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTHUB_DATA_DIR", str(tmp_path / "data"))


class FakeWhois:
    def __init__(self) -> None:
        self.nodes: dict[str, WhoisResult] = {}
        self.calls: list[str] = []

    def add(self, ip: str, node_id: str, name: str, tags: tuple[str, ...] = ("tag:agent",)) -> None:
        self.nodes[ip] = WhoisResult(Device(node_id=node_id, name=name, ip=ip), tags, None)

    def add_user(self, ip: str, node_id: str, login: str) -> None:
        """A user-owned (untagged) device, like the operator's phone."""
        self.nodes[ip] = WhoisResult(Device(node_id=node_id, name=f"{node_id}.example.ts.net", ip=ip), (), login)

    async def lookup(self, ip: str) -> WhoisResult:
        self.calls.append(ip)
        if ip not in self.nodes:
            raise IdentityError("tailscale whois found no such peer")
        return self.nodes[ip]


@dataclass
class AgentCreds:
    id: str
    name: str
    token: str
    ip: str

    @property
    def headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}", "x-forwarded-for": self.ip}


@dataclass
class Hub:
    state: HubState
    whois: FakeWhois
    client: TestClient
    _next_ip: int = field(default=10)

    def conn(self):
        return db.connect(self.state.config.db_path)

    def code(self, projects=("proj",)) -> str:
        conn = self.conn()
        with db.transaction(conn):
            return store.create_enrollment(conn, created_by=OPERATOR, projects=list(projects))

    def set_status(self, agent: AgentCreds, status: str) -> None:
        conn = self.conn()
        with db.transaction(conn):
            approvals.set_agent_status(conn, agent.id, status, actor=OPERATOR)

    def op_headers(self, **extra: str) -> dict[str, str]:
        """What tailscale serve sends for the operator's phone."""
        return {"tailscale-user-login": OP_LOGIN, "x-forwarded-for": OP_IP, "sec-fetch-site": "same-origin", **extra}

    def csrf(self) -> str:
        from agenthub.hub.operator import csrf_token

        return csrf_token(self.state.csrf_secret, OP_LOGIN)

    def new_ip(self) -> str:
        self._next_ip += 1
        return f"100.64.0.{self._next_ip}"

    def enroll(self, name: str, projects=("proj",), approve: bool = True) -> AgentCreds:
        ip = self.new_ip()
        self.whois.add(ip, node_id=f"n{name}", name=f"{name}-host.example.ts.net")
        resp = self.client.post(
            "/api/v1/enroll", json={"code": self.code(projects), "name": name}, headers={"x-forwarded-for": ip}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        agent = AgentCreds(id=body["agent"]["id"], name=name, token=body["token"], ip=ip)
        if approve:
            self.set_status(agent, "active")
        return agent


def make_hub(tmp_path, *, insecure_dev: bool = False) -> Hub:
    cfg = HubConfig(db_path=str(tmp_path / "data" / "hub.sqlite3"), operators=[OP_LOGIN])
    db.init_db(cfg.db_path).close()
    whois = FakeWhois()
    whois.add_user(OP_IP, "nphone", OP_LOGIN)
    state = HubState(cfg, insecure_dev=insecure_dev, whois=whois)
    # The TCP peer is loopback, as it is behind tailscale serve.
    client = TestClient(create_app(state), client=("127.0.0.1", 40000))
    return Hub(state=state, whois=whois, client=client)


@pytest.fixture
def hub(tmp_path) -> Hub:
    return make_hub(tmp_path)


@pytest.fixture
def dev_hub(tmp_path) -> Hub:
    return make_hub(tmp_path, insecure_dev=True)
