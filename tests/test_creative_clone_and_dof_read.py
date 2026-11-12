# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only
"""ADS-158: Meta has no /copies edge for creatives; read_ad_creative shows degrees_of_freedom_spec."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.ad_tools import read_ad_creative
from armavita_meta_ads_mcp.core.duplication_tools import clone_ad_creative


@pytest.mark.asyncio
async def test_clone_ad_creative_sends_nothing_and_says_what_to_do():
    with patch("armavita_meta_ads_mcp.core.duplication_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await clone_ad_creative(ad_creative_id="111", meta_access_token="token")
    payload = json.loads(raw)
    assert payload["success"] is False and payload["error"] == "unsupported_by_meta"
    assert "create_ad_creative" in payload["message"] and "no request was sent" in payload["message"]
    mock_api.assert_not_called()


async def _read(dof_reply):
    async def api(endpoint, token, params=None, method="GET"):
        fields = (params or {}).get("fields", "")
        if fields == "degrees_of_freedom_spec":
            if isinstance(dof_reply, Exception):
                raise dof_reply
            return dof_reply
        if fields == "dynamic_creative_spec":
            return {"id": "111"}
        if fields == "self_ai_disclosure":
            return {"id": "111"}
        return {"id": "111", "name": "c"}

    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", side_effect=api):
        return json.loads(await read_ad_creative(ad_creative_id="111", meta_access_token="token"))


@pytest.mark.asyncio
async def test_read_ad_creative_returns_the_product_showcase_setting():
    spec = {"creative_features_spec": {"product_extensions": {"enroll_status": "OPT_OUT"}}}
    payload = await _read({"id": "111", "degrees_of_freedom_spec": spec})
    assert payload["degrees_of_freedom_spec"] == spec and payload["degrees_of_freedom_read"] == "ok"


@pytest.mark.asyncio
async def test_a_failed_degrees_of_freedom_read_does_not_break_the_read():
    payload = await _read(RuntimeError("field unknown on this version"))
    assert payload["name"] == "c" and "degrees_of_freedom_spec" not in payload
    assert payload["degrees_of_freedom_read"] == "unavailable"


@pytest.mark.asyncio
async def test_an_error_reply_is_unavailable_and_nothing_stored_is_ok():
    assert (await _read({"error": {"message": "nope"}}))["degrees_of_freedom_read"] == "unavailable"
    nothing = await _read({"id": "111"})
    assert nothing["degrees_of_freedom_read"] == "ok" and "degrees_of_freedom_spec" not in nothing
