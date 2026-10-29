# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""FastMCP runtime assembly and CLI entrypoint (stdio by default, streamable HTTP optional)."""

import argparse
import logging
import os
import sys
from typing import Any, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from . import http_guard, http_mode, log_redaction
from .auth_state import login as login_auth
from .media_helpers import logger


class _ArmavitaFastMCP(FastMCP):
    """FastMCP with one HTTP-mode guard on calls that come from the client.

    In HTTP mode a Meta token must arrive only in the request's Authorization
    header. A token passed as the `meta_access_token` tool argument would have
    travelled through the agent's context and transcript, so it is refused for
    every tool. Tool-to-tool calls inside the server don't pass through here.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        if http_mode.is_enabled():
            problem = http_mode.argument_problem(arguments or {})
            if problem:
                raise ToolError(problem)
        return await super().call_tool(name, arguments)


mcp_server = _ArmavitaFastMCP("meta-ads")

# Prevent third-party HTTP libraries from logging full request URLs that can
# include access_token query parameters.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def login_cli() -> None:
    """Run local OAuth login flow from CLI."""
    logger.info("cli_login_flow_start")
    print("Starting Armavita CLI authentication flow...")
    login_auth()


def _configure_meta_app_id(cli_app_id: Optional[str]) -> None:
    from .auth_state import auth_manager, meta_config

    chosen = cli_app_id or os.environ.get("META_APP_ID")
    if not chosen:
        logger.warning("META_APP_ID not configured")
        return

    auth_manager.app_id = chosen
    meta_config.set_app_id(chosen)
    logger.info("meta_app_id_configured=%s", chosen)


def _import_tool_modules() -> None:
    from . import (  # noqa: F401
        account_controls_tools,
        account_tools,
        ad_tools,
        adset_tools,
        ads_archive_tools,
        audience_tools,
        budget_schedule_tools,
        campaign_tools,
        capi_tools,
        catalog_tools,
        conversion_tools,
        derived_metrics_tools,
        duplication_tools,
        insight_tools,
        insights_async_tools,
        partnership_tools,
        reach_frequency_tools,
        recommendation_tools,
        report_tools,
        research_tools,
        targeting_tools,
        threads_tools,
    )


_LIBRARY_LOGGERS = ("mcp", "httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access")


def _run_stdio() -> int:
    _import_tool_modules()
    log_redaction.install_everywhere(_LIBRARY_LOGGERS)
    mcp_server.run(transport="stdio")
    return 0


def configure_http(host: str, port: int, allowed_hosts: Optional[List[str]] = None, path: str = "/mcp") -> None:
    """Put the server into HTTP mode and set where it listens.

    Stateless JSON responses: every request stands alone, so a tunnel or proxy
    in front needs no session affinity. When bound to a loopback address the
    SDK's DNS-rebinding protection only accepts localhost Host headers, so the
    public name a tunnel forwards (e.g. mcp.example.com) must be listed in
    `allowed_hosts`.
    """
    settings = mcp_server.settings
    security = settings.transport_security
    if security is None or not security.enable_dns_rebinding_protection:
        # The SDK enables Host/Origin checking for loopback binds. Without it,
        # any Host would be accepted, so refuse to start rather than run open.
        # Checked before anything is changed, so a refusal leaves the server as it was.
        raise RuntimeError(
            "HTTP mode needs the MCP SDK's DNS-rebinding protection (transport_security), "
            "and it is not active. Refusing to start."
        )
    _import_tool_modules()
    http_mode.enable()
    registered = {tool.name for tool in mcp_server._tool_manager.list_tools()}
    for name in http_mode.UNREGISTERED_TOOLS:
        if name in registered:
            mcp_server.remove_tool(name)
    settings.host = host
    settings.port = port
    settings.streamable_http_path = path
    settings.stateless_http = True
    settings.json_response = True
    for name in allowed_hosts or []:
        name = name.strip()
        if not name:
            continue
        for entry in (name, f"{name}:*"):
            if entry not in security.allowed_hosts:
                security.allowed_hosts.append(entry)
        origin = f"https://{name}"
        if origin not in security.allowed_origins:
            security.allowed_origins.append(origin)


def build_http_app(shared_secret: Optional[str]):
    """The MCP app behind the ADS-46 guard (shared secret, body cap). configure_http() must run first."""
    return http_guard.HttpGuard(mcp_server.streamable_http_app(), shared_secret)


def _run_streamable_http(
    host: str, port: int, allowed_hosts: Optional[List[str]], path: str, shared_secret: Optional[str]
) -> int:
    import uvicorn

    configure_http(host, port, allowed_hosts, path)
    app = build_http_app(shared_secret)
    logger.info(
        "http_mode_start host=%s port=%s path=%s shared_secret=%s max_body_bytes=%s",
        host,
        port,
        path,
        "on" if shared_secret else "OFF",
        http_guard.MAX_BODY_BYTES,
    )
    # access_log=False: uvicorn's access log is off so request lines and
    # headers are never written by the HTTP layer.
    # ADS-45: the redacting filter goes on every handler that could see a request
    # (root, the MCP SDK, httpx, and uvicorn's own handlers via its log config).
    log_redaction.install_everywhere(_LIBRARY_LOGGERS)
    # In HTTP mode, no server.log: the package logs to stderr only (Errata, ADS-45).
    log_redaction.log_to_stderr_only(logger)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        log_config=log_redaction.uvicorn_log_config(),
    )
    return 0


def main() -> int:
    """CLI entrypoint used by `python -m armavita_meta_ads_mcp` and scripts."""
    parser = argparse.ArgumentParser(
        description="Armavita Meta Ads MCP (stdio by default; streamable HTTP with --transport streamable-http).",
        epilog="Use with local MCP clients such as Claude Code, Cursor, and Codex, or as a remote connector in HTTP mode.",
    )
    parser.add_argument("--login", action="store_true", help="Run login flow and cache token")
    parser.add_argument("--app-id", type=str, help="Override META_APP_ID")
    parser.add_argument("--version", action="store_true", help="Print package version")
    parser.add_argument(
        "--transport",
        type=str,
        default="stdio",
        choices=["stdio", "streamable-http"],
        help="stdio (default) or streamable-http. In HTTP mode the Meta token comes only from each request's "
        "Authorization: Bearer header; META_ACCESS_TOKEN and the login cache are ignored.",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="HTTP mode: address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP mode: port to bind (default 8765)")
    parser.add_argument("--path", type=str, default="/mcp", help="HTTP mode: endpoint path (default /mcp)")
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="HTTP mode: a public host name a tunnel forwards to this server (repeatable), e.g. mcp.example.com",
    )
    parser.add_argument(
        "--no-shared-secret",
        action="store_true",
        help="HTTP mode: run without the X-Armavita-Key check. Only for a local test with nothing exposed; "
        "refused unless --host is a loopback address and no --allowed-host is given.",
    )

    args = parser.parse_args()

    if args.version:
        from armavita_meta_ads_mcp import __version__

        print(f"Armavita Meta Ads MCP v{__version__}")
        return 0

    _configure_meta_app_id(args.app_id)

    if args.login:
        login_cli()
        return 0

    if args.transport == "streamable-http":
        # ADS-46: a shared secret is required unless explicitly waived for a purely local test.
        if args.no_shared_secret:
            if args.host not in ("127.0.0.1", "localhost", "::1") or args.allowed_host:
                print("--no-shared-secret is only allowed on a loopback --host with no --allowed-host.", file=sys.stderr)
                return 2
            shared_secret = None
            logger.warning("http_shared_secret_off reason=--no-shared-secret (local test only)")
        else:
            shared_secret = http_guard.secret_from_env()
            problem = http_guard.secret_problem(shared_secret)
            if problem:
                print(problem, file=sys.stderr)
                return 2
            # Redact the exact value too, not just after the header name (Errata, ADS-46 Low).
            log_redaction.register_secret(shared_secret)
        return _run_streamable_http(args.host, args.port, args.allowed_host, args.path, shared_secret)

    return _run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())