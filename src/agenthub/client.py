"""Async HTTP client for the hub's agent API (used by the MCP server and CLI)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from agenthub import __version__
from agenthub.config import AgentConfig


class HubError(Exception):
    def __init__(self, status: int, detail: Any, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"hub returned {status}: {detail}")
        self.status = status
        self.detail = detail
        self.body = body or {}


class HubClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30,
    ) -> None:
        headers = {"user-agent": f"agenthub/{__version__}"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1", headers=headers, transport=transport, timeout=timeout
        )

    @classmethod
    def from_config(cls, cfg: AgentConfig, **kwargs: Any) -> HubClient:
        return cls(cfg.hub_url, cfg.read_token(), **kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> HubClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._http.request(method, path, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:500]}
        if resp.status_code >= 400:
            raise HubError(resp.status_code, body.get("detail"), body)
        return body

    # --- identity --------------------------------------------------------------

    async def enroll(self, code: str, name: str) -> dict[str, Any]:
        return await self._request("POST", "/enroll", json={"code": code, "name": name})

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/agents/me")

    async def projects(self) -> list[str]:
        return (await self._request("GET", "/projects"))["projects"]

    # --- messages --------------------------------------------------------------

    async def post(self, project: str, body: str, channel: str = "general", to: str | None = None) -> dict[str, Any]:
        payload = {"channel": channel, "body": body, "to": to}
        return await self._request("POST", f"/projects/{quote(project)}/messages", json=payload)

    async def inbox(self, project: str | None = None, limit: int = 50, peek: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "peek": peek}
        if project:
            params["project"] = project
        return await self._request("GET", "/inbox", params=params)

    # --- board -----------------------------------------------------------------

    async def board_list(self, project: str, prefix: str = "") -> list[dict[str, Any]]:
        return (await self._request("GET", f"/projects/{quote(project)}/board", params={"prefix": prefix}))["keys"]

    async def board_get(self, project: str, key: str) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{quote(project)}/board/{quote(key)}")

    async def board_put(self, project: str, key: str, value: str, expected_version: int) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"/projects/{quote(project)}/board/{quote(key)}",
            json={"value": value, "expected_version": expected_version},
        )

    # --- search ----------------------------------------------------------------

    async def search(self, q: str, project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": q, "limit": limit}
        if project:
            params["project"] = project
        return (await self._request("GET", "/search", params=params))["results"]
