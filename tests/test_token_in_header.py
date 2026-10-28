# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-44: the Meta token reaches Graph only in the Authorization header, never in a URL or body."""

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import graph_client

TOKEN = "EAATOKENSENTINEL0123456789"
_RealAsyncClient = httpx.AsyncClient


def _capturing_client(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "1"}]})

    transport = httpx.MockTransport(handler)
    return lambda *args, **kwargs: _RealAsyncClient(*args, transport=transport, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, files",
    [("GET", None), ("POST", None), ("POST", {"source": ("a.png", b"\x89PNG", "image/png")}), ("DELETE", None)],
)
async def test_token_is_only_in_the_authorization_header(method, files):
    seen = []
    with patch.object(graph_client.httpx, "AsyncClient", _capturing_client(seen)):
        result = await graph_client.make_api_request(
            "act_1/campaigns", TOKEN, {"fields": "id,name", "page_size": 5}, method=method, files=files
        )

    assert result == {"data": [{"id": "1"}]}
    assert len(seen) == 1
    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert "access_token" not in str(request.url)
    body = request.content
    assert TOKEN.encode() not in body and b"access_token" not in body
    # Ordinary parameters still arrive where they did.
    assert ("fields=id%2Cname" in str(request.url)) or (b"fields=id%2Cname" in body) or (b'name="fields"' in body)


@pytest.mark.asyncio
async def test_a_token_slipped_into_params_is_dropped():
    seen = []
    with patch.object(graph_client.httpx, "AsyncClient", _capturing_client(seen)):
        await graph_client.make_api_request(
            "act_1/campaigns", TOKEN, {"access_token": "OTHERTOKEN123", "meta_access_token": "THIRDTOKEN123"}, method="GET"
        )

    request = seen[0]
    assert "OTHERTOKEN123" not in str(request.url) and "THIRDTOKEN123" not in str(request.url)
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_request_params_never_carry_a_token():
    params = graph_client._normalize_request_params({"meta_access_token": "X", "Access_Token": "Y", "fields": "id"})
    assert params == {"fields": "id"}
