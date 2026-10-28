# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-45: no Meta token reaches any log, from this package or from libraries."""

import logging
import logging.config
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import graph_client, http_mode, log_redaction, media_helpers, mcp_runtime
from armavita_meta_ads_mcp.core.mcp_runtime import mcp_server

TOKEN = "EAABsbCS1iHgBAKSENTINEL0123456789abcdefXYZ"


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {TOKEN}",
        f"GET https://graph.facebook.com/v25.0/me?fields=id&access_token={TOKEN}&limit=5",
        f"{{'access_token': '{TOKEN}', 'name': 'x'}}",
        f'{{"meta_access_token": "{TOKEN}"}}',
        f"token was {TOKEN} in a sentence",
    ],
)
def test_redact_removes_tokens_in_every_shape(text):
    cleaned = log_redaction.redact(text)
    assert TOKEN not in cleaned
    assert log_redaction.REDACTED in cleaned


def test_redact_leaves_ordinary_text_alone():
    text = "Graph request method=GET url=https://graph.facebook.com/v25.0/act_1/campaigns params={'fields': 'id'}"
    assert log_redaction.redact(text) == text


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))

    @property
    def text(self):
        return "\n".join(self.lines)


def test_filter_redacts_messages_args_and_tracebacks():
    logger = logging.getLogger("ads45.test")
    capture = _Capture()
    log_redaction.install_on_handlers([capture])
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.info("calling %s", f"https://graph.facebook.com/me?access_token={TOKEN}")
        try:
            raise RuntimeError(f"boom with Bearer {TOKEN}")
        except RuntimeError:
            logger.exception("failed")
    finally:
        logger.removeHandler(capture)

    assert TOKEN not in capture.text
    assert "Traceback" in capture.text and "RuntimeError" in capture.text  # still useful for debugging


def test_server_log_handler_has_the_filter():
    # Only the package's own server.log handler; pytest attaches its capture handlers here too.
    server_log = [h for h in media_helpers.logger.handlers if getattr(h, "baseFilename", "").endswith("server.log")]
    assert server_log
    for handler in server_log:
        assert any(isinstance(f, log_redaction.TokenRedactingFilter) for f in handler.filters)


def test_uvicorn_log_config_puts_the_filter_on_its_handlers():
    config = log_redaction.uvicorn_log_config()
    logging.config.dictConfig(config)
    for name in ("uvicorn", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            assert any(isinstance(f, log_redaction.TokenRedactingFilter) for f in handler.filters)


@pytest.fixture
def http_session_logs(monkeypatch):
    """HTTP mode on, every relevant logger at DEBUG, and a capture handler wherever records go."""
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)  # must never be used, or logged, in HTTP mode
    saved_tools = dict(mcp_server._tool_manager._tools)
    mcp_runtime.configure_http("127.0.0.1", 8765, [])
    mcp_server._session_manager = None
    app = mcp_server.streamable_http_app()

    capture = _Capture()
    names = ("", media_helpers.LOGGER_NAME, *mcp_runtime._LIBRARY_LOGGERS)
    saved_levels = {}
    for name in names:
        lg = logging.getLogger(name)
        saved_levels[name] = lg.level
        lg.setLevel(logging.DEBUG)
        lg.addHandler(capture)
    log_redaction.install_everywhere(mcp_runtime._LIBRARY_LOGGERS)
    log_redaction.install_on_handlers(media_helpers.logger.handlers)

    yield app, capture

    for name in names:
        lg = logging.getLogger(name)
        lg.removeHandler(capture)
        lg.setLevel(saved_levels[name])
    http_mode.disable()
    mcp_server._session_manager = None
    mcp_server._tool_manager._tools.clear()
    mcp_server._tool_manager._tools.update(saved_tools)


@pytest.mark.asyncio
async def test_http_session_never_logs_the_token(http_session_logs):
    app, capture = http_session_logs
    _RealAsyncClient = httpx.AsyncClient

    def graph(request: httpx.Request) -> httpx.Response:
        # A library that logs too much (here, httpx at DEBUG with the header). Without the
        # filter on the handlers this record would carry the token, so the test can fail.
        logging.getLogger("httpx").debug("HTTP Request: %s %s headers=%s", request.method, request.url, dict(request.headers))
        path = request.url.path
        if path.endswith("/111/adaccounts"):
            return httpx.Response(200, json={"data": [{"id": "act_1", "name": "ok"}]})
        if path.endswith("/222/adaccounts"):
            # Graph error that echoes the token back, as a hostile or odd upstream might.
            return httpx.Response(400, json={"error": {"code": 100, "message": f"bad token {TOKEN}"}})
        raise httpx.ConnectError(f"connection failed for Bearer {TOKEN}", request=request)

    transport = httpx.MockTransport(graph)

    def graph_client_factory(*args, **kwargs):
        return _RealAsyncClient(*args, transport=transport, **kwargs)

    calls = [
        ({"authorization": f"Bearer {TOKEN}"}, {"meta_user_id": "111"}),  # succeeds
        ({}, {"meta_user_id": "111"}),  # no header: refused in HTTP mode
        ({"authorization": f"Bearer {TOKEN}"}, {"meta_user_id": "222"}),  # Graph error
        ({"authorization": f"Bearer {TOKEN}"}, {"meta_user_id": "333"}),  # transport error
        ({"authorization": f"Bearer {TOKEN}"}, {"meta_user_id": "111", "meta_access_token": TOKEN}),  # refused arg
    ]
    with patch.object(graph_client.httpx, "AsyncClient", graph_client_factory):
        async with mcp_server.session_manager.run():
            asgi = httpx.ASGITransport(app=app)
            async with _RealAsyncClient(transport=asgi, base_url="http://127.0.0.1:8765") as client:
                for headers, arguments in calls:
                    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_ad_accounts", "arguments": arguments}}
                    response = await client.post(
                        "/mcp",
                        json=body,
                        headers={"accept": "application/json, text/event-stream", "content-type": "application/json", **headers},
                    )
                    assert response.status_code == 200

    assert capture.lines, "nothing was logged, so the test would prove nothing"
    assert TOKEN not in capture.text


def test_http_mode_logs_to_stderr_only(tmp_path):
    logger = logging.getLogger("ads45.stderr_only")
    file_handler = logging.FileHandler(tmp_path / "server.log")
    logger.addHandler(file_handler)
    log_redaction.log_to_stderr_only(logger)

    assert not any(isinstance(h, logging.FileHandler) for h in logger.handlers)
    streams = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
    assert streams and streams[0].stream is sys.stderr
    assert all(any(isinstance(f, log_redaction.TokenRedactingFilter) for f in h.filters) for h in logger.handlers)
    for h in list(logger.handlers):
        logger.removeHandler(h)


def test_server_log_is_private():
    import os
    import stat

    if os.name != "posix":
        pytest.skip("chmod semantics")
    log_file = media_helpers._resolve_log_file()
    assert stat.S_IMODE(os.stat(log_file).st_mode) == 0o600
