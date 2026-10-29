# SPDX-License-Identifier: AGPL-3.0-only

"""The ASGI layer in front of the MCP app in HTTP mode (ADS-46).

Two checks, both made before the MCP SDK sees a request:

- **Shared secret.** Each request must carry `X-Armavita-Key` matching the
  server's secret (env ARMAVITA_MCP_SHARED_SECRET). Without it, anyone who
  learned the URL could use the server with their own Meta token. The
  comparison is constant-time over SHA-256 digests, so neither the secret's
  content nor its length leaks through timing.
- **Request-body cap.** The SDK reads the whole body into memory, so a body over
  MAX_BODY_BYTES is refused with 413: up front from Content-Length, or as soon
  as a streamed (chunked) body passes the cap. The cap leaves room for the
  largest image upload the tools accept (30 MB as base64) plus JSON.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .image_limits import MAX_IMAGE_BASE64_CHARS

SECRET_ENV = "ARMAVITA_MCP_SHARED_SECRET"
SECRET_HEADER = "x-armavita-key"
MIN_SECRET_LENGTH = 32
_SECRET_CHARS = re.compile(r"[A-Za-z0-9_-]+")
MAX_BODY_BYTES = MAX_IMAGE_BASE64_CHARS + 4 * 1024 * 1024
# Across all requests at once: bodies being read or held for the app. Two full-size uploads
# fit; past that, new requests get 503 until memory frees up (Errata, ADS-46 Low).
MAX_BUFFERED_BYTES = 2 * MAX_BODY_BYTES + 8 * 1024 * 1024
# A body must arrive within this many seconds of the request starting (408 otherwise).
BODY_DEADLINE_SECONDS = 120.0

Scope = Dict[str, Any]
Message = Dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def secret_from_env() -> Optional[str]:
    value = os.environ.get(SECRET_ENV, "").strip()
    return value or None


def secret_problem(secret: Optional[str]) -> Optional[str]:
    """Why `secret` can't be used, or None if it can."""
    if secret is None:
        return (
            f"HTTP mode needs a shared secret: set {SECRET_ENV} to a random value of at least "
            f"{MIN_SECRET_LENGTH} characters (for example: python -c \"import secrets; print(secrets.token_urlsafe(32))\"), "
            "and send it from the connector as the X-Armavita-Key header. "
            "For a local test with nothing exposed, pass --no-shared-secret."
        )
    if len(secret) < MIN_SECRET_LENGTH:
        return f"{SECRET_ENV} is shorter than {MIN_SECRET_LENGTH} characters. Use a longer random value."
    if not _SECRET_CHARS.fullmatch(secret):
        # Letters, digits, '-' and '_' only (what secrets.token_urlsafe makes). Quotes and
        # backslashes would be escaped in reprs, and the escaped form would slip past
        # literal redaction (Errata, ADS-46 round 2).
        return f"{SECRET_ENV} may contain only letters, digits, '-' and '_'."
    return None


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


async def _reply(send: Send, status: int, message: str, extra_headers: Optional[List] = None) -> None:
    body = json.dumps({"error": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class HttpGuard:
    """ASGI middleware: shared-secret check, then a buffered, capped request body."""

    def __init__(
        self,
        app: ASGIApp,
        shared_secret: Optional[str],
        max_body_bytes: int = MAX_BODY_BYTES,
        max_buffered_bytes: int = MAX_BUFFERED_BYTES,
        body_deadline: float = BODY_DEADLINE_SECONDS,
    ):
        self.app = app
        self._secret_digest = _digest(shared_secret.encode()) if shared_secret else None
        self.max_body_bytes = max_body_bytes
        self.max_buffered_bytes = max_buffered_bytes
        self.body_deadline = body_deadline
        self._buffered = 0  # bytes held across all in-flight requests (one event loop, no lock needed)

    @property
    def has_secret(self) -> bool:
        return self._secret_digest is not None

    def holds_secret(self, secret: str) -> bool:
        return self._secret_digest is not None and hmac.compare_digest(_digest(secret.encode()), self._secret_digest)

    def _authorized(self, scope: Scope) -> bool:
        if self._secret_digest is None:
            return True
        presented = b""
        for name, value in scope.get("headers") or []:
            if name.lower() == SECRET_HEADER.encode():
                presented = value
                break
        # Digest both sides so the comparison is constant-time and length-independent.
        return hmac.compare_digest(_digest(presented), self._secret_digest)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if not self._authorized(scope):
            await _reply(send, 401, "Missing or wrong X-Armavita-Key.")
            return

        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await _reply(send, 400, "Bad Content-Length.")
                    return
                if declared > self.max_body_bytes:
                    await _reply(send, 413, f"Request body over {self.max_body_bytes} bytes.", [(b"connection", b"close")])
                    return

        # Read the body here, counting as it arrives, then hand it to the app in one piece.
        # Every byte held counts against a cap shared by all requests, and the whole body has
        # one deadline, so key holders can't pile up memory or hold connections open.
        held = [0]  # bytes this request holds against the shared cap; always given back
        try:
            try:
                body = await asyncio.wait_for(self._read_body(receive, send, held), timeout=self.body_deadline)
            except asyncio.TimeoutError:
                await _reply(send, 408, "Request body took too long.", [(b"connection", b"close")])
                return
            if body is None:
                return  # already answered (413/503), or the client went away

            replayed = False

            async def replay() -> Message:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        finally:
            self._buffered -= held[0]

    async def _read_body(self, receive: Receive, send: Send, held: List[int]) -> Optional[bytes]:
        chunks: List[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_body_bytes:
                await _reply(send, 413, f"Request body over {self.max_body_bytes} bytes.", [(b"connection", b"close")])
                return None
            if self._buffered + len(chunk) > self.max_buffered_bytes:
                await _reply(send, 503, "Server busy; try again shortly.", [(b"retry-after", b"5")])
                return None
            self._buffered += len(chunk)
            held[0] += len(chunk)
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)
