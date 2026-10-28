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
