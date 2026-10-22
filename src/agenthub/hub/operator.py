"""Human operator identity for the dashboard and every operator action.

Identity comes from ``tailscale serve``: for requests from user-owned
(untagged) devices it sets ``Tailscale-User-Login`` after deleting any copy
the client sent (tailscale v1.102.2, ipn/ipnlocal/serve.go). Tagged devices
never get identity headers. We do not take the header on faith alone:

  1. ``--insecure-dev`` disables human identity entirely (no operator access).
  2. Any request carrying an Authorization header is refused, so an agent
     token can never reach an operator endpoint, even alongside a forged
     identity header.
  3. The caller's tailnet IP (XFF from serve, loopback peer only) is resolved
     with ``tailscale whois``; the node must be untagged and its login must
     equal the header.
  4. The login must be in the hub config's ``operators`` allowlist.
  5. The device must not be one that has an agent enrolled (pending or
     active), so a compromised agent machine owned by the operator still
     cannot approve its own requests.

The hub config's ``operator_devices`` (opt-in) replaces 3-5 for the nodes it
lists: a request from one of those nodes, tagged or not, with an agent enrolled
or not, is the operator login it maps to. Tagged nodes carry no identity
header, so the mapping is the identity, and every local process on such a
machine without an agent token, agents included, can act as the operator.
Step 2 and the CSRF and same-origin checks still apply.

State-changing requests also need a CSRF token (HMAC of the login with a
per-hub secret) and must not be cross-site (Sec-Fetch-Site / Origin), because
identity headers are ambient like cookies: any page the operator's browser
visits could otherwise post to the hub over the tailnet.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from agenthub import paths
from agenthub.config import write_private
from agenthub.hub.store import Device
from agenthub.hub.tailscale import IdentityError, tailnet_client_ip

LOGIN_HEADER = "tailscale-user-login"
CSRF_HEADER = "x-csrf-token"


@dataclass(frozen=True)
class Operator:
    login: str
    device: Device

    @property
    def actor(self) -> str:
        return f"human:{self.login}"


def load_csrf_secret(path: Path | None = None) -> bytes:
    path = path or paths.config_dir() / "csrf.key"
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    secret = secrets.token_bytes(32)
    write_private(path, secret.hex() + "\n")
    return secret


def csrf_token(secret: bytes, login: str) -> str:
    return hmac.new(secret, f"csrf:{login}".encode(), sha256).hexdigest()


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(403, detail)


async def authenticate_operator(conn: HTTPConnection, db: sqlite3.Connection) -> Operator:
    hub = conn.app.state.hub
    if hub.insecure_dev:
        raise _forbidden("operator access is disabled in --insecure-dev (human identity is not trusted)")
    if "authorization" in conn.headers:
        raise _forbidden("agent credentials are not accepted on operator endpoints")
    login = conn.headers.get(LOGIN_HEADER, "").strip()
    if not login and not hub.config.operator_devices:
        raise _forbidden("no Tailscale user identity: open the dashboard from a user-owned device over the tailnet")
    peer = conn.client.host if conn.client else None
    try:
        ip = tailnet_client_ip(peer, conn.headers)
        who = await hub.whois.lookup(ip)
    except IdentityError as exc:
        raise _forbidden(f"tailnet identity check failed: {exc}") from exc
    device_login = hub.config.operator_devices.get(who.device.node_id)
    if device_login is not None:
        if device_login not in hub.config.operators:
            raise _forbidden(f"{device_login} is not an operator of this hub")
        return Operator(login=device_login, device=who.device)
    if not login:
        raise _forbidden("no Tailscale user identity: open the dashboard from a user-owned device over the tailnet")
    if who.tags:
        raise _forbidden("tagged devices cannot act as an operator")
    if who.login != login:
        raise _forbidden("identity header does not match the device's Tailscale user")
    if login not in hub.config.operators:
        raise _forbidden(f"{login} is not an operator of this hub")
    if db.execute("SELECT 1 FROM agents WHERE device_node_id = ? AND status != 'revoked'",
                  (who.device.node_id,)).fetchone():
        raise _forbidden("this device runs an enrolled agent and cannot be used to operate the hub")
    return Operator(login=login, device=who.device)


def check_same_origin(conn: HTTPConnection) -> None:
    """Reject cross-site requests (CSRF / cross-site WebSocket hijacking)."""
    site = conn.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise _forbidden("cross-site request refused")
    origin = conn.headers.get("origin")
    if origin is not None:
        host = conn.headers.get("x-forwarded-host") or conn.headers.get("host")
        if urlsplit(origin).netloc != host:
            raise _forbidden("cross-origin request refused")


def check_csrf(conn: HTTPConnection, operator: Operator, submitted: str | None) -> None:
    check_same_origin(conn)
    expected = csrf_token(conn.app.state.hub.csrf_secret, operator.login)
    if not submitted or not hmac.compare_digest(submitted, expected):
        raise _forbidden("missing or invalid CSRF token; reload the page")
