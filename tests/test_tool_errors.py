# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-59: failed calls come back as isError, writes are never retried into duplicates, and
a write whose outcome is unknown says so."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp.shared.memory import create_connected_server_and_client_session

from armavita_meta_ads_mcp.core import adset_tools, graph_client
from armavita_meta_ads_mcp.core.mcp_runtime import _import_tool_modules, mcp_server

_import_tool_modules()
_RealAsyncClient = httpx.AsyncClient


def _call(tool: str, arguments: dict, reply):
    """A real MCP client session; the tool's Graph call answers with `reply`."""

    async def run():
        async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
            with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
                api.return_value = reply
                return await client.call_tool(tool, {"meta_access_token": "t", **arguments})

    return asyncio.run(run())


# --- isError --------------------------------------------------------------------------------

def test_a_graph_error_is_iserror_with_the_tools_json_as_text():
    graph_400 = {"error": {"message": "HTTP Error: 400", "details": {"error": {"code": 100}}}}
    result = _call("update_ad_set", {"ad_set_id": "1", "status": "PAUSED"}, graph_400)
    assert result.isError is True
    assert json.loads(result.content[0].text) == graph_400  # unchanged, so readers (and the bridge) can parse it


def test_a_local_validation_error_is_iserror_and_not_rewrapped():
    result = _call("update_ad_set", {"ad_set_id": "1", "daily_budget": -5}, {"success": True})
    assert result.isError is True
    body = json.loads(result.content[0].text)
    assert "data" not in body and "daily_budget" in body["error"]


def test_success_false_is_iserror():
    result = _call("update_ad_set", {"ad_set_id": "1", "status": "PAUSED"}, {"success": False})
    assert result.isError is True


def test_a_success_is_not_an_error():
    result = _call("update_ad_set", {"ad_set_id": "1", "status": "PAUSED"}, {"success": True})
    assert result.isError is False


# --- retries and ambiguous writes ------------------------------------------------------------

def _graph(handler):
    """Run make_api_request against a fake Graph; returns (result, number of requests)."""
    calls = []

    def counted(request):
        calls.append(request)
        return handler(request)

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(*args, transport=httpx.MockTransport(counted), **kwargs)

    async def run(method):
        with patch.object(graph_client.httpx, "AsyncClient", side_effect=client_factory), patch.object(
            graph_client.asyncio, "sleep", new_callable=AsyncMock
        ):
            return await graph_client.make_api_request("act_1/campaigns", "t", {"name": "x"}, method=method)

    return run, calls


def test_a_post_with_a_rate_limit_code_inside_a_500_is_not_retried():
    """The audit's case: POST 500 {"code": 4} made four campaigns."""
    run, calls = _graph(lambda r: httpx.Response(500, json={"error": {"code": 4, "message": "limit"}}))
    result = asyncio.run(run("POST"))
    assert len(calls) == 1
    assert result["error"]["write_outcome"] == "unknown" and "Read the object back" in result["error"]["next_step"]


def test_a_post_rate_limited_with_a_4xx_is_retried():
    replies = iter([httpx.Response(400, json={"error": {"code": 17}}), httpx.Response(200, json={"id": "c1"})])
    run, calls = _graph(lambda r: next(replies))
    assert asyncio.run(run("POST")) == {"id": "c1"} and len(calls) == 2


def test_a_get_is_still_retried_on_a_500():
    replies = iter([httpx.Response(500, json={"error": {"code": 2}}), httpx.Response(200, json={"data": []})])
    run, calls = _graph(lambda r: next(replies))
    assert asyncio.run(run("GET")) == {"data": []} and len(calls) == 2


def test_a_post_that_timed_out_may_have_landed_and_says_so():
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    run, calls = _graph(timeout)
    result = asyncio.run(run("POST"))
    assert len(calls) == 1
    assert result["error"]["write_outcome"] == "unknown" and "may have been applied" in result["error"]["next_step"]


def test_a_post_that_never_connected_was_not_sent():
    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    run, calls = _graph(refused)
    result = asyncio.run(run("POST"))
    assert result["error"]["write_outcome"] == "not_sent" and "next_step" not in result["error"]
