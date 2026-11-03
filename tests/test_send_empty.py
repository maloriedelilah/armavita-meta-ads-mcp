# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-96: empty lists, objects and text from a padding client are "not given"; send_empty is the
one way to really clear a field."""

import asyncio
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import ad_tools, adset_tools, audience_tools, campaign_tools, conversion_tools
from armavita_meta_ads_mcp.core.mcp_runtime import _import_tool_modules, mcp_server

_import_tool_modules()


def _call(tool: str, arguments: dict):
    try:
        return asyncio.run(mcp_server.call_tool(tool, {"meta_access_token": "token", **arguments}))
    except ToolError as refused:
        return str(refused)


def _posts(mock: AsyncMock) -> list:
    return [c.args[2] for c in mock.call_args_list if c.kwargs.get("method") == "POST"]


def _text(result) -> str:
    if isinstance(result, tuple):  # (content blocks, structured) from FastMCP
        return "".join(getattr(block, "text", "") for block in result[0])
    return str(result)


def _padded(tool: str, keep: dict) -> dict:
    """Every optional argument of `tool` filled the way a padding client fills it."""
    fn = mcp_server._tool_manager.get_tool(tool).fn
    args = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is not None or name in ("meta_access_token", "send_empty"):
            continue
        annotation = str(param.annotation)
        args[name] = [] if "List" in annotation else {} if "Dict" in annotation else "" if "str" in annotation else None
    args = {k: v for k, v in args.items() if v is not None}
    args.update(keep)
    return args


def _reads_then(post_reply, read=None):
    read = read if read is not None else {"daily_budget": "1000"}
    return lambda *a, **k: dict(post_reply) if k.get("method") == "POST" else dict(read)


def test_a_padded_ad_set_budget_edit_sends_only_the_budget():
    """The card's repro: [] and {} for frequency caps, attribution, bid constraints, promoted object."""
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = _reads_then({"success": True})
        _call("update_ad_set", _padded("update_ad_set", {"ad_set_id": "as1", "daily_budget": 1500}))
    assert _posts(api) == [{"daily_budget": "1500"}]


def test_a_padded_campaign_edit_sends_only_the_change():
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = _reads_then({"success": True})
        _call("update_campaign", _padded("update_campaign", {"campaign_id": "c1", "name": "Spring push"}))
    assert _posts(api) == [{"name": "Spring push"}]


@pytest.mark.parametrize("field, empty, sent", [
    ("frequency_control_specs", [], []),
    ("attribution_spec", [], "[]"),
    ("bid_constraints", {}, "{}"),
])
def test_send_empty_really_clears_an_ad_set_field(field, empty, sent):
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        out = _call("update_ad_set", {"ad_set_id": "as1", field: empty, "send_empty": [field]})
    assert _posts(api) == [{field: sent}]
    assert json.loads(_text(out))["sent_empty"] == [field]


def test_clearing_on_the_other_tools():
    with patch.object(campaign_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        _call("update_campaign", {"campaign_id": "c1", "special_ad_categories": [], "send_empty": ["special_ad_categories"]})
        assert _posts(api) == [{"special_ad_categories": "[]"}]
    with patch.object(audience_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        _call("update_custom_audience", {"custom_audience_id": "1", "description": "", "send_empty": ["description"]})
        assert _posts(api) == [{"description": ""}]
    with patch.object(conversion_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        _call("update_custom_conversion", {"custom_conversion_id": "1", "description": "", "send_empty": ["description"]})
        assert _posts(api) == [{"description": ""}]
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        _call("update_ad", {"ad_id": "1", "tracking_specs": [], "send_empty": ["tracking_specs"]})
        assert _posts(api) == [{"tracking_specs": "[]"}]


def test_an_empty_description_without_send_empty_changes_nothing():
    with patch.object(audience_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.return_value = {"success": True}
        out = _call("update_custom_audience", {"custom_audience_id": "1", "description": "", "name": "Readers"})
    assert _posts(api) == [{"name": "Readers"}] and "sent_empty" not in _text(out)


@pytest.mark.parametrize("arguments, words", [
    ({"targeting": {}, "send_empty": ["targeting"]}, "can't clear"),
    ({"frequency_control_specs": [{"event": "IMPRESSIONS"}], "send_empty": ["frequency_control_specs"]}, "empty ("),
    ({"send_empty": ["attribution_spec"]}, "empty ("),
    ({"daily_budget": 1500, "send_empty": "frequency_control_specs"}, "send_empty"),
])
def test_send_empty_mistakes_are_refused_before_anything_is_sent(arguments, words):
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = _reads_then({"success": True})
        out = _call("update_ad_set", {"ad_set_id": "as1", **arguments})
    assert words in _text(out) and _posts(api) == []


def test_a_padded_send_empty_names_nothing():
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = _reads_then({"success": True})
        _call("update_ad_set", {"ad_set_id": "as1", "daily_budget": 1500, "frequency_control_specs": [], "send_empty": []})
    assert _posts(api) == [{"daily_budget": "1500"}]


def test_tools_without_clearable_fields_dont_take_send_empty():
    out = _call("update_ad_creative", {"ad_creative_id": "1", "name": "x", "send_empty": ["name"]})
    assert "doesn't take send_empty" in _text(out)


def test_a_padded_read_only_tool_gets_no_blank_values():
    """Errata's Low on #30: create_report is read-only, but a padded "" used to reach it."""
    cleaned = mcp_server._without_blank_optionals("create_report", {"ad_account_id": "act_1", "report_name": "", "campaign_ids": []})
    assert cleaned == {"ad_account_id": "act_1"}


def test_clearing_end_time_under_a_campaign_budget_is_allowed():
    """ADS-96 Low: an ad set under a campaign daily budget has none of its own; the campaign's counts."""
    read = {"id": "as1", "campaign": {"daily_budget": "5000", "id": "c1"}}
    with patch.object(adset_tools, "make_api_request", new_callable=AsyncMock) as api:
        api.side_effect = _reads_then({"success": True}, read)
        _call("update_ad_set", {"ad_set_id": "as1", "end_time": "", "send_empty": ["end_time"]})
    assert _posts(api) == [{"end_time": "0"}]
    reads = [c for c in api.call_args_list if c.kwargs.get("method") != "POST"]
    assert "campaign{daily_budget}" in reads[0].args[2]["fields"]
