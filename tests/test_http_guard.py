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


# --- review round 1 (Errata) -------------------------------------------------


def test_rich_tracebacks_never_print_the_secret_or_token():
    import io as _io
    import logging

    from rich.console import Console
    from rich.logging import RichHandler

    token = "EAABsbCS1iHgBAKRICHSENTINEL0123456789"
    buffer = _io.StringIO()
    handler = RichHandler(console=Console(file=buffer, width=200), rich_tracebacks=True)
    log_redaction.install_on_handlers([handler])
    logger = logging.getLogger("ads46.rich")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        secret_in_local = SECRET  # noqa: F841  (rich tracebacks can show locals)
        raise RuntimeError(f"x-armavita-key: {SECRET} and Bearer {token}")
    except RuntimeError:
        logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    out = buffer.getvalue()
    assert "boom" in out
    assert SECRET not in out and token not in out


def test_rich_handlers_are_replaced_with_plain_ones():
    import logging

    from rich.logging import RichHandler

    logger = logging.getLogger("ads46.replace")
    logger.addHandler(RichHandler(rich_tracebacks=True))
    log_redaction.replace_rich_handlers(logger)
    assert logger.handlers and not any(isinstance(h, RichHandler) for h in logger.handlers)
    for h in list(logger.handlers):
        logger.removeHandler(h)


def test_a_registered_secret_is_redacted_whatever_surrounds_it():
    odd = "Zq9-" + "x" * 40 + "-odd"
    log_redaction.register_secret(odd)
    for text in (f"key=({odd})", f'"{odd}"', f"<{odd}>", f"secret was {odd}."):
        assert odd not in log_redaction.redact(text)


def test_production_runs_the_guard_with_the_secret(monkeypatch):
    """The app handed to uvicorn must be an HttpGuard that holds the configured secret."""
    from unittest.mock import patch

    import logging

    from armavita_meta_ads_mcp.core import media_helpers

    saved_tools = dict(mcp_server._tool_manager._tools)
    # _run_streamable_http rewires logging (stderr only, no RichHandler); put it back after.
    saved_handlers = {name: list(logging.getLogger(name).handlers) for name in ("", media_helpers.LOGGER_NAME)}
    mcp_server._session_manager = None
    try:
        with patch("uvicorn.run") as run:
            mcp_runtime._run_streamable_http("127.0.0.1", 8765, [], "/mcp", SECRET)
        app = run.call_args.args[0]
        assert isinstance(app, http_guard.HttpGuard)
        assert app.holds_secret(SECRET)
        assert not app.holds_secret(SECRET + "x")
    finally:
        http_mode.disable()
        mcp_server._session_manager = None
        mcp_server._tool_manager._tools.clear()
        mcp_server._tool_manager._tools.update(saved_tools)
        for name, handlers in saved_handlers.items():
            logging.getLogger(name).handlers[:] = handlers


@pytest.mark.asyncio
async def test_a_slow_body_gets_408():
    import asyncio

    async def slow():
        yield b"x"
        await asyncio.sleep(1)
        yield b"y"

    guard = http_guard.HttpGuard(_echo, SECRET, body_deadline=0.2)
    async with _client(guard) as client:
        response = await client.post("/mcp", content=slow(), headers={"x-armavita-key": SECRET})
    assert response.status_code == 408
    assert guard._buffered == 0


@pytest.mark.asyncio
async def test_bodies_held_at_once_are_capped_and_released():
    import asyncio

    release = asyncio.Event()

    async def holding_app(scope, receive, send):
        await receive()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guard = http_guard.HttpGuard(holding_app, SECRET, max_body_bytes=100, max_buffered_bytes=150)
    async with _client(guard) as client:
        first = asyncio.create_task(client.post("/mcp", content=b"x" * 100, headers={"x-armavita-key": SECRET}))
        await asyncio.sleep(0.05)
        second = await client.post("/mcp", content=b"x" * 100, headers={"x-armavita-key": SECRET})
        assert second.status_code == 503
        release.set()
        assert (await first).status_code == 200
    assert guard._buffered == 0
