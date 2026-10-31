# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-55: --login never writes secrets to the log; only a dead cached token logs the user out."""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import auth_state, graph_client, media_helpers, oauth_callback_server

SECRETS = ("APPSECRET_SENTINEL", "AUTHCODE_SENTINEL", "SHORTTOKEN_SENTINEL")


@pytest.fixture
def app_credentials(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "APPSECRET_SENTINEL")
    monkeypatch.setattr(auth_state.meta_config, "get_app_id", lambda: "123")


def _raise_with_url(*args, **kwargs):
    # requests puts the full URL, query string included, into ConnectionError messages.
    raise requests.ConnectionError(
        "HTTPSConnectionPool: Max retries exceeded with url: /oauth/access_token?"
        "client_secret=APPSECRET_SENTINEL&code=AUTHCODE_SENTINEL&fb_exchange_token=SHORTTOKEN_SENTINEL"
    )


class _Captured(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def server_log():
    """Capture what the project's logger (server.log; propagate=False) receives, at DEBUG."""
    logger = media_helpers.logger
    handler = _Captured()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def _assert_no_secrets(log):
    assert log.lines, "nothing was logged: the test would prove nothing"
    assert all(secret not in log.text for secret in SECRETS), log.text


def test_code_exchange_posts_secrets_in_the_body(app_credentials, monkeypatch):
    calls = {}

    def fake_post(url, data=None, timeout=None, **kwargs):
        calls.update(url=url, data=data)
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "EAAOK0123456789"})

    monkeypatch.setattr(auth_state.requests, "post", fake_post)
    monkeypatch.setattr(auth_state.requests, "get", MagicMock(side_effect=AssertionError("GET must not be used")))

    info = auth_state.exchange_code_for_short_lived("AUTHCODE_SENTINEL", redirect_uri="http://localhost:8080/callback")

    assert info.meta_access_token == "EAAOK0123456789"
    assert calls["data"]["client_secret"] == "APPSECRET_SENTINEL" and calls["data"]["code"] == "AUTHCODE_SENTINEL"
    assert urlparse(calls["url"]).query == ""


def test_exchange_network_errors_never_log_secrets(app_credentials, monkeypatch, server_log):
    monkeypatch.setattr(auth_state.requests, "post", _raise_with_url)

    assert auth_state.exchange_code_for_short_lived("AUTHCODE_SENTINEL") is None
    assert auth_state.exchange_token_for_long_lived("SHORTTOKEN_SENTINEL") is None

    _assert_no_secrets(server_log)
    assert "ConnectionError" in server_log.text


def test_exchange_error_responses_never_log_the_body(app_credentials, monkeypatch, server_log):
    body = {"error": {"code": 100, "message": "bad code AUTHCODE_SENTINEL for secret APPSECRET_SENTINEL"}}
    monkeypatch.setattr(
        auth_state.requests,
        "post",
        lambda *a, **k: SimpleNamespace(status_code=400, text=str(body), json=lambda: body),
    )

    assert auth_state.exchange_code_for_short_lived("AUTHCODE_SENTINEL") is None
    assert auth_state.exchange_token_for_long_lived("SHORTTOKEN_SENTINEL") is None

    _assert_no_secrets(server_log)
    assert "HTTP 400" in server_log.text and "Graph error code 100" in server_log.text


def _graph_error_client(code):
    request = httpx.Request("GET", "https://graph.facebook.com/v25.0/act_1/campaigns")
    response = httpx.Response(400, request=request, json={"error": {"code": code, "message": "x"}})

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            response.raise_for_status()

    return _Client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code, token, cleared",
    [
        (10, "CACHED_TOKEN_0123456789", False),  # an ordinary permission error
        (102, "CACHED_TOKEN_0123456789", False),
        (190, "SOME_OTHER_TOKEN_0123456789", False),  # not the cached login
        (190, "CACHED_TOKEN_0123456789", True),  # the cached login token is dead
    ],
)
async def test_only_a_dead_cached_token_logs_the_user_out(monkeypatch, code, token, cleared):
    monkeypatch.setattr(auth_state.auth_manager, "get_access_token", lambda: "CACHED_TOKEN_0123456789")
    invalidate = MagicMock()
    monkeypatch.setattr(auth_state.auth_manager, "invalidate_token", invalidate)

    with patch.object(graph_client.httpx, "AsyncClient", _graph_error_client(code)), patch.object(
        graph_client.asyncio, "sleep", new_callable=AsyncMock
    ):
        await graph_client.make_api_request("act_1/campaigns", token, {}, method="GET")

    assert invalidate.called is cleared


def _callback(query, monkeypatch):
    responses = []
    handler = oauth_callback_server.CallbackHandler.__new__(oauth_callback_server.CallbackHandler)
    monkeypatch.setattr(handler, "_html_response", lambda markup: responses.append(markup), raising=False)
    shutdown = MagicMock()
    monkeypatch.setattr(oauth_callback_server, "_shutdown_async", shutdown)
    handler._handle_callback(urlparse(f"http://localhost:8080/callback?{query}"))
    return responses, shutdown


def test_error_callback_without_this_logins_state_is_ignored(monkeypatch):
    monkeypatch.setitem(oauth_callback_server.token_container, "expected_state", "REAL_STATE_0123456789")
    monkeypatch.setitem(oauth_callback_server.token_container, "status", "waiting")
    monkeypatch.setitem(oauth_callback_server.token_container, "error", None)

    responses, shutdown = _callback("error=access_denied&state=WRONG", monkeypatch)

    shutdown.assert_not_called()
    assert oauth_callback_server.token_container.get("error") is None
    assert "Ignored" in responses[0]


def test_error_callback_with_this_logins_state_ends_the_login(monkeypatch):
    monkeypatch.setitem(oauth_callback_server.token_container, "expected_state", "REAL_STATE_0123456789")
    monkeypatch.setitem(oauth_callback_server.token_container, "error", None)

    _, shutdown = _callback("error=access_denied&state=REAL_STATE_0123456789", monkeypatch)

    shutdown.assert_called_once()
    assert oauth_callback_server.token_container.get("error") == "access_denied"


def test_printed_callback_text_has_no_control_characters():
    assert auth_state._printable("bad\x1b[31mred\x07\nline") == "bad[31mredline"
    assert len(auth_state._printable("x" * 1000)) == 300


def test_unused_authenticate_is_gone():
    assert not hasattr(auth_state.AuthManager, "authenticate")


def test_callback_failures_log_only_the_exception_type():
    # ADS-66: no logger.exception (its traceback carries the message) and no exception text.
    source = Path(oauth_callback_server.__file__).read_text()
    assert "logger.exception(" not in source
    assert ", exc)" not in source


def test_token_cache_folder_is_made_private_only_when_a_token_is_saved(tmp_path, monkeypatch):
    """No folder at import or on read; on save, the folder is 0700 and the file 0600."""
    import os
    import stat

    from armavita_meta_ads_mcp.core import auth_state

    if os.name != "posix":
        pytest.skip("chmod semantics")
    cache = tmp_path / "cfg" / "armavita-meta-ads-mcp" / "token_cache.json"
    manager = auth_state.auth_manager
    monkeypatch.setattr(manager, "_cache_path", lambda: cache)
    assert manager._load_cached_token() is False
    assert not cache.parent.exists()

    monkeypatch.setattr(manager, "token_info", auth_state.TokenInfo(meta_access_token="EAA" + "x" * 40))
    manager._persist_token()
    assert stat.S_IMODE(os.stat(cache.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600


def test_token_save_never_writes_through_a_symlink(tmp_path, monkeypatch):
    """A symlink at the cache path is replaced, not followed (Errata, #18 Low)."""
    import os

    from armavita_meta_ads_mcp.core import auth_state

    if os.name != "posix":
        pytest.skip("symlink semantics")
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    folder = tmp_path / "cfg"
    folder.mkdir()
    cache = folder / "token_cache.json"
    os.symlink(victim, cache)
    manager = auth_state.auth_manager
    monkeypatch.setattr(manager, "_cache_path", lambda: cache)
    monkeypatch.setattr(manager, "token_info", auth_state.TokenInfo(meta_access_token="EAA" + "y" * 40))
    manager._persist_token()

    assert victim.read_text() == "untouched"
    assert not cache.is_symlink() and "EAA" in cache.read_text()
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp")]


def test_token_save_refuses_a_symlink_planted_at_the_temp_path(tmp_path, monkeypatch):
    """O_EXCL|O_NOFOLLOW on the temp file: a symlink already at that name is never written through,
    and the save fails rather than leaking the token (Errata, ADS-75 Low)."""
    import os

    from armavita_meta_ads_mcp.core import auth_state

    if os.name != "posix":
        pytest.skip("symlink semantics")
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    folder = tmp_path / "cfg"
    folder.mkdir()
    cache = folder / "token_cache.json"
    monkeypatch.setattr(auth_state.secrets, "token_hex", lambda n=None: "fixedname")
    planted = folder / f".{cache.name}.{os.getpid()}.fixedname.tmp"
    os.symlink(victim, planted)

    manager = auth_state.auth_manager
    monkeypatch.setattr(manager, "_cache_path", lambda: cache)
    monkeypatch.setattr(manager, "token_info", auth_state.TokenInfo(meta_access_token="EAA" + "z" * 40))
    with pytest.raises(FileExistsError):
        manager._persist_token()

    assert victim.read_text() == "untouched"
    assert not cache.exists()
    assert planted.is_symlink()  # left alone, not followed or replaced


@pytest.mark.parametrize("app_id", ["", "MISSING_META_APP_ID", "not-a-number"])
def test_login_without_an_app_explains_instead_of_opening_facebook(monkeypatch, capsys, app_id):
    """Without a Meta app, Facebook only says "Invalid App ID" (Malorie, Oct 30). Say what's needed instead."""
    from unittest.mock import patch

    from armavita_meta_ads_mcp.core import auth_state

    monkeypatch.setattr(auth_state.auth_manager, "app_id", app_id)
    with patch.object(auth_state.webbrowser, "open") as browser, patch.object(auth_state, "start_callback_server") as server:
        auth_state.login()
    browser.assert_not_called()
    server.assert_not_called()
    out = capsys.readouterr().out
    assert "META_APP_ID" in out and "localhost:8080/callback" in out


def test_login_with_an_app_still_opens_the_browser(monkeypatch):
    from unittest.mock import patch

    from armavita_meta_ads_mcp.core import auth_state

    monkeypatch.setattr(auth_state.auth_manager, "app_id", "1234567890")
    with patch.object(auth_state.webbrowser, "open") as browser, patch.object(
        auth_state, "start_callback_server", return_value=8080
    ), patch.object(auth_state, "shutdown_callback_server"), patch.object(auth_state.time, "time", side_effect=[0, 10_000]):
        auth_state.login()
    browser.assert_called_once()
    assert "client_id=1234567890" in browser.call_args.args[0]


def test_login_does_not_ask_for_scopes_facebook_rejects():
    """Facebook answers "Invalid Scopes" for these and the login stops (Malorie, Oct 31)."""
    from armavita_meta_ads_mcp.core import auth_state

    scopes = auth_state.AUTH_SCOPE.split(",")
    assert "instagram_basic" not in scopes and "threads_business_basic" not in scopes
    assert {"ads_management", "ads_read"} <= set(scopes)
