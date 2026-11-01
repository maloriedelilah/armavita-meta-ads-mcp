# SPDX-License-Identifier: AGPL-3.0-only
"""ADS-56: no default audience on create; update can't drop the audience by accident."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.adset_tools import create_ad_set, update_ad_set

_API = "armavita_meta_ads_mcp.core.adset_tools.make_api_request"
_PARENT = "armavita_meta_ads_mcp.core.adset_tools._parent_campaign_bid_strategy"

CURRENT = {
    "age_min": 35,
    "age_max": 64,
    "genders": [2],
    "geo_locations": {"countries": ["GB"], "location_types": ["home", "recent"]},
    "custom_audiences": [{"id": "111"}],
    "targeting_automation": {"advantage_audience": 0},
}


def _out(raw: str) -> dict:
    """meta_api_tool wraps some replies as {"data": "<json>"}; unwrap them."""
    out = json.loads(raw)
    if set(out) == {"data"} and isinstance(out["data"], str):
        out = json.loads(out["data"])
    return out


def _create(**over):
    args = dict(
        ad_account_id="act_1", campaign_id="cmp_1", name="Set", optimization_goal="LINK_CLICKS",
        billing_event="IMPRESSIONS", daily_budget=1000, meta_access_token="token",
    )
    args.update(over)
    return create_ad_set(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize("targeting", [None, {}, {"age_min": 25}, {"geo_locations": {}}])
async def test_create_refuses_missing_or_geo_less_targeting_and_sends_nothing(targeting):
    with patch(_API, new_callable=AsyncMock) as api, patch(_PARENT, new_callable=AsyncMock) as parent:
        parent.return_value = None
        out = _out(await _create(targeting=targeting))
    assert "geo_locations" in json.dumps(out["error"]) and "Nothing was created" in json.dumps(out["error"])
    assert api.call_count == 0


@pytest.mark.asyncio
async def test_create_sends_exactly_the_targeting_given_with_advantage_off_and_echoes_it():
    with patch(_API, new_callable=AsyncMock) as api, patch(_PARENT, new_callable=AsyncMock) as parent:
        parent.return_value = None
        api.return_value = {"id": "new"}
        out = _out(await _create(targeting={"geo_locations": {"countries": ["GB"]}, "age_min": 25}))
    sent = json.loads(api.call_args.args[2]["targeting"])
    assert sent == {"geo_locations": {"countries": ["GB"]}, "age_min": 25, "targeting_automation": {"advantage_audience": 0}}
    assert "US" not in json.dumps(sent)
    assert out["targeting_sent"] == sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "targeting, words",
    [
        ({}, "targeting is empty"),
        ({"age_min": 25}, "no geo_locations"),
        ({"geo_locations": {"countries": ["GB"]}}, "targeting_automation"),
    ],
)
async def test_update_refuses_a_replacement_that_would_drop_the_audience(targeting, words):
    with patch(_API, new_callable=AsyncMock) as api:
        out = _out(await update_ad_set(ad_set_id="as_1", meta_access_token="token", targeting=targeting))
    assert words in out["error"] and "Nothing was changed" in out["error"]
    assert api.call_count == 0


@pytest.mark.asyncio
async def test_targeting_patch_changes_only_the_given_keys():
    calls = []

    async def api(endpoint, token, params, method="GET"):
        calls.append((endpoint, dict(params), method))
        if method == "GET":
            return {"id": "as_1", "targeting": json.loads(json.dumps(CURRENT))}
        return {"success": True}

    with patch(_API, side_effect=api):
        out = _out(await update_ad_set(
            ad_set_id="as_1", meta_access_token="token", targeting_patch={"age_min": 25, "genders": None},
        ))
    (get, post) = calls
    assert get[0] == "as_1" and get[1] == {"fields": "targeting"} and get[2] == "GET"
    sent = json.loads(post[1]["targeting"])
    expected = dict(CURRENT, age_min=25)
    expected.pop("genders")
    assert sent == expected  # geo, location_types, custom audiences and Advantage+ state all kept
    assert out["targeting_sent"] == expected


@pytest.mark.asyncio
async def test_targeting_patch_that_removes_geo_is_refused():
    async def api(endpoint, token, params, method="GET"):
        return {"id": "as_1", "targeting": dict(CURRENT)} if method == "GET" else {"success": True}

    with patch(_API, side_effect=api) as mock:
        out = _out(await update_ad_set(ad_set_id="as_1", meta_access_token="token", targeting_patch={"geo_locations": None}))
    assert "no geo_locations" in out["error"] and "Nothing was changed" in out["error"]
    assert all(c.kwargs.get("method", "GET") == "GET" for c in mock.call_args_list)


@pytest.mark.asyncio
async def test_targeting_patch_refuses_when_the_current_spec_cant_be_read():
    async def api(endpoint, token, params, method="GET"):
        return {"error": {"message": "HTTP Error: 400"}}

    with patch(_API, side_effect=api) as mock:
        out = _out(await update_ad_set(ad_set_id="as_1", meta_access_token="token", targeting_patch={"age_min": 25}))
    assert "Couldn't read" in out["error"] and mock.call_count == 1


@pytest.mark.asyncio
async def test_targeting_and_targeting_patch_together_are_refused():
    with patch(_API, new_callable=AsyncMock) as api:
        out = _out(await update_ad_set(
            ad_set_id="as_1", meta_access_token="token",
            targeting=dict(CURRENT), targeting_patch={"age_min": 25},
        ))
    assert "not both" in out["error"] and api.call_count == 0


@pytest.mark.asyncio
async def test_a_complete_replacement_is_sent_and_echoed():
    with patch(_API, new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        out = _out(await update_ad_set(ad_set_id="as_1", meta_access_token="token", targeting=dict(CURRENT)))
    assert json.loads(api.call_args.args[2]["targeting"]) == CURRENT
    assert out["targeting_sent"] == CURRENT
