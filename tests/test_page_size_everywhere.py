# SPDX-License-Identifier: AGPL-3.0-only

"""Errata's Low on #36 (ADS-113): every tool's page_size is 1-500, checked before pydantic coerces."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp.shared.memory import create_connected_server_and_client_session

from armavita_meta_ads_mcp.core import campaign_tools
from armavita_meta_ads_mcp.core.mcp_runtime import _import_tool_modules, mcp_server

_import_tool_modules()


def _call(arguments):
    async def run():
        async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
            with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
                api.return_value = {"data": []}
                result = await client.call_tool("list_campaigns", {"ad_account_id": "act_1", "meta_access_token": "t", **arguments})
                return result, api

    return asyncio.run(run())


@pytest.mark.parametrize("value", [0, 501, 10**9, -1, True, 1.5, "1e3", "²", "9" * 5000])
def test_a_bad_page_size_is_refused_before_any_call(value):
    result, api = _call({"page_size": value})
    assert result.isError is True and "page_size must be a whole number from 1 to 500" in result.content[0].text
    api.assert_not_called()


@pytest.mark.parametrize("value", [1, 25, 500, "50"])
def test_a_good_page_size_goes_through(value):
    result, api = _call({"page_size": value})
    assert result.isError is False and api.called


def test_every_tool_with_page_size_has_a_default_within_the_cap():
    import inspect

    for tool in mcp_server._tool_manager.list_tools():
        param = inspect.signature(tool.fn).parameters.get("page_size")
        if param is not None and isinstance(param.default, int):
            assert 1 <= param.default <= 500, tool.name
