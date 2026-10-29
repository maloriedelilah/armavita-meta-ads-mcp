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


# Exact values to remove wherever they appear, whatever surrounds them (ADS-46: the
# shared secret). Registered at startup; plain substring replacement, so linear.
_LITERALS: list = []


def register_secret(value: str) -> None:
    """Redact this exact value from every log line from now on."""
    if value and value not in _LITERALS:
        _LITERALS.append(value)


def redact(text: Any) -> str:
    """Return `text` as a string with anything token-shaped, and every registered secret, replaced."""
    result = str(text)
    for literal in _LITERALS:
        result = result.replace(literal, REDACTED)
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
            # Pre-render the traceback so the formatter uses our redacted text, then drop the
            # live exception: handlers such as rich's RichHandler render from exc_info and would
            # print the unredacted exception (Errata, ADS-46).
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
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


def replace_rich_handlers(logger: logging.Logger) -> None:
    """Swap rich's RichHandler (installed on root by the MCP SDK) for a plain stderr handler.

    RichHandler(rich_tracebacks=True) builds its traceback from the live exception, with
    locals, outside anything a filter can rewrite. A plain handler prints the redacted text.
    """
    import sys

    for handler in list(logger.handlers):
        if type(handler).__name__ == "RichHandler":
            plain = logging.StreamHandler(sys.stderr)
            plain.setLevel(handler.level)
            plain.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logger.removeHandler(handler)
            logger.addHandler(plain)


def install_everywhere(logger_names: Iterable[str] = ()) -> None:
    """Replace RichHandlers, then attach the filter to the root logger's handlers and the named loggers'."""
    replace_rich_handlers(logging.getLogger())
    for name in logger_names:
        replace_rich_handlers(logging.getLogger(name))
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
