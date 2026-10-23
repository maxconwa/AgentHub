import pytest
from typer.testing import CliRunner

from agenthub.cli import app as cli_app
from agenthub.cli import uvicorn_options
from agenthub.config import HubConfig
from agenthub.hub.app import BindError, check_bind


def test_oversized_body_rejected_by_content_length(hub):
    a = hub.enroll("a")
    resp = hub.client.post("/api/v1/projects/proj/messages", content=b"x" * (65 * 1024),
                           headers={**a.headers, "content-type": "application/json"})
    assert resp.status_code == 413


def test_oversized_chunked_body_rejected(hub):
    a = hub.enroll("a")

    def chunks():
        for _ in range(100):
            yield b"x" * 1024

    resp = hub.client.post("/api/v1/chats/project/proj/messages", content=chunks(),
                           headers={**a.headers, "content-type": "application/json"})
    assert resp.status_code == 413


def test_oversized_enroll_rejected_before_auth(hub):
    resp = hub.client.post("/api/v1/enroll", content=b"x" * (65 * 1024))
    assert resp.status_code == 413


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.0.0.2"])
def test_loopback_bind_allowed(host):
    check_bind(host, insecure_dev=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "100.64.0.1", "192.168.1.5", "localhost", "example.com"])
def test_non_loopback_bind_refused(host):
    with pytest.raises(BindError):
        check_bind(host, insecure_dev=False)


def test_insecure_dev_allows_any_bind():
    check_bind("0.0.0.0", insecure_dev=True)


def test_uvicorn_does_not_rewrite_peer_from_proxy_headers():
    assert uvicorn_options("127.0.0.1", 8700)["proxy_headers"] is False


def test_serve_cli_refuses_non_loopback_config(monkeypatch):
    HubConfig(bind_host="0.0.0.0").save()
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: ran.append(k))
    result = CliRunner().invoke(cli_app, ["hub", "serve"])
    assert result.exit_code == 1 and "refusing to bind" in result.output
    assert ran == []


def test_serve_cli_insecure_dev_warns(monkeypatch, tmp_path):
    HubConfig(bind_host="0.0.0.0", db_path=str(tmp_path / "h.db")).save()
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda app, **k: ran.append((app, k)))
    result = CliRunner().invoke(cli_app, ["hub", "serve", "--insecure-dev"])
    assert result.exit_code == 0, result.output
    assert "--insecure-dev" in result.output and "DISABLED" in result.output
    app, kwargs = ran[0]
    assert app.state.hub.insecure_dev is True and kwargs["proxy_headers"] is False
