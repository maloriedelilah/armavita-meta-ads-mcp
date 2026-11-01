# SPDX-License-Identifier: AGPL-3.0-only
"""ADS-57: a clone either does what it was asked or refuses before copying anything."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import armavita_meta_ads_mcp.core  # noqa: F401  (registers every tool)
from armavita_meta_ads_mcp.core.duplication_tools import clone_ad_set, clone_campaign
from armavita_meta_ads_mcp.core.mcp_runtime import mcp_server
from mcp.server.fastmcp.exceptions import ToolError

_API = "armavita_meta_ads_mcp.core.duplication_tools.make_api_request"
_PREFLIGHT = "armavita_meta_ads_mcp.core.duplication_tools._run_v25_duplication_preflight"


def _payload(raw: str) -> dict:
    out = json.loads(raw)
    if set(out) == {"data"} and isinstance(out["data"], str):
        out = json.loads(out["data"])
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, args, dropped",
    [
        ("clone_ad_set", {"ad_set_id": "1", "new_daily_budget": 20}, "new_daily_budget"),
        ("clone_ad_set", {"ad_set_id": "1", "new_targeting": {"geo_locations": {"countries": ["GB"]}}}, "new_targeting"),
        ("clone_campaign", {"campaign_id": "1", "new_daily_budget": 5}, "new_daily_budget"),
        ("clone_campaign", {"campaign_id": "1", "copy_schedule": True}, "copy_schedule"),
        ("clone_campaign", {"campaign_id": "1", "include_creatives": False}, "include_creatives"),
        ("clone_ad", {"ad_id": "1", "new_creative_name": "X"}, "new_creative_name"),
        ("clone_ad_creative", {"ad_creative_id": "1", "new_headline": "Read it"}, "new_headline"),
        ("update_ad_set", {"ad_set_id": "1", "daily_budgett": 500}, "daily_budgett"),  # a typo is caught too
    ],
)
async def test_an_option_a_tool_doesnt_take_is_refused_before_anything_is_sent(tool, args, dropped):
    with patch(_API, new_callable=AsyncMock) as api, patch(
        "armavita_meta_ads_mcp.core.adset_tools.make_api_request", new_callable=AsyncMock
    ) as api2:
        with pytest.raises(ToolError) as refused:
            await mcp_server.call_tool(tool, {**args, "meta_access_token": "EAA" + "x" * 30})
    assert dropped in str(refused.value) and "Nothing was done" in str(refused.value)
    assert api.call_count == 0 and api2.call_count == 0


@pytest.mark.asyncio
async def test_every_tool_accepts_its_own_parameters():
    tools = await mcp_server.list_tools()
    for t in tools:
        props = (t.inputSchema or {}).get("properties", {})
        assert mcp_server._unknown_arguments(t.name, {k: None for k in props}) == set(), t.name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, deep",
    [({}, True), ({"include_ad_sets": False, "include_ads": False}, False), ({"include_ad_sets": False}, False)],
)
async def test_campaign_include_ad_sets_maps_to_deep_copy(kwargs, deep):
    with patch(_API, new_callable=AsyncMock) as api, patch(_PREFLIGHT, new_callable=AsyncMock) as pre:
        pre.return_value = None
        api.return_value = {"copied_campaign_id": "c2"}
        out = _payload(await clone_campaign(campaign_id="c1", meta_access_token="token", **kwargs))
    assert out["success"] is True and out["warnings"] == []  # no warning on every call any more
    assert api.call_args.args[2]["deep_copy"] is deep


@pytest.mark.asyncio
async def test_campaign_ad_sets_without_their_ads_is_refused_not_faked():
    with patch(_API, new_callable=AsyncMock) as api, patch(_PREFLIGHT, new_callable=AsyncMock) as pre:
        pre.return_value = None
        with pytest.raises(Exception) as refused:
            await clone_campaign(campaign_id="c1", meta_access_token="token", include_ads=False)
    assert "unsupported_option" in str(refused.value) and "Nothing was copied" in str(refused.value)
    assert api.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("include_ads, deep", [(True, True), (False, False)])
async def test_ad_set_include_ads_maps_to_deep_copy_without_contradicting_warnings(include_ads, deep):
    with patch(_API, new_callable=AsyncMock) as api, patch(_PREFLIGHT, new_callable=AsyncMock) as pre:
        pre.return_value = None
        api.return_value = {"copied_adset_id": "s2"}
        out = _payload(await clone_ad_set(ad_set_id="s1", meta_access_token="token", include_ads=include_ads))
    assert api.call_args.args[2]["deep_copy"] is deep and out["warnings"] == []


@pytest.mark.asyncio
async def test_a_deep_copy_reports_every_object_it_made():
    reply = {
        "copied_campaign_id": "c2",
        "ad_object_ids": [
            {"ad_object_type": "campaign", "source_id": "c1", "copied_id": "c2"},
            {"ad_object_type": "ad_set", "source_id": "s1", "copied_id": "s2"},
            {"ad_object_type": "ad", "source_id": "a1", "copied_id": "a2"},
        ],
    }
    with patch(_API, new_callable=AsyncMock) as api, patch(_PREFLIGHT, new_callable=AsyncMock) as pre:
        pre.return_value = None
        api.return_value = reply
        out = _payload(await clone_campaign(campaign_id="c1", meta_access_token="token"))
    assert out["new_id"] == "c2"
    assert out["copied_objects"] == [
        {"type": "campaign", "source_id": "c1", "new_id": "c2"},
        {"type": "ad_set", "source_id": "s1", "new_id": "s2"},
        {"type": "ad", "source_id": "a1", "new_id": "a2"},
    ]
