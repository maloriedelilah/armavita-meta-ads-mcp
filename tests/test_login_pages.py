"""ADS-84: the pages the browser shows at the end of --login."""

import http.client
import threading
from http.server import HTTPServer
from urllib.parse import quote, urlparse

import pytest

from armavita_meta_ads_mcp.core import auth_state, oauth_callback_server as ocs

STATE = "REAL_STATE_0123456789"


@pytest.fixture
def callback(monkeypatch):
    monkeypatch.setitem(ocs.token_container, "expected_state", STATE)
    monkeypatch.setitem(ocs.token_container, "status", "waiting")
    monkeypatch.setitem(ocs.token_container, "error", None)
    monkeypatch.setattr(ocs, "_shutdown_async", lambda: None)

    def run(query):
        responses = []
        handler = ocs.CallbackHandler.__new__(ocs.CallbackHandler)
        monkeypatch.setattr(handler, "_html_response", lambda markup: responses.append(markup), raising=False)
        handler._handle_callback(urlparse(f"http://localhost:8080/callback?{query}"))
        return responses[0]

    return run


def test_success_page_is_titled_centered_and_explains(callback, monkeypatch):
    class Info:
        meta_access_token = "EAAB_TOKEN_SENTINEL_0123456789"
        expires_in = 5000000
        meta_user_id = "1"

    monkeypatch.setattr(auth_state, "complete_oauth_from_auth_code",
                        lambda *a, **k: {"success": True, "token_info": Info(), "used_long_lived_token": True})
    page = callback(f"code=abc&state={STATE}")
    assert "Facebook API Ads MCP Connector" in page
    assert "The token lasts about 58 days." in page  # from expires_in (5,000,000 s)
    assert "<h1>Connected to Meta</h1>" in page
    assert "Your Meta access token is saved on this computer" in page and "You can close this window." in page
    assert "text-align:center" in page and "sans-serif" in page
    assert "<script" not in page
    assert "EAAB_TOKEN_SENTINEL" not in page


def test_failure_pages_say_nothing_was_saved_and_escape_what_facebook_sent(callback):
    page = callback(f"error=access_denied&error_description={quote('<img src=x onerror=alert(1)>')}&state={STATE}")
    assert "<h1>Not connected</h1>" in page and "Nothing was saved" in page
    assert "<img" not in page and "&lt;img" in page
    assert "You can close this window." in page


@pytest.mark.parametrize("query", ["state=" + STATE, "code=abc&state=WRONG"])
def test_missing_code_and_state_mismatch_get_the_styled_failure_page(callback, query):
    page = callback(query)
    assert "Facebook API Ads MCP Connector" in page and "<h1>Not connected</h1>" in page


def test_a_stray_error_callback_is_ignored(callback):
    page = callback("error=access_denied&state=WRONG")
    assert "<h1>Nothing changed</h1>" in page and "older sign-in" in page


def test_render_page_escapes_every_string():
    page = ocs.render_page("<b>h</b>", ["<i>p</i>"], tone='"><x')
    assert "<b>" not in page and "<i>" not in page and '"><x' not in page


def test_pages_are_served_with_no_store_and_a_script_free_csp(monkeypatch):
    monkeypatch.setitem(ocs.token_container, "expected_state", STATE)
    monkeypatch.setattr(ocs, "_shutdown_async", lambda: None)
    server = HTTPServer(("127.0.0.1", 0), ocs.CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/callback?error=x&state=WRONG")
        r = conn.getresponse()
        r.read()
    finally:
        thread.join(5)
        server.server_close()
    assert r.status == 200
    assert r.getheader("Cache-Control") == "no-store"
    csp = r.getheader("Content-Security-Policy")
    assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    assert r.getheader("X-Frame-Options") == "DENY"


def test_a_failed_token_exchange_keeps_the_error_code_off_the_page(callback, monkeypatch):
    """ADS-220 (standards:page-text): plain words on the page; the code is printed in the terminal."""
    monkeypatch.setattr(auth_state, "complete_oauth_from_auth_code", lambda *a, **k: {"success": False, "error": "oauth_completion_failed"})
    page = callback(f"code=abc&state={STATE}")
    assert "<h1>Not connected</h1>" in page and "The details are in your terminal." in page
    assert "oauth_completion_failed" not in page
    assert ocs.token_container["error"] == "oauth_completion_failed"  # what the terminal prints


def test_a_state_mismatch_is_explained_without_jargon(callback):
    page = callback("code=abc&state=WRONG")
    assert "didn&#x27;t come from the sign-in you started" in page and "CSRF" not in page


@pytest.mark.parametrize("expires_in, words", [(5400, "about 2 hours"), (3000, "about 50 minutes"), (None, "an hour or two")])
def test_a_short_lived_token_is_never_called_60_days(callback, monkeypatch, expires_in, words):
    """ADS-228 (Errata's L1 on ADS-220): when Facebook won't extend the token, the page says so."""
    class Info:
        meta_access_token = "EAAB_SHORT_SENTINEL"
        meta_user_id = "1"

    Info.expires_in = expires_in
    monkeypatch.setattr(auth_state, "complete_oauth_from_auth_code",
                        lambda *a, **k: {"success": True, "token_info": Info(), "used_long_lived_token": False})
    page = callback(f"code=abc&state={STATE}")
    assert "<h1>Connected to Meta</h1>" in page
    assert f"Facebook didn&#x27;t extend this token, so it lasts only {words}." in page
    assert "about 60 days" in page and "lasts about 60 days. When" not in page
