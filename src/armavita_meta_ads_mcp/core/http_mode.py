# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP transport mode: the Meta token comes only from each request.

In HTTP mode the server is meant to sit behind a Claude custom connector that
sends ``Authorization: Bearer <Meta token>`` on every call. The token is read
from the current request when a tool needs it and is never stored: no global,
no cache, nothing on disk. There is deliberately no fallback to
``META_ACCESS_TOKEN``, the ``--login`` cache or app credentials, so a request
without the header cannot borrow whatever token the host happens to have.

stdio mode is unaffected: nothing here is consulted unless ``enable()`` ran.
"""

from typing import Optional

# Process-wide switch, set once at startup by the HTTP runner. It holds no secret.
_enabled = False

TOKEN_ARGUMENT_REFUSED = (
    "meta_access_token is not accepted as a tool argument on this server. "
    "It runs in HTTP mode, where the Meta token comes only from the connector's "
    "Authorization: Bearer header, so it never passes through the conversation. "
    "Call the tool again without meta_access_token."
)

MISSING_BEARER = (
    "This server runs in HTTP mode and the request carried no Meta token. "
    "Send it in the request's Authorization header as 'Bearer <token>' "
    "(the custom connector's request header). There is no fallback to "
    "environment or cached tokens in HTTP mode."
)


def enable() -> None:
    """Switch the process into HTTP mode. Called once by the HTTP runner."""
    global _enabled
    _enabled = True


def disable() -> None:
    """Switch HTTP mode off again (used by tests)."""
    global _enabled
    _enabled = False


def is_enabled() -> bool:
    return _enabled


def request_bearer_token() -> Optional[str]:
    """Return the bearer token from the request being handled, or None.

    Reads FastMCP's request context for the current tool call. Returns None
    outside a request, when the header is missing, or when it is not a
    non-empty Bearer credential.
    """
    from .mcp_runtime import mcp_server  # local import: mcp_runtime imports auth_state

    try:
        request = mcp_server.get_context().request_context.request
    except (LookupError, ValueError):
        return None
    if request is None:
        return None

    headers = getattr(request, "headers", None)
    if headers is None:
        return None

    scheme, _, credential = str(headers.get("authorization", "")).strip().partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        return None
    return credential
