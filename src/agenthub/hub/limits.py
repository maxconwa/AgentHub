"""Request size limits (pure ASGI middleware, so it runs before body parsing)."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimit:
    """Reject request bodies larger than ``max_bytes`` with 413.

    Checks Content-Length up front and also counts streamed chunks, so a
    chunked upload without Content-Length cannot bypass the limit. ``larger``
    gives a few exact paths a bigger limit of their own (a report's HTML page).
    """

    def __init__(self, app: ASGIApp, max_bytes: int, larger: dict[str, int] | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.larger = larger or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        max_bytes = self.larger.get(scope.get("path", ""), self.max_bytes)
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > max_bytes
                except ValueError:
                    too_big = True
                if too_big:
                    await _reject(send)
                    return

        received = 0
        response_started = False
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
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


async def _reject(send: Send) -> None:
    body = json.dumps({"detail": "request body too large"}).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
