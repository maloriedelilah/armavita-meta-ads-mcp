# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-46: the shared-secret header and the request-body cap in front of the MCP app."""

import json
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import http_guard, http_mode, log_redaction, mcp_runtime
from armavita_meta_ads_mcp.core.mcp_runtime import mcp_server

SECRET = "k" * 20 + "SECRETSENTINEL-0123456789"


async def _echo(scope, receive, send):
    """A stand-in app that returns the body it received, so replay can be checked."""
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/octet-stream")]})
    await send({"type": "http.response.body", "body": body})


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765")


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"x-armavita-key": "wrong"}, {"x-armavita-key": SECRET[:-1]}, {"x-armavita-key": SECRET + "x"}])
async def test_requests_without_the_right_secret_are_refused(headers):
    async with _client(http_guard.HttpGuard(_echo, SECRET)) as client:
        response = await client.post("/mcp", content=b"{}", headers=headers)
    assert response.status_code == 401
    assert SECRET not in response.text


@pytest.mark.asyncio
async def test_the_right_secret_passes_and_the_body_is_replayed_intact():
    payload = b"x" * 70_000
    async with _client(http_guard.HttpGuard(_echo, SECRET)) as client:
        response = await client.post("/mcp", content=payload, headers={"X-Armavita-Key": SECRET})
    assert response.status_code == 200
    assert response.content == payload


@pytest.mark.asyncio
async def test_a_declared_body_over_the_cap_is_refused_before_reading():
    reads = []

    async def app(scope, receive, send):  # must never run
        reads.append(scope)

    guard = http_guard.HttpGuard(app, SECRET, max_body_bytes=100)
    async with _client(guard) as client:
        response = await client.post("/mcp", content=b"x" * 101, headers={"x-armavita-key": SECRET})
    assert response.status_code == 413
    assert reads == []


@pytest.mark.asyncio
async def test_a_streamed_body_over_the_cap_is_refused():
    async def chunks():
        for _ in range(50):
            yield b"x" * 10

    guard = http_guard.HttpGuard(_echo, SECRET, max_body_bytes=100)
    async with _client(guard) as client:  # a generator body is sent chunked, with no Content-Length
        response = await client.post("/mcp", content=chunks(), headers={"x-armavita-key": SECRET})
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_secret_is_checked_before_the_body_cap():
    guard = http_guard.HttpGuard(_echo, SECRET, max_body_bytes=10)
    async with _client(guard) as client:
        response = await client.post("/mcp", content=b"x" * 100)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_without_a_secret_the_cap_still_applies():
    guard = http_guard.HttpGuard(_echo, None, max_body_bytes=10)
    async with _client(guard) as client:
        assert (await client.post("/mcp", content=b"x" * 5)).status_code == 200
        assert (await client.post("/mcp", content=b"x" * 11)).status_code == 413


@pytest.mark.asyncio
async def test_lifespan_and_other_scopes_pass_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await http_guard.HttpGuard(app, SECRET)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_the_cap_fits_the_largest_image_upload():
    from armavita_meta_ads_mcp.core import image_limits

    assert http_guard.MAX_BODY_BYTES > image_limits.MAX_IMAGE_BASE64_CHARS
    assert http_guard.MAX_BODY_BYTES < 64 * 1024 * 1024


@pytest.mark.parametrize(
    "secret, ok",
    [(None, False), ("short", False), ("a" * 31, False), ("a" * 32, True), ("a" * 30 + " b", False), ("a" * 40 + "\n", False)],
)
def test_secret_problem(secret, ok):
    assert (http_guard.secret_problem(secret) is None) is ok


@pytest.mark.parametrize(
    "text",
    [
        f"{{'x-armavita-key': '{SECRET}'}}",
        f'{{"X-Armavita-Key": "{SECRET}"}}',
        f"[(b'x-armavita-key', b'{SECRET}')]",
        f"X-Armavita-Key: {SECRET}",
    ],
)
def test_the_secret_is_redacted_from_logs(text):
    assert SECRET not in log_redaction.redact(text)


def test_secret_redaction_is_linear():
    hostile = "x-armavita-key" + " " * 200_000 + "!"
    start = time.perf_counter()
    log_redaction.redact(hostile * 5)
    assert time.perf_counter() - start < 0.5


# --- the real MCP app behind the guard ----------------------------------------


@pytest.fixture
def guarded_app():
    saved_tools = dict(mcp_server._tool_manager._tools)
    mcp_runtime.configure_http("127.0.0.1", 8765, [])
    mcp_server._session_manager = None
    yield mcp_runtime.build_http_app(SECRET)
    http_mode.disable()
    mcp_server._session_manager = None
    mcp_server._tool_manager._tools.clear()
    mcp_server._tool_manager._tools.update(saved_tools)


@pytest.mark.asyncio
async def test_real_app_needs_the_secret(guarded_app):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    async with mcp_server.session_manager.run():
        async with _client(guarded_app) as client:
            refused = await client.post("/mcp", json=body, headers=headers)
            allowed = await client.post("/mcp", json=body, headers={**headers, "x-armavita-key": SECRET})
    assert refused.status_code == 401
    assert allowed.status_code == 200
    assert any(tool["name"] == "list_ad_accounts" for tool in json.loads(allowed.text)["result"]["tools"])
