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

    # --- chats -----------------------------------------------------------------

    @staticmethod
    def _chat_path(kind: str, ref: str) -> str:
        return f"/chats/{quote(kind, safe='')}/{quote(str(ref), safe='')}"

    async def chat_post(self, kind: str, ref: str, body: str, to: str | None = None) -> dict[str, Any]:
        """Write in a chat this agent is in (``agent``/``me``, ``task``/<id>, ``project``/<name>)."""
        return await self._request("POST", self._chat_path(kind, ref) + "/messages", json={"body": body, "to": to})

    async def chat_history(self, kind: str, ref: str, limit: int = 30, after: int = 0) -> dict[str, Any]:
        return await self._request("GET", self._chat_path(kind, ref) + "/messages",
                                   params={"limit": limit, "after": after})

    async def chat_stream(self, kind: str, ref: str, items: list[dict[str, Any]],
                          task_id: int | None = None) -> dict[str, Any]:
        """A session's live output: ``text``, ``tool`` and ``status`` items (see the chat contract)."""
        return await self._request("POST", self._chat_path(kind, ref) + "/stream",
                                   json={"items": items, "task_id": task_id})

    async def report_chat_run(self, kind: str, ref: str, *, outcome: str, seconds: float,
                              session_id: str | None = None, num_turns: int | None = None,
                              cost_usd: float | None = None, project: str | None = None,
                              task_id: int | None = None) -> dict[str, Any]:
        """One chat session this runner ran (turns, est. cost, duration), for the Usage page."""
        payload = {"outcome": outcome, "seconds": seconds, "session_id": session_id, "num_turns": num_turns,
                   "cost_usd": cost_usd, "project": project, "task_id": task_id}
        return await self._request("POST", self._chat_path(kind, ref) + "/runs", json=payload)

    async def pending_deliveries(self) -> list[dict[str, Any]]:
        """The operator's messages this agent's runner hasn't given to a session yet, oldest first."""
        return (await self._request("GET", "/chats/deliveries"))["deliveries"]

    async def claim_delivery(self, delivery_id: int, mode: str, note: str = "") -> dict[str, Any]:
        """Compare-and-set: HubError 409 when it was delivered already (skip it then)."""
        return await self._request("POST", f"/chats/deliveries/{delivery_id}", json={"mode": mode, "note": note})

    async def chat_session(self) -> str | None:
        return (await self._request("GET", "/chats/agent/me/session"))["session_id"]

    async def set_chat_session(self, session_id: str | None) -> dict[str, Any]:
        return await self._request("PUT", "/chats/agent/me/session", json={"session_id": session_id})

    async def take_task(self, request_id: int, title: str, body: str = "") -> dict[str, Any]:
        return await self._request("POST", f"/chats/requests/{request_id}/take", json={"title": title, "body": body})

    async def sit_out(self, request_id: int, reason: str) -> dict[str, Any]:
        return await self._request("POST", f"/chats/requests/{request_id}/sit-out", json={"reason": reason})

    # --- messages --------------------------------------------------------------

    async def inbox(
        self, project: str | None = None, limit: int = 50, peek: bool = False, addressed_only: bool = False
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "peek": peek, "addressed_only": addressed_only}
        if project:
            params["project"] = project
        return await self._request("GET", "/inbox", params=params)

    async def message_operator(self, body: str) -> dict[str, Any]:
        """Write to the operator in this agent's own chat."""
        return await self._request("POST", "/private", json={"body": body})

    async def post_device(self, body: str, to: str | None = None) -> dict[str, Any]:
        """Write on the board of this agent's own machine."""
        return await self._request("POST", "/device/messages", json={"body": body, "to": to})

    async def device_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """The newest notes on this agent's own machine's board (read-only, inbox cursor untouched)."""
        return (await self._request("GET", "/device/messages", params={"limit": limit}))["messages"]

    # --- questions to the operator -------------------------------------------

    async def ask_operator(self, project: str, question: str, task_id: int | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/projects/{quote(project)}/questions",
                                   json={"question": question, "task_id": task_id})

    async def get_question(self, question_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/questions/{question_id}")

    async def withdraw_question(self, question_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/questions/{question_id}/withdraw")

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

    # --- reports ---------------------------------------------------------------

    async def publish_report(
        self, project: str, title: str, *, summary: str = "", url: str | None = None, html: str | None = None,
        task_id: int | None = None, report_id: int | None = None,
    ) -> dict[str, Any]:
        payload = {"project": project, "title": title, "summary": summary, "url": url, "html": html,
                   "task_id": task_id, "report_id": report_id}
        return await self._request("POST", "/reports", json=payload)

    async def list_reports(self, project: str, task_id: int | None = None) -> list[dict[str, Any]]:
        params = {"task_id": task_id} if task_id is not None else {}
        return (await self._request("GET", f"/projects/{quote(project)}/reports", params=params))["reports"]

    async def get_report(self, report_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/reports/{report_id}")

    # --- tasks -----------------------------------------------------------------

    async def list_tasks(self, project: str, state: str | None = None, mine: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"mine": mine}
        if state:
            params["state"] = state
        return (await self._request("GET", f"/projects/{quote(project)}/tasks", params=params))["tasks"]

    async def create_task(
        self, project: str, title: str, body: str = "", assignee: str | None = None,
        budget: dict[str, int] | None = None, reason: str = "",
    ) -> dict[str, Any]:
        payload = {"title": title, "body": body, "assignee": assignee, "budget": budget, "reason": reason}
        return await self._request("POST", f"/projects/{quote(project)}/tasks", json=payload)

    async def get_task(self, task_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    async def claim_task(self, task_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/claim")

    async def update_task(self, task_id: int, state: str | None = None, note: str = "") -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/update", json={"state": state, "note": note})

    async def finish_task(self, task_id: int, note: str = "") -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/finish", json={"note": note})

    async def review_task(self, task_id: int, verdict: str, notes: str = "") -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/verdict", json={"verdict": verdict, "notes": notes})

    async def pause_task(self, task_id: int, until: str | None) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/pause", json={"until": until})

    async def task_runs(self, task_id: int, agent: str | None = None) -> dict[str, Any]:
        """All runs of the task; ``usage`` is ``agent``'s own when given, else the whole task's."""
        params = {"agent": agent} if agent else None
        return await self._request("GET", f"/tasks/{task_id}/runs", params=params)

    async def start_run(self, task_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/runs")

    async def set_run_session(self, task_id: int, run_id: int, session_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/runs/{run_id}/session", json={"session_id": session_id})

    async def end_run(
        self, task_id: int, run_id: int, *, outcome: str, session_id: str | None = None,
        num_turns: int | None = None, cost_usd: float | None = None,
    ) -> dict[str, Any]:
        payload = {"outcome": outcome, "session_id": session_id, "num_turns": num_turns, "cost_usd": cost_usd}
        return await self._request("POST", f"/tasks/{task_id}/runs/{run_id}/end", json=payload)

    async def progress(self, task_id: int, items: list[dict[str, str]], run_id: int | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/progress", json={"run_id": run_id, "items": items})

    # --- approvals -------------------------------------------------------------

    async def request_tool_permission(
        self, task_id: int, tool_name: str, tool_input: Any, reason: str = ""
    ) -> dict[str, Any]:
        payload = {"task_id": task_id, "tool_name": tool_name, "tool_input": tool_input, "reason": reason}
        return await self._request("POST", "/approvals/tool", json=payload)

    async def consume_tool_permission(self, task_id: int, tool_name: str, tool_input: Any) -> dict[str, Any]:
        payload = {"task_id": task_id, "tool_name": tool_name, "tool_input": tool_input}
        return await self._request("POST", "/approvals/tool/consume", json=payload)

    async def request_approval(
        self, kind: str, details: dict[str, Any], project: str | None = None, reason: str = ""
    ) -> dict[str, Any]:
        payload = {"kind": kind, "details": details, "project": project, "reason": reason}
        return await self._request("POST", "/approvals", json=payload)

    async def get_approval(self, approval_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/approvals/{approval_id}")

    async def my_approvals(self, awaiting_reply: bool = False) -> list[dict[str, Any]]:
        return (await self._request("GET", "/approvals", params={"awaiting_reply": awaiting_reply}))["approvals"]

    async def discuss_approval(self, approval_id: int, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/approvals/{approval_id}/discussion", json={"body": body})

    async def request_command(
        self, command: str, cwd: str, project: str | None = None, reason: str = ""
    ) -> dict[str, Any]:
        payload = {"command": command, "cwd": cwd, "project": project, "reason": reason}
        return await self._request("POST", "/approvals/command", json=payload)

    async def consume_command(self, command: str, cwd: str) -> dict[str, Any]:
        return await self._request("POST", "/approvals/command/consume", json={"command": command, "cwd": cwd})

    # --- intents ---------------------------------------------------------------

    async def announce_intent(
        self, project: str, patterns: list[str], description: str = "", ttl_minutes: int = 60
    ) -> dict[str, Any]:
        payload = {"patterns": patterns, "description": description, "ttl_minutes": ttl_minutes}
        return await self._request("POST", f"/projects/{quote(project)}/intents", json=payload)

    async def list_intents(self, project: str) -> list[dict[str, Any]]:
        return (await self._request("GET", f"/projects/{quote(project)}/intents"))["intents"]

    async def clear_intent(self, intent_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/intents/{intent_id}/clear")

    # --- asks ------------------------------------------------------------------

    async def ask(self, project: str, to: str, question: str, timeout_seconds: int) -> dict[str, Any]:
        payload = {"to": to, "question": question, "timeout_seconds": timeout_seconds}
        return await self._request("POST", f"/projects/{quote(project)}/asks", json=payload)

    async def get_ask(self, ask_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/asks/{ask_id}")

    async def answer_ask(self, ask_id: int, answer: str) -> dict[str, Any]:
        return await self._request("POST", f"/asks/{ask_id}/answer", json={"answer": answer})

    async def pending_asks(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/asks"))["asks"]

    # --- search ----------------------------------------------------------------

    async def search(self, q: str, project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": q, "limit": limit}
        if project:
            params["project"] = project
        return (await self._request("GET", "/search", params=params))["results"]
