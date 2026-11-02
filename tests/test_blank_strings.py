# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-61: a blank string from a client that fills unused fields is "not given", never a real
value Graph would apply (an enum or ID sent as "", a value rule set detached by "  ")."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import ad_tools, adset_tools, campaign_tools
from armavita_meta_ads_mcp.core.mcp_runtime import _import_tool_modules, mcp_server

_import_tool_modules()


def _call(tool: str, arguments: dict):
    """Call a tool the way an MCP client does, through FastMCP's argument handling."""
    return asyncio.run(mcp_server.call_tool(tool, {"meta_access_token": "token", **arguments}))


def _posts(mock: AsyncMock) -> list:
    return [c.args[2] for c in mock.call_args_list if c.kwargs.get("method") == "POST"]


def _text(result) -> str:
    return json.dumps(result, default=str)


BLANKS = ["", "   "]


@pytest.mark.parametrize("blank", BLANKS)
def test_a_budget_edit_with_blank_strings_everywhere_sends_only_the_budget(blank):
    """The strict-schema client: every unused string field arrives as "" (or spaces)."""
    padded = {k: blank for k in (
        "status", "bid_strategy", "optimization_goal", "destination_type", "dsa_beneficiary",
        "dsa_payor", "start_time", "end_time",
    )}
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: {"daily_budget": "1000"} if k.get("method") != "POST" else {"success": True}
        _call("update_ad_set", {"ad_set_id": "as1", "daily_budget": 1500, **padded})
    assert _posts(api) == [{"daily_budget": "1500"}]


@pytest.mark.parametrize("blank", BLANKS)
def test_a_padded_budget_edit_never_detaches_the_value_rule_set(blank):
    """The card's headline case (Errata on #28): the padded client sends value_rule_set_id "" too."""
    padded = {k: blank for k in ("status", "end_time", "bid_strategy", "value_rule_set_id")}
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: {"daily_budget": "1000"} if k.get("method") != "POST" else {"success": True}
        _call("update_ad_set", {"ad_set_id": "as1", "daily_budget": 1500, **padded})
    assert _posts(api) == [{"daily_budget": "1500"}]


def test_detaching_takes_the_explicit_flag():
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: {"success": True}
        out = _call("update_ad_set", {"ad_set_id": "as1", "detach_value_rule_set": True})
        assert _posts(api) == [{"value_rule_set_id": ""}] and "detached" in _text(out)
        both = _call("update_ad_set", {"ad_set_id": "as1", "detach_value_rule_set": True, "value_rule_set_id": "123"})
    assert "not both" in _text(both) and len(_posts(api)) == 1


@pytest.mark.parametrize("blank", BLANKS)
def test_campaign_update_ignores_blank_enums_and_names(blank):
    padded = {k: blank for k in ("name", "status", "bid_strategy", "objective")}
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: {"daily_budget": "1000"} if k.get("method") != "POST" else {"success": True}
        _call("update_campaign", {"campaign_id": "c1", "daily_budget": 2000, **padded})
    assert _posts(api) == [{"daily_budget": "2000"}]


def test_campaign_create_doesnt_send_blank_enums():
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"id": "c1"}
        _call("create_campaign", {
            "ad_account_id": "act_1", "name": "Books", "objective": "OUTCOME_TRAFFIC", "daily_budget": 1000,
            "bid_strategy": "", "buying_type": " ", "smart_promotion_type": "",
        })
    sent = _posts(api)[0]
    assert "bid_strategy" not in sent and "buying_type" not in sent and "smart_promotion_type" not in sent


def test_update_ad_with_a_blank_creative_id_doesnt_send_one():
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        _call("update_ad", {"ad_id": "ad1", "status": "PAUSED", "ad_creative_id": ""})
    assert _posts(api) == [{"status": "PAUSED"}]


def test_only_optional_arguments_of_write_tools_are_cleaned():
    """Read tools, required arguments and arguments whose blank means something keep what was sent."""
    clean = mcp_server._without_blank_optionals
    assert clean("list_campaigns", {"ad_account_id": "act_1", "status_filter": ""}) == {"ad_account_id": "act_1", "status_filter": ""}
    assert clean("update_ad_set", {"ad_set_id": "", "status": " ", "value_rule_set_id": ""}) == {"ad_set_id": "", "value_rule_set_id": ""}


def test_a_dynamic_creative_spec_edit_doesnt_replace_the_feed():
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        _call("update_ad_creative", {"ad_creative_id": "cr1", "dynamic_creative_spec": {"x": 1}})
    sent = _posts(api)[0]
    assert "asset_feed_spec" not in sent and sent["dynamic_creative_spec"] == {"x": 1}


def test_ad_formats_still_sets_the_feed():
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        _call("update_ad_creative", {"ad_creative_id": "cr1", "ad_formats": ["SINGLE_IMAGE"]})
    assert _posts(api)[0]["asset_feed_spec"] == {"ad_formats": ["SINGLE_IMAGE"]}


def test_a_write_tool_without_an_annotation_is_still_cleaned():
    tool = mcp_server._tool_manager.get_tool("update_ad_set")
    saved = tool.annotations
    try:
        tool.annotations = None
        assert mcp_server._without_blank_optionals("update_ad_set", {"ad_set_id": "as1", "status": ""}) == {"ad_set_id": "as1"}
    finally:
        tool.annotations = saved


@pytest.mark.parametrize("current", [{"lifetime_budget": "90000"}, {"id": "as1"}, {"error": {"code": 17}}])
def test_clear_end_time_is_refused_without_a_daily_budget(current):
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: current if k.get("method") != "POST" else {"success": True}
        out = _call("update_ad_set", {"ad_set_id": "as1", "clear_end_time": True})
    assert "needs an ad set with a daily budget" in _text(out) and _posts(api) == []


def test_clear_end_time_sends_graphs_ongoing_value():
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = lambda *a, **k: {"daily_budget": "1000"} if k.get("method") != "POST" else {"success": True}
        _call("update_ad_set", {"ad_set_id": "as1", "clear_end_time": True})
        assert _posts(api) == [{"end_time": "0"}]
        refused = _call("update_ad_set", {"ad_set_id": "as1", "clear_end_time": True, "end_time": "2027-01-01T00:00:00Z"})
    assert "not both" in _text(refused) and len(_posts(api)) == 1
