"""Request size limits (pure ASGI middleware, so it runs before body parsing)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# What the operator writes (messages, notes, task descriptions, answers, approval replies) has no length limit; what
# an agent writes keeps one. An agent's message (a chat post, a question, an ask or its answer, a note on the device
# board, a task description) is at most this many characters.
AGENT_TEXT_CHARS = 64 * 1024
# An agent's request body may be at least this big, whatever hub.toml's max_body_bytes says (its old default was
# 64 KiB): room for a message of AGENT_TEXT_CHARS characters however JSON escapes them.
AGENT_MIN_BODY_BYTES = 1024 * 1024
# The operator's dashboard requests (/ui, once the operator is authenticated) may be this big: a ceiling for the
# hub's memory, not a length limit anyone writes against (millions of characters, form-encoded).
OPERATOR_MAX_BODY_BYTES = 64 * 1024 * 1024
UI_PREFIX = "/ui/"


class BodySizeLimit:
    """Reject request bodies larger than ``max_bytes`` with 413.

    Checks Content-Length up front and also counts streamed chunks, so a
    chunked upload without Content-Length cannot bypass the limit. ``larger``
    gives a few exact paths a bigger limit of their own (a report's HTML page).
    A dashboard request (under /ui/) may grow to ``operator_bytes`` once
    ``is_operator`` says it comes from the operator; that check runs only for
    a body past ``max_bytes``, before any of the body is read.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, larger: dict[str, int] | None = None, *,
                 operator_bytes: int = 0, is_operator: Callable[[Scope], Awaitable[bool]] | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.larger = larger or {}
        self.operator_bytes = operator_bytes
        self.is_operator = is_operator

    async def _operator_limit(self, scope: Scope) -> int | None:
        """``operator_bytes`` for an authenticated operator's dashboard request, else None."""
        if self.is_operator is None or not scope.get("path", "").startswith(UI_PREFIX):
            return None
        return self.operator_bytes if await self.is_operator(scope) else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        max_bytes = self.larger.get(scope.get("path", ""), self.max_bytes)
        elevated = False
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    await _reject(send)
                    return
                if length > max_bytes:
                    raised = await self._operator_limit(scope)
                    if raised is None or length > raised:
                        await _reject(send)
                        return
                    max_bytes, elevated = raised, True

        received = 0
        response_started = False
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected, max_bytes, elevated
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes and not elevated:  # a chunked body without Content-Length
                    elevated = True
                    max_bytes = max(max_bytes, await self._operator_limit(scope) or 0)
                if received > max_bytes:
                    # Answer 413 ourselves and tell the app the client went
                    # away; raising here would be turned into a 400 by FastAPI.
                    rejected = True
                    if not response_started:
                        await _reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)


class LargeFormRequest(Request):
    """A request whose form fields may be as big as the whole body may be (Starlette's default caps each field at
    1 MB): the operator's text has no length limit, and BodySizeLimit has already bounded the body."""

    def form(self, *, max_files: int | float = 1000, max_fields: int | float = 1000,
             max_part_size: int = OPERATOR_MAX_BODY_BYTES) -> Any:
        return super().form(max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)


class LargeFormRoute(APIRoute):
    """The dashboard's routes: their forms are parsed as :class:`LargeFormRequest`."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Any]]:
        handler = super().get_route_handler()

        async def route(request: Request) -> Any:
            return await handler(LargeFormRequest(request.scope, request.receive))

        return route


async def _reject(send: Send) -> None:
    body = json.dumps({"detail": "request body too large"}).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
