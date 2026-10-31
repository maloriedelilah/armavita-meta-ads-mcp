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

    monkeypatch.setattr(auth_state, "complete_oauth_from_auth_code", lambda *a, **k: {"success": True, "token_info": Info()})
    page = callback(f"code=abc&state={STATE}")
    assert "Facebook API Ads MCP Connector" in page
    assert "<h1>Authorization complete</h1>" in page
    assert "saved your Meta access token" in page and "You can close this window." in page
    assert "text-align:center" in page and "sans-serif" in page
    assert "<script" not in page
    assert "EAAB_TOKEN_SENTINEL" not in page


def test_failure_pages_say_nothing_was_saved_and_escape_what_facebook_sent(callback):
    page = callback(f"error=access_denied&error_description={quote('<img src=x onerror=alert(1)>')}&state={STATE}")
    assert "<h1>Authorization failed</h1>" in page and "Nothing was saved" in page
    assert "<img" not in page and "&lt;img" in page
    assert "You can close this window." in page


@pytest.mark.parametrize("query", ["state=" + STATE, "code=abc&state=WRONG"])
def test_missing_code_and_state_mismatch_get_the_styled_failure_page(callback, query):
    page = callback(query)
    assert "Facebook API Ads MCP Connector" in page and "<h1>Authorization failed</h1>" in page


def test_a_stray_error_callback_is_ignored(callback):
    page = callback("error=access_denied&state=WRONG")
    assert "<h1>Ignored</h1>" in page and "nothing changed" in page


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
