import pytest
from typer.testing import CliRunner

from agenthub.cli import app as cli_app
from agenthub.cli import uvicorn_options
from agenthub.config import HubConfig
from agenthub.hub.app import BindError, check_bind
from agenthub.hub.limits import AGENT_MIN_BODY_BYTES, OPERATOR_MAX_BODY_BYTES

TOO_BIG = AGENT_MIN_BODY_BYTES + 1024  # past an agent's limit (hub.toml's max_body_bytes is never below it)


def test_oversized_body_rejected_by_content_length(hub):
    a = hub.enroll("a")
    resp = hub.client.post("/api/v1/projects/proj/messages", content=b"x" * TOO_BIG,
                           headers={**a.headers, "content-type": "application/json"})
    assert resp.status_code == 413


def _chunks(total: int):
    for _ in range(total // 65536 + 1):
        yield b"x" * 65536


def test_oversized_chunked_body_rejected(hub):
    a = hub.enroll("a")
    resp = hub.client.post("/api/v1/chats/project/proj/messages", content=_chunks(TOO_BIG),
                           headers={**a.headers, "content-type": "application/json"})
    assert resp.status_code == 413


def test_oversized_enroll_rejected_before_auth(hub):
    resp = hub.client.post("/api/v1/enroll", content=b"x" * TOO_BIG)
    assert resp.status_code == 413


def test_an_old_hub_toml_limit_still_fits_an_agent_message(tmp_path):
    """hub.toml written by 0.3.8 says max_body_bytes = 65536; an agent's 64 KiB message still fits."""
    from .conftest import make_hub

    hub = make_hub(tmp_path, max_body_bytes=64 * 1024)
    a = hub.enroll("a")
    body = "\u00e9" * (64 * 1024)  # 64 Ki characters, 2 bytes each in UTF-8, more if JSON escapes them
    resp = hub.client.post("/api/v1/chats/project/proj/messages", json={"body": body}, headers=a.headers)
    assert resp.status_code == 201, resp.text


def test_the_operator_may_send_a_bigger_dashboard_request(hub):
    """Past an agent's limit, a /ui request passes only when it comes from the operator (checked before the body is
    read); the route then answers (here 403: no CSRF token), not the size limit."""
    big = b"note=" + b"x" * TOO_BIG
    form = {"content-type": "application/x-www-form-urlencoded"}
    assert hub.client.post("/ui/projects/proj/description", content=big,
                           headers={**hub.op_headers(), **form}).status_code == 403
    assert hub.client.post("/ui/projects/proj/description", content=_chunks(TOO_BIG),
                           headers={**hub.op_headers(), **form}).status_code == 403
    # not the operator: no identity, or an agent's token alongside the operator's identity
    a = hub.enroll("a")
    assert hub.client.post("/ui/projects/proj/description", content=big, headers=form).status_code == 413
    assert hub.client.post("/ui/projects/proj/description", content=_chunks(TOO_BIG),
                           headers=form).status_code == 413
    assert hub.client.post("/ui/projects/proj/description", content=big,
                           headers={**hub.op_headers(), **a.headers, **form}).status_code == 413
    # agent routes keep the agent limit, even from the operator's device
    assert hub.client.post("/api/v1/enroll", content=big, headers=hub.op_headers()).status_code == 413
    # and the operator's own ceiling holds
    huge = {"content-length": str(OPERATOR_MAX_BODY_BYTES + 1), **hub.op_headers(), **form}
    assert hub.client.post("/ui/projects/proj/description", content=b"", headers=huge).status_code == 413


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
