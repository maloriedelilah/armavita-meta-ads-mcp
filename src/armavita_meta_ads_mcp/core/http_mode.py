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

import re
from typing import Any, Optional

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


SMUGGLED_TOKEN_REFUSED = (
    "A tool argument contains 'access_token'. On this server the Meta token comes only "
    "from the connector's Authorization header, so tokens inside arguments are refused."
)

BAD_ID_REFUSED = (
    "An ID argument contains '?', '&' or '#'. IDs are path segments here; query strings "
    "are not accepted inside them."
)

# RFC 6750 b64token. Meta tokens are plain alphanumerics, well inside this set.
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9\-._~+/]+=*$")
_TOKEN_KEYS = {"meta_access_token", "access_token"}


def argument_problem(arguments: Any, key: str = "") -> Optional[str]:
    """Return a refusal message if client-supplied tool arguments try to carry a token.

    Checks every level of the arguments: a token-named key with a value (any
    letter case), any string containing 'access_token', and ID arguments
    (keys ending in '_id' or '_ids') holding '?', '&' or '#'.
    """
    if isinstance(arguments, dict):
        for name, value in arguments.items():
            name = str(name)
            if name.lower() in _TOKEN_KEYS and value not in (None, ""):
                return TOKEN_ARGUMENT_REFUSED
            problem = argument_problem(value, name)
            if problem:
                return problem
        return None
    if isinstance(arguments, (list, tuple)):
        for value in arguments:
            problem = argument_problem(value, key)
            if problem:
                return problem
        return None
    if isinstance(arguments, str):
        if "access_token" in arguments.lower():
            return SMUGGLED_TOKEN_REFUSED
        lowered_key = key.lower()
        if (lowered_key.endswith("_id") or lowered_key.endswith("_ids")) and any(c in arguments for c in "?&#"):
            return BAD_ID_REFUSED
    return None


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
    Bearer credential made only of RFC 6750 token characters.
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
    if scheme.lower() != "bearer" or not _BEARER_TOKEN.match(credential):
        # Missing, empty, spaces inside, control or non-ASCII characters: not a token.
        return None
    return credential
