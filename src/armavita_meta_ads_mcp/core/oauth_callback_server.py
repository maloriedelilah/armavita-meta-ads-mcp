# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""Local OAuth callback server used by Meta login flow."""


import html
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .media_helpers import logger


CALLBACK_SERVER_TIMEOUT = 180

# Shared state updated by callback requests.
token_container: dict = {}



def _state_is_valid(state: Optional[str], expected_state: Optional[str]) -> bool:
    """Fail-closed CSRF check: require an issued nonce AND a constant-time match."""
    if not expected_state or not state:
        return False
    return secrets.compare_digest(str(state), str(expected_state))


def reset_token_container() -> None:
    token_container.clear()
    token_container.update(
        {
            "token": None,
            "expires_in": None,
            "meta_user_id": None,
            "auth_code": None,
            "state": None,
            "expected_state": None,
            "redirect_uri": None,
            "status": "pending",
            "error": None,
            "timestamp": None,
        }
    )


reset_token_container()


_callback_lock = threading.Lock()
_callback_thread: Optional[threading.Thread] = None
_callback_httpd: Optional[HTTPServer] = None
_callback_port: Optional[int] = None
_shutdown_timer: Optional[threading.Timer] = None


PAGE_TITLE = "Facebook API Ads MCP Connector"

_PAGE_STYLE = (
    ":root{color-scheme:light dark;--bg:#f7f7f8;--card:#fff;--ink:#1d1d1f;--muted:#5f6368;--line:#dadce0;"
    "--ok:#188038;--bad:#c5221f}"
    "@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1f22;--ink:#ececee;--muted:#a1a4a9;"
    "--line:#34363a;--ok:#81c995;--bad:#f28b82}}"
    "*{box-sizing:border-box}"
    "body{margin:0;background:var(--bg);color:var(--ink);"
    "font:16px/1.55 system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    "main{max-width:34rem;margin:12vh auto;padding:0 16px}"
    ".card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:32px 28px;text-align:center}"
    ".brand{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 20px}"
    ".mark{font-size:34px;line-height:1;margin:0 0 12px}"
    ".ok .mark{color:var(--ok)}.bad .mark{color:var(--bad)}"
    "h1{font-size:24px;line-height:1.25;margin:0 0 14px}"
    "p{margin:0 0 12px;color:var(--muted)}"
    ".close{margin-top:22px;color:var(--ink);font-weight:600}"
)


def render_page(heading: str, paragraphs: list, tone: str = "ok", show_close: bool = True) -> str:
    """A centered, sans-serif page for the browser at the end of --login (ADS-84).

    Every piece of text is escaped here, so callers pass plain strings. No scripts.
    """
    mark = {"ok": "\u2713", "bad": "\u2715"}.get(tone, "")
    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{html.escape(heading)} \u00b7 {html.escape(PAGE_TITLE)}</title>",
        f"<style>{_PAGE_STYLE}</style></head><body><main>",
        f"<div class=\"card {html.escape(tone)}\"><p class=\"brand\">{html.escape(PAGE_TITLE)}</p>",
    ]
    if mark:
        parts.append(f"<p class=\"mark\" aria-hidden=\"true\">{mark}</p>")
    parts.append(f"<h1>{html.escape(heading)}</h1>")
    parts.extend(f"<p>{html.escape(str(text))}</p>" for text in paragraphs)
    if show_close:
        parts.append("<p class=\"close\">You can close this window.</p>")
    parts.append("</div></main></body></html>")
    return "".join(parts)


def _failed_page(reason: str) -> str:
    return render_page(
        "Authorization failed",
        [reason, "Nothing was saved. Go back to your terminal and run --login again."],
        tone="bad",
    )


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            self._handle_callback(parsed)
            return
        # Note: there is deliberately no "/token" route. The access token must
        # never be served over HTTP — any local process could read it. login()
        # reads the in-process token_container directly instead.
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):  # noqa: D401, ANN001
        _ = (fmt, args)
        return

    def _html_response(self, markup: str) -> None:
        body = markup.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _handle_callback(self, parsed_url) -> None:
        params = parse_qs(parsed_url.query)
        auth_code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        auth_error = params.get("error", [None])[0]
        auth_error_description = params.get("error_description", [None])[0]

        if auth_error:
            # An error callback only counts if it carries this login's state.
            # Otherwise any web page could open localhost:<port>/callback?error=x
            # and abort a login in progress. Ignore it and keep waiting.
            if not _state_is_valid(state, token_container.get("expected_state")):
                self._html_response(
                    render_page(
                        "Ignored",
                        ["This page doesn't belong to the login in progress, so nothing changed."],
                        tone="neutral",
                    )
                )
                return
            primary_text = auth_error_description or auth_error
            token_container.update(
                {
                    "status": "error",
                    "error": primary_text,
                    "state": state,
                    "timestamp": time.time(),
                }
            )
            self._html_response(_failed_page(f"Facebook said: {primary_text}"))
            _shutdown_async()
            return

        if not auth_code:
            token_container.update(
                {
                    "status": "error",
                    "error": "missing_code_or_error",
                    "timestamp": time.time(),
                }
            )
            self._html_response(_failed_page("Facebook didn't send back an authorization code."))
            _shutdown_async()
            return

        # CSRF defense: the returned `state` must match the nonce issued by the
        # login flow (issue_login_state). Fail closed — reject before exchanging
        # the code if no nonce was issued or it does not match.
        expected_state = token_container.get("expected_state")
        if not _state_is_valid(state, expected_state):
            token_container.update(
                {
                    "status": "error",
                    "error": "state_mismatch",
                    "state": state,
                    "timestamp": time.time(),
                }
            )
            self._html_response(
                _failed_page("This reply didn't match the login that was started, so it was refused (a CSRF check).")
            )
            _shutdown_async()
            return

        token_container.update(
            {
                "auth_code": auth_code,
                "state": state,
                "status": "exchanging",
                "error": None,
                "timestamp": time.time(),
            }
        )

        try:
            from .auth_state import complete_oauth_from_auth_code

            result = complete_oauth_from_auth_code(
                auth_code,
                redirect_uri=token_container.get("redirect_uri"),
                persist_token=True,
            )
            if result.get("success"):
                token_info = result.get("token_info")
                token_container.update(
                    {
                        "token": getattr(token_info, "meta_access_token", None),
                        "expires_in": getattr(token_info, "expires_in", None),
                        "meta_user_id": getattr(token_info, "meta_user_id", None),
                        "status": "success",
                        "error": None,
                        "timestamp": time.time(),
                    }
                )
                self._html_response(
                    render_page(
                        "Authorization complete",
                        [
                            "Facebook signed you in, and the connector saved your Meta access token on this "
                            "computer, in a file only your user account can read.",
                            "Your AI app can now use the Meta Ads tools with your ad accounts. When the token "
                            "expires, run --login again to get a new one.",
                        ],
                    )
                )
            else:
                error_code = result.get("error", "oauth_completion_failed")
                token_container.update(
                    {
                        "status": "error",
                        "error": error_code,
                        "timestamp": time.time(),
                    }
                )
                self._html_response(
                    _failed_page(f"Facebook's sign-in finished, but getting the token failed ({error_code}).")
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("OAuth completion callback failed: %s", type(exc).__name__)
            token_container.update(
                {
                    "status": "error",
                    "error": str(exc),
                    "timestamp": time.time(),
                }
            )
            self._html_response(_failed_page("Something went wrong while finishing the login."))

        _shutdown_async()



def _choose_port(start_port: int = 8080, attempts: int = 10) -> int:
    port = start_port
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("localhost", port))
            except OSError:
                port += 1
                continue
            return port
    raise RuntimeError(f"Unable to allocate callback port after {attempts} attempts")



def _server_loop(port: int) -> None:
    global _callback_httpd
    try:
        httpd = HTTPServer(("localhost", port), CallbackHandler)
        _callback_httpd = httpd
        logger.info("Callback server listening on http://localhost:%s", port)
        httpd.serve_forever()
    except Exception as exc:  # noqa: BLE001
        logger.error("Callback server loop crashed: %s", type(exc).__name__)



def _shutdown_async() -> None:
    threading.Thread(target=shutdown_callback_server, daemon=True).start()



def shutdown_callback_server() -> None:
    global _callback_thread, _callback_httpd, _callback_port, _shutdown_timer

    with _callback_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None

        if _callback_httpd is not None:
            try:
                _callback_httpd.shutdown()
                _callback_httpd.server_close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error while closing callback server: %s", type(exc).__name__)

        if _callback_thread is not None and _callback_thread.is_alive():
            _callback_thread.join(timeout=3)

        _callback_thread = None
        _callback_httpd = None
        _callback_port = None



def start_callback_server() -> int:
    global _callback_thread, _callback_port, _shutdown_timer

    if os.environ.get("META_ADS_DISABLE_CALLBACK_SERVER"):
        raise RuntimeError("Callback server disabled via META_ADS_DISABLE_CALLBACK_SERVER")

    with _callback_lock:
        reset_token_container()

        if _callback_port is not None:
            token_container["redirect_uri"] = f"http://localhost:{_callback_port}/callback"
            return _callback_port

        port = _choose_port()
        _callback_port = port
        token_container["redirect_uri"] = f"http://localhost:{port}/callback"

        _callback_thread = threading.Thread(target=_server_loop, args=(port,), daemon=True)
        _callback_thread.start()

        # short delay to let socket bind and thread initialize
        time.sleep(0.3)

        def timed_shutdown() -> None:
            logger.info("Callback server timed out after %ss", CALLBACK_SERVER_TIMEOUT)
            shutdown_callback_server()

        _shutdown_timer = threading.Timer(CALLBACK_SERVER_TIMEOUT, timed_shutdown)
        _shutdown_timer.daemon = True
        _shutdown_timer.start()

        return port