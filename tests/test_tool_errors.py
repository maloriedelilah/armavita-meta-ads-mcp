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


# --- clone tools (Errata's blocker on #29) ---------------------------------------------------

def _clone(handler):
    """clone_campaign over a real MCP client session against a fake Graph."""
    calls = []

    def counted(request):
        calls.append(request)
        return handler(request)

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(*args, transport=httpx.MockTransport(counted), **kwargs)

    async def run():
        async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
            with patch.object(graph_client.httpx, "AsyncClient", side_effect=client_factory), patch.object(
                graph_client.asyncio, "sleep", new_callable=AsyncMock
            ):
                return await client.call_tool("clone_campaign", {"campaign_id": "120000000000001", "meta_access_token": "t"})

    result = asyncio.run(run())
    posts = [c for c in calls if c.method == "POST"]
    return result, posts


def test_a_clone_post_500_with_code_4_is_not_told_to_retry():
    result, posts = _clone(lambda r: httpx.Response(500, json={"error": {"code": 4, "message": "limit"}}))
    text = result.content[0].text
    assert result.isError is True and len(posts) == 1
    assert "Retry with backoff" not in text and '"write_outcome": "unknown"' in text and "Read the object back" in text


def test_a_clone_post_that_timed_out_says_it_may_have_landed():
    def timeout(request):
        if request.method == "POST":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"id": "120000000000001", "objective": "OUTCOME_TRAFFIC"})

    result, posts = _clone(timeout)
    text = result.content[0].text
    assert result.isError is True and len(posts) == 1
    assert '"write_outcome": "unknown"' in text and "may have been applied" in text


def test_a_clone_rate_limited_with_a_4xx_still_gets_the_retry_hint():
    result, posts = _clone(lambda r: httpx.Response(400, json={"error": {"code": 4, "message": "limit"}}))
    assert result.isError is True and "Retry with backoff" in result.content[0].text


def test_metas_per_object_write_limit_isnt_retried_and_says_when_to_try():
    """ADS-100: 613/4841018 ("1 calls per 30 seconds") failed again inside our backoff live."""
    body = {"error": {"code": 613, "error_subcode": 4841018, "message": "(#613) Calls to this API have exceeded the concurrent request rate limit of 1 calls per 30 seconds."}}
    run, calls = _graph(lambda r: httpx.Response(400, json=body))
    result = asyncio.run(run("POST"))
    assert len(calls) == 1
    assert result["error"]["write_outcome"] == "not_applied" and "30 seconds" in result["error"]["next_step"]


def test_other_613s_are_still_retried():
    replies = iter([httpx.Response(400, json={"error": {"code": 613}}), httpx.Response(200, json={"id": "c1"})])
    run, calls = _graph(lambda r: next(replies))
    assert asyncio.run(run("POST")) == {"id": "c1"} and len(calls) == 2


def _delete(handler):
    calls = []

    def counted(request):
        calls.append(request)
        return handler(request)

    def client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(*args, transport=httpx.MockTransport(counted), **kwargs)

    async def run():
        with patch.object(graph_client.httpx, "AsyncClient", side_effect=client_factory), patch.object(
            graph_client.asyncio, "sleep", new_callable=AsyncMock
        ):
            return await graph_client.make_api_request("120000000000001", "t", {}, method="DELETE")

    return asyncio.run(run()), calls


_LIMIT_613 = {"error": {"code": 613, "error_subcode": 4841018, "message": "1 calls per 30 seconds"}}


def test_a_delete_retried_after_a_timeout_never_says_not_applied():
    """Errata's Low on ADS-100: the first DELETE may have landed before the retry was refused."""
    replies = iter(["timeout", httpx.Response(400, json=_LIMIT_613)])

    def handler(request):
        reply = next(replies)
        if reply == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return reply

    result, calls = _delete(handler)
    assert len(calls) == 2
    assert result["error"]["write_outcome"] == "unknown" and "may have been applied" in result["error"]["next_step"]


def test_a_delete_refused_at_once_is_still_not_applied():
    result, calls = _delete(lambda r: httpx.Response(400, json=_LIMIT_613))
    assert len(calls) == 1 and result["error"]["write_outcome"] == "not_applied"


def test_a_delete_whose_connection_never_opened_isnt_ambiguous():
    replies = iter(["refused", httpx.Response(400, json=_LIMIT_613)])

    def handler(request):
        reply = next(replies)
        if reply == "refused":
            raise httpx.ConnectError("refused", request=request)
        return reply

    result, _ = _delete(handler)
    assert result["error"]["write_outcome"] == "not_applied"


def test_a_delete_retried_after_a_5xx_never_says_not_applied():
    """Errata's Low on #39: the 5xx-then-613 path, not only a timeout."""
    replies = iter([httpx.Response(500, json={"error": {"code": 1}}), httpx.Response(400, json=_LIMIT_613)])
    result, calls = _delete(lambda r: next(replies))
    assert len(calls) == 2 and result["error"]["write_outcome"] == "unknown"


def test_a_delete_that_times_out_on_every_try_says_what_became_of_it():
    """Errata's Low on #39: the last retry's failure carried no write_outcome."""
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    result, calls = _delete(timeout)
    assert len(calls) > 1 and result["error"]["write_outcome"] == "unknown"


def test_a_pool_timeout_is_not_sent():
    """Errata's Low on #39: PoolTimeout happens before the request leaves."""
    replies = iter(["pool", httpx.Response(400, json=_LIMIT_613)])

    def handler(request):
        reply = next(replies)
        if reply == "pool":
            raise httpx.PoolTimeout("no connection free")
        return reply

    result, _ = _delete(handler)
    assert result["error"]["write_outcome"] == "not_applied"
