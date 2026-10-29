# SPDX-License-Identifier: AGPL-3.0-only

"""A logging filter that keeps Meta tokens out of every log line (ADS-45).

The code already avoids logging tokens: they travel only in the Authorization
header (ADS-44), tool arguments are logged with the token masked, and errors
log exception types. This filter is the backstop for whatever slips past that,
from this package or from libraries (httpx, the MCP SDK, uvicorn). It rewrites
the message, its arguments and any traceback text before a handler formats
them.

It is attached to handlers, not loggers: a filter on a logger only sees
records logged to that exact logger, while a handler sees everything that
reaches it, including records propagated from other libraries.
"""

import logging
import re
from typing import Any, Dict, Iterable

REDACTED = "***REDACTED***"

_PATTERNS = (
    # Authorization: Bearer <token> (and bare "Bearer <token>" anywhere)
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1" + REDACTED),
    # access_token=... in URLs and form bodies
    (re.compile(r"(?i)(access_token=)[^&\s'\"<>]+"), r"\1" + REDACTED),
    # "access_token": "..." / 'access_token': '...' in reprs and JSON
    (re.compile(r"""(?i)(['"]?(?:meta_)?access_token['"]?\s*[:=]\s*['"]?)[^'"\s,}&]+"""), r"\1" + REDACTED),
    # The HTTP-mode shared secret (ADS-46), in header dicts, ASGI header tuples and "name: value" lines
    (re.compile(r"""(?i)(x-armavita-key['"]?\s*[:=,]\s*b?['"]?)[^'"\s,})&]+"""), r"\1" + REDACTED),
    # Meta user/page/system-user tokens on their own: "EAA" followed by a long run
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), REDACTED),
)


def redact(text: Any) -> str:
    """Return `text` as a string with anything token-shaped replaced."""
    result = str(text)
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class TokenRedactingFilter(logging.Filter):
    """Rewrites each record's message and traceback with tokens removed. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001  (a bad format string must not break logging)
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info:
            # Pre-render the traceback so the formatter uses our redacted text.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_FILTER = TokenRedactingFilter()


def install_on_handlers(handlers: Iterable[logging.Handler]) -> None:
    """Attach the redacting filter to each handler (idempotent)."""
    for handler in handlers:
        if not any(isinstance(existing, TokenRedactingFilter) for existing in handler.filters):
            handler.addFilter(_FILTER)


def install_everywhere(logger_names: Iterable[str] = ()) -> None:
    """Attach the filter to the root logger's handlers and to those of the named loggers."""
    install_on_handlers(logging.getLogger().handlers)
    for name in logger_names:
        install_on_handlers(logging.getLogger(name).handlers)


def log_to_stderr_only(logger: logging.Logger) -> None:
    """Replace a logger's file handlers with one redacting stderr handler.

    Used in HTTP mode: nothing collects quietly in a file on the host (or in a
    container), and whatever runs the server decides where stderr goes.
    """
    import sys

    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()
    if not any(isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stderr for h in logger.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(stream)
    install_on_handlers(logger.handlers)


def uvicorn_log_config() -> Dict[str, Any]:
    """uvicorn's default logging config, with the redacting filter on its handlers."""
    import copy

    import uvicorn.config

    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_tokens"] = {"()": f"{__name__}.TokenRedactingFilter"}
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", [])
        if "redact_tokens" not in handler["filters"]:
            handler["filters"].append("redact_tokens")
    return config
