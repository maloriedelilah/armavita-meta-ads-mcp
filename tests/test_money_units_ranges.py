# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-60: budget and bid units are stated, amounts are range-checked, conflicting budget
arguments are refused, big jumps need confirming, and budget schedules get a sane window."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import ad_tools, adset_tools, budget_schedule_tools, campaign_tools
from armavita_meta_ads_mcp.core.ad_tools import create_ad, update_ad
from armavita_meta_ads_mcp.core.adset_tools import create_ad_set, update_ad_set
from armavita_meta_ads_mcp.core.budget_schedule_tools import create_campaign_budget_schedule
from armavita_meta_ads_mcp.core.campaign_tools import create_campaign, update_campaign


def _out(raw: str) -> dict:
    payload = json.loads(raw)
    if isinstance(payload.get("data"), str):
        return json.loads(payload["data"])
    return payload


def _error(raw: str) -> str:
    err = _out(raw).get("error")
    return err if isinstance(err, str) else json.dumps(err)


def _posts(mock: AsyncMock) -> list:
    return [c for c in mock.call_args_list if c.kwargs.get("method") == "POST"]


BAD_AMOUNTS = [0, -500, True, 10**13]


# --- the units are stated on every money parameter ---------------------------------------

@pytest.mark.parametrize(
    "tool",
    [create_campaign, update_campaign, create_ad_set, update_ad_set, create_ad, update_ad, create_campaign_budget_schedule],
)
def test_every_money_tool_says_minor_units_and_jpy(tool):
    doc = tool.__doc__ or ""
    assert "minor currency units" in doc and "JPY" in doc and "read_ad_account" in doc


# --- ranges --------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("value", BAD_AMOUNTS)
async def test_campaign_budgets_out_of_range_are_refused(value):
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        created = await create_campaign(
            ad_account_id="act_1", name="c", objective="OUTCOME_TRAFFIC", meta_access_token="t", daily_budget=value
        )
        updated = await update_campaign(campaign_id="c1", meta_access_token="t", lifetime_budget=value)
        capped = await update_campaign(campaign_id="c1", meta_access_token="t", spend_cap=value)
    assert "daily_budget" in _error(created) and "lifetime_budget" in _error(updated) and "spend_cap" in _error(capped)
    api.assert_not_called()


@pytest.mark.asyncio
async def test_spend_cap_may_be_removed_with_graphs_no_cap_value():
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        out = _out(await update_campaign(campaign_id="c1", meta_access_token="t", spend_cap=922337203685478))
    assert "error" not in out and _posts(api)[0].args[2]["spend_cap"] == "922337203685478"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", BAD_AMOUNTS)
async def test_ad_set_budgets_and_bids_out_of_range_are_refused(value):
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        created = await create_ad_set(
            ad_account_id="act_1", campaign_id="c1", name="a", optimization_goal="LINK_CLICKS",
            billing_event="IMPRESSIONS", targeting={"geo_locations": {"countries": ["US"]}},
            meta_access_token="t", daily_budget=value,
        )
        updated = await update_ad_set(ad_set_id="as1", meta_access_token="t", bid_amount=value)
    assert "daily_budget" in _error(created) and "bid_amount" in _error(updated)
    api.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", BAD_AMOUNTS)
async def test_ad_bids_out_of_range_are_refused(value):
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        created = await create_ad(
            ad_account_id="act_1", name="a", ad_set_id="as1", ad_creative_id="cr1", meta_access_token="t", bid_amount=value
        )
        updated = await update_ad(ad_id="ad1", meta_access_token="t", bid_amount=value)
    assert "bid_amount" in _error(created) and "bid_amount" in _error(updated)
    api.assert_not_called()


# --- conflicting arguments -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_ad_set_level_budgets_with_a_budget_is_refused_not_silently_cleared():
    """The old code sent daily_budget="" (clearing it) and dropped the budget the caller gave."""
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        out = await update_campaign(
            campaign_id="c1", meta_access_token="t", use_ad_set_level_budgets=True, daily_budget=5000
        )
        created = await create_campaign(
            ad_account_id="act_1", name="c", objective="OUTCOME_TRAFFIC", meta_access_token="t",
            use_ad_set_level_budgets=True, lifetime_budget=5000,
        )
    assert "use_ad_set_level_budgets" in _error(out) and "use_ad_set_level_budgets" in _error(created)
    api.assert_not_called()


@pytest.mark.asyncio
async def test_daily_and_lifetime_together_are_refused():
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as capi, patch.object(
        adset_tools, "make_api_request", new_callable=AsyncMock
    ) as aapi:
        c = await update_campaign(campaign_id="c1", meta_access_token="t", daily_budget=1000, lifetime_budget=9000)
        a = await update_ad_set(ad_set_id="as1", meta_access_token="t", daily_budget=1000, lifetime_budget=9000)
    assert "not both" in _error(c) and "not both" in _error(a)
    capi.assert_not_called()
    aapi.assert_not_called()


# --- big jumps -------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("module, call", [
    (campaign_tools, lambda **kw: update_campaign(campaign_id="c1", meta_access_token="t", **kw)),
    (adset_tools, lambda **kw: update_ad_set(ad_set_id="as1", meta_access_token="t", **kw)),
])
async def test_raising_a_budget_more_than_3x_needs_confirming(module, call):
    current = {"id": "x", "daily_budget": "1000"}
    with patch.object(module, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: current if k.get("method") != "POST" else {"success": True}
        refused = await call(daily_budget=100000)  # the "times 100" mistake
        assert "confirm_budget_increase" in _error(refused) and _posts(api) == []
        within = _out(await call(daily_budget=3000))
        assert within.get("budget_increase_check") == "passed" and _posts(api)[-1].args[2]["daily_budget"] == "3000"
        confirmed = _out(await call(daily_budget=100000, confirm_budget_increase=True))
        assert "error" not in confirmed and _posts(api)[-1].args[2]["daily_budget"] == "100000"


@pytest.mark.asyncio
async def test_a_failed_budget_read_doesnt_block_the_change_but_says_so():
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: {"error": {"code": 2}} if k.get("method") != "POST" else {"success": True}
        out = _out(await update_ad_set(ad_set_id="as1", meta_access_token="t", daily_budget=5000))
    assert out["budget_increase_check"].startswith("skipped")


@pytest.mark.asyncio
async def test_lowering_a_budget_needs_no_confirming():
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: {"daily_budget": "50000"} if k.get("method") != "POST" else {"success": True}
        out = _out(await update_campaign(campaign_id="c1", meta_access_token="t", daily_budget=1000))
    assert "error" not in out


# --- budget schedules ------------------------------------------------------------------------

def _schedule(**kw):
    now = int(time.time())
    args = dict(campaign_id="c1", budget_value=2000, budget_value_type="ABSOLUTE",
                time_start=now + 3600, time_end=now + 3600 + 4 * 3600, meta_access_token="t")
    args.update(kw)
    return create_campaign_budget_schedule(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize("change, words", [
    (lambda now: {"time_end": now + 3600 - 60}, "after time_start"),
    (lambda now: {"time_end": now + 3600 + 3600}, "at least 3 hours"),
    (lambda now: {"time_start": now - 86400, "time_end": now + 86400}, "in the past"),
    (lambda now: {"time_start": (now + 3600) * 1000, "time_end": (now + 9 * 3600) * 1000}, "milliseconds"),
    (lambda now: {"budget_value": 0}, "budget_value"),
    (lambda now: {"budget_value": -5}, "budget_value"),
])
async def test_bad_schedules_are_refused(change, words):
    now = int(time.time())
    with patch.object(budget_schedule_tools, "make_api_request", new_callable=AsyncMock) as api:
        raw = await _schedule(**change(now))
    assert words in _error(raw)
    api.assert_not_called()


@pytest.mark.asyncio
async def test_a_good_schedule_is_sent():
    with patch.object(budget_schedule_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"id": "sched_1"}
        out = _out(await _schedule())
    assert out == {"id": "sched_1"} and api.call_args.args[0] == "c1/budget_schedules"
