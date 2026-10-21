"""Caller identification behind ``tailscale serve``.

Verified against tailscale v1.102.2 source (ipn/ipnlocal/serve.go) and the
serve KB article (tailscale.com/kb/1312/serve, "Last validated Jan 20, 2026"):

* serve proxies with Go's ``httputil.ReverseProxy`` in Rewrite mode, which
  drops all incoming ``X-Forwarded-*`` headers, then *sets* (not appends)
  ``X-Forwarded-For`` to the tailnet source address. So a request that came
  through serve has exactly one XFF value: the caller's tailnet IP.
* serve deletes any client-supplied ``Tailscale-User-*`` headers and re-adds
  them only for user-owned (untagged) nodes; never for tagged nodes or Funnel.

These headers are only meaningful because the hub listens on loopback and
serve is the only thing in front of it. We therefore trust XFF only when the
TCP peer itself is loopback.

``tailscale whois --json <ip>`` output (tailscale v1.102.2,
``apitype.WhoIsResponse``): ``Node.StableID``, ``Node.Name`` (FQDN with a
trailing dot), ``Node.Tags`` (omitted when untagged), ``Node.Addresses``,
``UserProfile.LoginName``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from agenthub.hub.store import Device

# Tailscale assigns node addresses from these ranges.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class IdentityError(Exception):
    """The caller's tailnet identity could not be established."""


def is_tailnet_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr in TAILNET_V4 or addr in TAILNET_V6


def is_loopback(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def tailnet_client_ip(peer_ip: str | None, headers: Mapping[str, str]) -> str:
    """Return the caller's tailnet IP as reported by ``tailscale serve``.

    Raises IdentityError unless the request came from loopback carrying a
    single X-Forwarded-For entry that is a tailnet address.
    """
    if not is_loopback(peer_ip):
        raise IdentityError("request did not arrive via tailscale serve")
    xff = headers.get("x-forwarded-for")
    if not xff:
        raise IdentityError("missing X-Forwarded-For from tailscale serve")
    parts = [p.strip() for p in xff.split(",")]
    if len(parts) != 1:
        raise IdentityError("unexpected multi-hop X-Forwarded-For")
    if not is_tailnet_ip(parts[0]):
        raise IdentityError("X-Forwarded-For is not a tailnet address")
    return parts[0]


@dataclass(frozen=True)
class WhoisResult:
    device: Device
    tags: tuple[str, ...]
    login: str | None


def parse_whois(ip: str, data: dict) -> WhoisResult:
    node = data.get("Node") or {}
    node_id = node.get("StableID")
    name = (node.get("Name") or "").rstrip(".")
    if not node_id or not name:
        raise IdentityError("whois response lacks node identity")
    tags = tuple(node.get("Tags") or ())
    login = None if tags else (data.get("UserProfile") or {}).get("LoginName")
    return WhoisResult(device=Device(node_id=node_id, name=name, ip=ip), tags=tags, login=login)


class Whois(Protocol):
    async def lookup(self, ip: str) -> WhoisResult: ...


class TailscaleWhois:
    """Runs ``tailscale whois --json`` with a short positive-result cache."""

    def __init__(self, tailscale_bin: str = "tailscale", cache_seconds: float = 30, timeout: float = 5) -> None:
        self.tailscale_bin = tailscale_bin
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._cache: dict[str, tuple[float, WhoisResult]] = {}

    async def lookup(self, ip: str) -> WhoisResult:
        if not is_tailnet_ip(ip):
            raise IdentityError("not a tailnet address")
        cached = self._cache.get(ip)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        try:
            proc = await asyncio.create_subprocess_exec(
                self.tailscale_bin, "whois", "--json", ip,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        except (OSError, TimeoutError) as exc:
            raise IdentityError(f"tailscale whois failed: {exc.__class__.__name__}") from exc
        if proc.returncode != 0:
            raise IdentityError("tailscale whois found no such peer")
        try:
            result = parse_whois(ip, json.loads(stdout))
        except (ValueError, AttributeError) as exc:
            raise IdentityError("unparseable whois output") from exc
        self._cache[ip] = (time.monotonic(), result)
        return result


DEV_DEVICE = Device(node_id="insecure-dev", name="insecure-dev", ip="127.0.0.1")
