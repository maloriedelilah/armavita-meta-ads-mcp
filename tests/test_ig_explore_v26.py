# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only
"""ADS-41: from Graph v26 Meta refuses IG `explore` (2490589); `explore_home` is still allowed."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import adset_tools
from armavita_meta_ads_mcp.core.adset_tools import create_ad_set, update_ad_set

MOD = "armavita_meta_ads_mcp.core.adset_tools"


async def _create(targeting, version="v26.0"):
    with patch(f"{MOD}.make_api_request", new_callable=AsyncMock) as api, patch(
        f"{MOD}._parent_campaign_bid_strategy", new_callable=AsyncMock, return_value=None
    ), patch(f"{MOD}.META_GRAPH_API_VERSION", version):
        api.return_value = {"success": True, "id": "new_adset"}
        raw = await create_ad_set(
            ad_account_id="act_1", campaign_id="cmp_1", name="t", optimization_goal="LINK_CLICKS",
            billing_event="IMPRESSIONS", meta_access_token="token",
            targeting={"geo_locations": {"countries": ["US"]}, **targeting},
        )
    return json.loads(raw), api


def _sent(api):
    return json.loads(api.call_args.args[2]["targeting"])


@pytest.mark.asyncio
async def test_v26_drops_explore_keeps_explore_home_and_says_so():
    payload, api = await _create({"publisher_platforms": ["instagram"], "instagram_positions": ["stream", "explore", "explore_home"]})
    assert _sent(api)["instagram_positions"] == ["stream", "explore_home"]
    assert "explore" in payload["_warning"] and "explore_home" in payload["_warning"]


@pytest.mark.asyncio
async def test_v25_sends_explore_unchanged():
    payload, api = await _create({"instagram_positions": ["stream", "explore"]}, version="v25.0")
    assert _sent(api)["instagram_positions"] == ["stream", "explore"]


@pytest.mark.asyncio
async def test_explore_only_takes_instagram_off_rather_than_opening_every_ig_placement():
    _, api = await _create({"publisher_platforms": ["facebook", "instagram"], "facebook_positions": ["feed"], "instagram_positions": ["explore"]})
    sent = _sent(api)
    assert "instagram_positions" not in sent and sent["publisher_platforms"] == ["facebook"]


@pytest.mark.asyncio
async def test_explore_as_the_only_placement_is_refused_and_nothing_is_sent():
    payload, api = await _create({"publisher_platforms": ["instagram"], "instagram_positions": ["explore"]})
    assert "Nothing was changed" in payload["error"]
    api.assert_not_called()


@pytest.mark.asyncio
async def test_a_patch_on_a_live_ad_set_carrying_explore_still_goes_through_on_v26():
    current = {"targeting": {"geo_locations": {"countries": ["US"]}, "targeting_automation": {"advantage_audience": 0},
                             "publisher_platforms": ["instagram"], "instagram_positions": ["stream", "explore", "explore_home"]}}

    async def api(endpoint, token, params=None, method="GET"):
        return current if method == "GET" else {"success": True, "id": "adset_1"}

    with patch(f"{MOD}.make_api_request", side_effect=api) as mock, patch(f"{MOD}.META_GRAPH_API_VERSION", "v26.0"):
        raw = await update_ad_set(ad_set_id="adset_1", meta_access_token="token", targeting_patch={"age_min": 25})
    post = [c for c in mock.call_args_list if c.kwargs.get("method") == "POST" or (len(c.args) > 3 and c.args[3] == "POST")]
    assert post, raw
    sent = json.loads(post[-1].args[2]["targeting"])
    assert sent["instagram_positions"] == ["stream", "explore_home"] and sent["age_min"] == 25
    assert "explore" in json.loads(raw)["_warning"]


def test_graph_major():
    assert adset_tools._graph_major("v26.0") == 26 and adset_tools._graph_major("v25.0") == 25
    assert adset_tools._graph_major("bogus") == 0
