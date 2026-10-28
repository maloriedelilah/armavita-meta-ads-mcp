# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.adset_tools import create_ad_set, list_ad_sets, read_ad_set, update_ad_set

_PATCH_TARGET = "armavita_meta_ads_mcp.core.adset_tools.make_api_request"


@pytest.mark.asyncio
async def test_update_ad_set_attaches_value_rule_set():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}

        raw = await update_ad_set(ad_set_id="adset_1", meta_access_token="token", value_rule_set_id=" 1439455934496080 ")

    assert json.loads(raw)["success"] is True
    endpoint, _, params = mock_api.call_args.args
    assert endpoint == "adset_1"
    assert params == {"value_rule_set_id": "1439455934496080"}
    assert mock_api.call_args.kwargs["method"] == "POST"


@pytest.mark.asyncio
async def test_update_ad_set_empty_value_rule_set_detaches():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}

        raw = await update_ad_set(ad_set_id="adset_1", meta_access_token="token", value_rule_set_id="")

    assert json.loads(raw)["success"] is True
    assert mock_api.call_args.args[2] == {"value_rule_set_id": ""}


@pytest.mark.asyncio
async def test_update_ad_set_leaves_value_rule_set_alone_when_unset():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}

        await update_ad_set(ad_set_id="adset_1", meta_access_token="token", daily_budget=1000)

    params = mock_api.call_args.args[2]
    assert "value_rule_set_id" not in params
    assert params["daily_budget"] == "1000"


@pytest.mark.asyncio
async def test_create_ad_set_sends_value_rule_set_id():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api, patch(
        "armavita_meta_ads_mcp.core.adset_tools._parent_campaign_bid_strategy", new_callable=AsyncMock
    ) as mock_parent:
        mock_parent.return_value = None
        mock_api.return_value = {"id": "new_adset"}

        await create_ad_set(
            ad_account_id="act_1",
            campaign_id="cmp_1",
            name="New Adset",
            optimization_goal="LINK_CLICKS",
            billing_event="IMPRESSIONS",
            daily_budget=1000,
            meta_access_token="token",
            value_rule_set_id="1439455934496080",
        )

    assert mock_api.call_args.args[2]["value_rule_set_id"] == "1439455934496080"


@pytest.mark.asyncio
async def test_create_ad_set_omits_blank_value_rule_set_id():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api, patch(
        "armavita_meta_ads_mcp.core.adset_tools._parent_campaign_bid_strategy", new_callable=AsyncMock
    ) as mock_parent:
        mock_parent.return_value = None
        mock_api.return_value = {"id": "new_adset"}

        await create_ad_set(
            ad_account_id="act_1",
            campaign_id="cmp_1",
            name="New Adset",
            optimization_goal="LINK_CLICKS",
            billing_event="IMPRESSIONS",
            daily_budget=1000,
            meta_access_token="token",
            value_rule_set_id="",
        )

    assert "value_rule_set_id" not in mock_api.call_args.args[2]


@pytest.mark.asyncio
async def test_read_and_list_ad_sets_request_value_rule_set_id():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"id": "adset_1", "value_rule_set_id": "1439455934496080"}

        await read_ad_set(ad_set_id="adset_1", meta_access_token="token")
        read_fields = mock_api.call_args.args[2]["fields"]

        mock_api.return_value = {"data": []}
        await list_ad_sets(ad_account_id="act_1", meta_access_token="token")
        list_fields = mock_api.call_args.args[2]["fields"]

    assert "value_rule_set_id" in read_fields.split(",")
    assert "value_rule_set_id" in list_fields.split(",")


@pytest.mark.asyncio
async def test_update_ad_set_null_is_refused_and_nothing_is_sent():
    # ADS-38 review M1: null must never detach (clients send null for unused fields).
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        raw = await update_ad_set(ad_set_id="adset_1", meta_access_token="token", daily_budget=1500, value_rule_set_id=None)

    mock_api.assert_not_called()
    reply = json.loads(raw)
    # meta_api_tool wraps a string error reply as {"data": "<json>"}.
    if "data" in reply:
        reply = json.loads(reply["data"])
    error = reply["error"]
    assert "value_rule_set_id is null" in error and 'Pass "" to detach' in error


@pytest.mark.asyncio
async def test_update_ad_set_reports_attach_and_detach_in_result():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        # A fresh reply per call: the tool adds keys to the dict Graph returns.
        mock_api.side_effect = lambda *args, **kwargs: {"success": True}
        attached = json.loads(await update_ad_set(ad_set_id="adset_1", meta_access_token="token", value_rule_set_id="123"))
        detached = json.loads(await update_ad_set(ad_set_id="adset_1", meta_access_token="token", value_rule_set_id=""))
        budget_only = json.loads(await update_ad_set(ad_set_id="adset_1", meta_access_token="token", daily_budget=1000))

    assert attached["value_rule_set"] == {"attached": "123"}
    assert detached["value_rule_set"] == {"detached": True}
    assert "value_rule_set" not in budget_only


@pytest.mark.asyncio
async def test_update_ad_set_keep_leaves_value_rule_set_alone():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}

        await update_ad_set(ad_set_id="adset_1", meta_access_token="token", daily_budget=1000, value_rule_set_id="keep")

    params = mock_api.call_args.args[2]
    assert "value_rule_set_id" not in params
    assert params["daily_budget"] == "1000"


def _call_through_mcp(arguments):
    """Call update_ad_set the way an MCP client does, through FastMCP's argument handling."""
    import asyncio

    from armavita_meta_ads_mcp.core.mcp_runtime import _import_tool_modules, mcp_server

    _import_tool_modules()
    return asyncio.run(mcp_server.call_tool("update_ad_set", arguments))


def test_update_ad_set_over_mcp_null_refused_omitted_keeps_id_attaches():
    # The original bug lived here: with a None default, FastMCP gave the tool None
    # for both an omitted argument and an explicit JSON null.
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}
        result = _call_through_mcp({"ad_set_id": "adset_1", "meta_access_token": "token", "value_rule_set_id": None})
    mock_api.assert_not_called()
    assert "value_rule_set_id is null" in str(result)

    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}
        _call_through_mcp({"ad_set_id": "adset_1", "meta_access_token": "token", "daily_budget": 1000})
    params = mock_api.call_args.args[2]
    assert "value_rule_set_id" not in params
    assert params["daily_budget"] == "1000"

    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}
        _call_through_mcp({"ad_set_id": "adset_1", "meta_access_token": "token", "value_rule_set_id": "1439455934496080"})
    assert mock_api.call_args.args[2] == {"value_rule_set_id": "1439455934496080"}


def test_update_ad_set_over_mcp_all_optionals_null_is_refused_not_detached():
    # Errata's repro: a client that fills every optional with null plus a budget change.
    import inspect

    args = {name: None for name in inspect.signature(update_ad_set).parameters if name != "ad_set_id"}
    args.update({"ad_set_id": "adset_1", "meta_access_token": "token", "daily_budget": 1500})
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}
        result = _call_through_mcp(args)

    mock_api.assert_not_called()
    assert "value_rule_set_id is null" in str(result)


@pytest.mark.asyncio
async def test_null_refusal_names_keep_for_strict_clients():
    # ADS-66: a strict-schema client can't leave the field out, so the message offers "keep".
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        raw = await update_ad_set(ad_set_id="1", meta_access_token="token", value_rule_set_id=None)

    mock_api.assert_not_called()
    reply = json.loads(raw)
    if "data" in reply:
        reply = json.loads(reply["data"])
    assert '(or pass "keep")' in reply["error"]
