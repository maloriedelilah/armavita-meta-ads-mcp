# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP mode (ADS-43): the Meta token comes only from each request's Authorization header.

These drive the real Starlette app FastMCP builds for streamable HTTP, through
httpx's ASGI transport, so headers travel the same path a connector's do.
"""

import asyncio
import json
import random
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import auth_state, http_mode, mcp_runtime
from armavita_meta_ads_mcp.core.mcp_runtime import mcp_server

_PATCH_TARGET = "armavita_meta_ads_mcp.core.account_tools.make_api_request"
_BASE_URL = "http://127.0.0.1:8765"
_ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def http_server(monkeypatch):
    """HTTP mode on, a fresh app, and an env token that must never be used."""
    monkeypatch.setenv("META_ACCESS_TOKEN", "ENV_TOKEN_MUST_NOT_BE_USED_0123456789")
    settings = mcp_server.settings
    saved = (
        settings.host,
        settings.port,
        settings.streamable_http_path,
        settings.stateless_http,
        settings.json_response,
        list(settings.transport_security.allowed_hosts) if settings.transport_security else None,
        list(settings.transport_security.allowed_origins) if settings.transport_security else None,
    )
    saved_tools = dict(mcp_server._tool_manager._tools)  # configure_http unregisters some tools
    mcp_runtime.configure_http("127.0.0.1", 8765, ["mcp.example.com"])
    mcp_server._session_manager = None  # the SDK builds it once per process; tests need a fresh one
    app = mcp_server.streamable_http_app()
    yield app
    http_mode.disable()
    mcp_server._tool_manager._tools.clear()
    mcp_server._tool_manager._tools.update(saved_tools)
    mcp_server._session_manager = None
    settings.host, settings.port, settings.streamable_http_path, settings.stateless_http, settings.json_response = saved[:5]
    if settings.transport_security is not None:
        settings.transport_security.allowed_hosts[:] = saved[5]
        settings.transport_security.allowed_origins[:] = saved[6]


async def _calls(app, specs):
    """Send several tools/call requests through one running app; return [(http_status, JSON body or None)].

    Each spec is (tool, arguments, headers, base_url). The SDK's session manager
    can only be run once per instance, so all of a test's requests share one run.
    """
    results = []
    async with mcp_server.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        for tool, arguments, headers, base_url in specs:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
            request_headers = {"accept": _ACCEPT, "content-type": "application/json", **(headers or {})}
            async with httpx.AsyncClient(transport=transport, base_url=base_url or _BASE_URL) as client:
                response = await client.post("/mcp", json=body, headers=request_headers)
            try:
                results.append((response.status_code, response.json()))
            except ValueError:
                results.append((response.status_code, None))
    return results


async def _call(app, tool, arguments, headers=None, base_url=None):
    return (await _calls(app, [(tool, arguments, headers, base_url)]))[0]


async def _concurrent_calls(app, specs):
    """Like _calls, but all requests in flight at once."""
    async with mcp_server.session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as client:

            async def one(tool, arguments, headers):
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
                request_headers = {"accept": _ACCEPT, "content-type": "application/json", **(headers or {})}
                response = await client.post("/mcp", json=body, headers=request_headers)
                return response.status_code, response.json()

            return await asyncio.gather(*(one(t, a, h) for t, a, h in specs))


def _tool_text(payload):
    return "".join(block.get("text", "") for block in payload["result"]["content"])


@pytest.mark.asyncio
async def test_bearer_header_is_the_token_used(http_server):
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"data": []}
        status, payload = await _call(http_server, "list_ad_accounts", {}, {"authorization": "Bearer TOKEN_A_0123456789abcdef"})

    assert status == 200
    assert payload["result"].get("isError") is not True
    assert mock_api.call_args.args[1] == "TOKEN_A_0123456789abcdef"


@pytest.mark.asyncio
async def test_each_request_sees_only_its_own_token(http_server):
    seen = []
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = lambda *a, **k: seen.append(a[1]) or {"data": []}
        await _calls(
            http_server,
            [
                ("list_ad_accounts", {}, {"authorization": "Bearer TOKEN_A_0123456789abcdef"}, None),
                ("list_ad_accounts", {}, {"authorization": "Bearer TOKEN_B_0123456789abcdef"}, None),
                ("list_ad_accounts", {}, None, None),
            ],
        )

    # The third call had no header: no Graph call at all, and nothing carried over.
    assert seen == ["TOKEN_A_0123456789abcdef", "TOKEN_B_0123456789abcdef"]


@pytest.mark.asyncio
async def test_no_header_is_refused_without_env_fallback(http_server):
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        status, payload = await _call(http_server, "list_ad_accounts", {})

    assert status == 200
    mock_api.assert_not_called()
    text = _tool_text(payload)
    assert "Not authenticated" in text
    assert "Authorization" in text
    assert "ENV_TOKEN" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    ["Basic dXNlcjpwYXNz", "Bearer", "Bearer    ", "TOKEN_A_0123456789abcdef", "Bearer TOKEN A", "Bearer TOKEN\tA"],
)
async def test_non_bearer_or_empty_header_is_refused(http_server, header):
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        _, payload = await _call(http_server, "list_ad_accounts", {}, {"authorization": header})

    mock_api.assert_not_called()
    assert "Not authenticated" in _tool_text(payload)


@pytest.mark.asyncio
async def test_token_argument_is_refused_even_with_a_header(http_server):
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        _, payload = await _call(
            http_server,
            "list_ad_accounts",
            {"meta_access_token": "ARG_TOKEN_0123456789abcdef"},
            {"authorization": "Bearer TOKEN_A_0123456789abcdef"},
        )

    mock_api.assert_not_called()
    assert payload["result"]["isError"] is True
    assert "not accepted as a tool argument" in _tool_text(payload)
    assert "ARG_TOKEN" not in _tool_text(payload)


@pytest.mark.asyncio
async def test_token_argument_is_refused_on_tools_without_graph_calls(http_server):
    _, payload = await _call(
        http_server,
        "apply_recommendation",
        {"object_id": "1", "recommendation_data": {"code": "x"}, "meta_access_token": "ARG_TOKEN_0123456789abcdef"},
        {"authorization": "Bearer TOKEN_A_0123456789abcdef"},
    )

    assert payload["result"]["isError"] is True
    assert "not accepted as a tool argument" in _tool_text(payload)
    assert "ARG_TOKEN" not in _tool_text(payload)


@pytest.mark.asyncio
async def test_concurrent_requests_each_see_only_their_own_token(http_server):
    # ADS-43 review M2: requests in flight together, with random delays inside the Graph call.
    async def fake_graph(endpoint, token, params, *args, **kwargs):
        await asyncio.sleep(random.uniform(0, 0.02))
        return {"data": [{"endpoint": endpoint, "token": token}]}

    tokens = {str(100000 + i): f"TOKEN_{i:03d}_0123456789abcdef" for i in range(40)}  # meta_user_id must be numeric (ADS-53)
    specs = [("list_ad_accounts", {"meta_user_id": who}, {"authorization": f"Bearer {tok}"}) for who, tok in tokens.items()]
    with patch(_PATCH_TARGET, new=AsyncMock(side_effect=fake_graph)):
        results = await _concurrent_calls(http_server, specs)

    assert len(results) == len(tokens)
    for (who, tok), (status, payload) in zip(tokens.items(), results):
        assert status == 200
        text = _tool_text(payload)
        assert f"{who}/adaccounts" in text
        assert tok in text  # this request's token, and no other
        assert all(other not in text for name, other in tokens.items() if name != who)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"Meta_Access_Token": "ARG_TOKEN_0123456789abcdef"}, "not accepted as a tool argument"),
        ({"access_token": "ARG_TOKEN_0123456789abcdef"}, "not accepted as a tool argument"),
        ({"meta_user_id": "me?access_token=ARG_TOKEN_0123456789abcdef"}, "contains 'access_token'"),
        ({"meta_user_id": "me?fields=id"}, "query strings are not accepted"),
        ({"meta_user_id": "me#frag"}, "query strings are not accepted"),
    ],
)
async def test_smuggled_tokens_and_query_strings_are_refused(http_server, arguments, expected):
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        _, payload = await _call(http_server, "list_ad_accounts", arguments, {"authorization": "Bearer TOKEN_A_0123456789abcdef"})

    mock_api.assert_not_called()
    assert payload["result"]["isError"] is True
    assert expected in _tool_text(payload)
    assert "ARG_TOKEN" not in _tool_text(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"meta_access_token": ""}, {"meta_access_token": None}])
async def test_empty_token_argument_is_harmless(http_server, arguments):
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"data": []}
        await _call(http_server, "list_ad_accounts", arguments, {"authorization": "Bearer TOKEN_A_0123456789abcdef"})

    assert mock_api.call_args.args[1] == "TOKEN_A_0123456789abcdef"


@pytest.mark.asyncio
async def test_allowed_public_host_is_accepted_and_unknown_host_refused(http_server):
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"data": []}
        (ok_status, _), (bad_status, _) = await _calls(
            http_server,
            [
                ("list_ad_accounts", {}, {"authorization": "Bearer TOKEN_A_0123456789abcdef"}, "https://mcp.example.com"),
                ("list_ad_accounts", {}, {"authorization": "Bearer TOKEN_A_0123456789abcdef"}, "https://evil.example.net"),
            ],
        )

    assert ok_status == 200
    assert bad_status == 421


@pytest.mark.asyncio
async def test_get_current_access_token_ignores_env_in_http_mode(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "ENV_TOKEN_MUST_NOT_BE_USED_0123456789")
    http_mode.enable()
    try:
        # Outside any request there is no header, so no token: never the env one.
        assert await auth_state.get_current_access_token() is None
    finally:
        http_mode.disable()


@pytest.mark.asyncio
async def test_stdio_mode_still_uses_env_token(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "ENV_TOKEN_FOR_STDIO_0123456789")
    assert not http_mode.is_enabled()
    assert await auth_state.get_current_access_token() == "ENV_TOKEN_FOR_STDIO_0123456789"


def test_bad_bearer_does_not_delete_the_login_cache(tmp_path, monkeypatch):
    # ADS-43 review M1: Graph auth errors call invalidate_token(); in HTTP mode it must not
    # touch the operator's stdio login cache.
    cache = tmp_path / "token_cache.json"
    cache.write_text("{}")
    monkeypatch.setattr(auth_state.auth_manager, "_cache_path", lambda: cache)
    sentinel = object()
    monkeypatch.setattr(auth_state.auth_manager, "token_info", sentinel)
    http_mode.enable()
    try:
        auth_state.auth_manager.invalidate_token()
    finally:
        http_mode.disable()

    assert cache.exists()
    assert auth_state.auth_manager.token_info is sentinel


def test_stdio_invalidate_token_still_clears_the_cache(tmp_path, monkeypatch):
    cache = tmp_path / "token_cache.json"
    cache.write_text("{}")
    monkeypatch.setattr(auth_state.auth_manager, "_cache_path", lambda: cache)
    monkeypatch.setattr(auth_state.auth_manager, "token_info", object())
    assert not http_mode.is_enabled()
    auth_state.auth_manager.invalidate_token()

    assert not cache.exists()
    assert auth_state.auth_manager.token_info is None


def test_configure_http_refuses_to_start_without_host_checking(monkeypatch):
    monkeypatch.setattr(mcp_server.settings, "transport_security", None)
    try:
        with pytest.raises(RuntimeError, match="DNS-rebinding"):
            mcp_runtime.configure_http("127.0.0.1", 8765, [])
    finally:
        http_mode.disable()
