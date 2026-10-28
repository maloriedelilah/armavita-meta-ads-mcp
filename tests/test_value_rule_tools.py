# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.value_rule_tools import list_value_rule_sets, read_value_rule_set

_PATCH_TARGET = "armavita_meta_ads_mcp.core.value_rule_tools.make_api_request"


def _tool_error(raw: str) -> str:
    # meta_api_tool wraps tool-level validation errors in a {"data": "<json>"} envelope.
    return json.loads(json.loads(raw)["data"])["error"]


@pytest.mark.asyncio
async def test_list_value_rule_sets_calls_account_edge_with_default_fields():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"data": [{"id": "rs_1", "name": "Women 25-64"}]}

        raw = await list_value_rule_sets(ad_account_id="123", meta_access_token="token")

    assert json.loads(raw)["data"][0]["id"] == "rs_1"
    endpoint, token, params = mock_api.call_args.args
    assert endpoint == "act_123/value_rule_set"
    assert token == "token"
    assert params["fields"] == "id,name,status,product_type,is_default_setting"
    assert params["page_size"] == 25
    assert "status" not in params and "product_type" not in params


@pytest.mark.asyncio
async def test_list_value_rule_sets_passes_filters_and_cursor():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"data": []}

        await list_value_rule_sets(
            ad_account_id="act_123",
            meta_access_token="token",
            status="active",
            product_type="audience",
            page_size=10,
            page_cursor="abc",
        )

    endpoint, _, params = mock_api.call_args.args
    assert endpoint == "act_123/value_rule_set"
    assert params["status"] == "ACTIVE"
    assert params["product_type"] == "AUDIENCE"
    assert params["page_size"] == 10
    assert params["page_cursor"] == "abc"


@pytest.mark.asyncio
async def test_list_value_rule_sets_rejects_bad_filters_without_calling_api():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        raw = await list_value_rule_sets(ad_account_id="act_123", meta_access_token="token", status="LIVE")

    assert "Invalid status" in _tool_error(raw)
    mock_api.assert_not_called()


@pytest.mark.asyncio
async def test_list_value_rule_sets_requires_account():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        raw = await list_value_rule_sets(ad_account_id="", meta_access_token="token")

    assert _tool_error(raw) == "ad_account_id is required"
    mock_api.assert_not_called()


@pytest.mark.asyncio
async def test_read_value_rule_set_includes_rules_by_default():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [
            {"id": "rs_1", "name": "Women 25-64", "status": "ACTIVE"},
            {"data": [{"id": "rule_1"}]},
        ]

        raw = await read_value_rule_set(rule_set_id="rs_1", meta_access_token="token")

    payload = json.loads(raw)
    assert payload["name"] == "Women 25-64"
    assert payload["rules"] == {"data": [{"id": "rule_1"}]}
    assert mock_api.call_args_list[0].args[0] == "rs_1"
    assert mock_api.call_args_list[1].args[0] == "rs_1/rules"


@pytest.mark.asyncio
async def test_read_value_rule_set_can_skip_rules():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"id": "rs_1"}

        raw = await read_value_rule_set(rule_set_id="rs_1", meta_access_token="token", include_rules=False)

    assert json.loads(raw) == {"id": "rs_1"}
    assert mock_api.call_count == 1


@pytest.mark.asyncio
async def test_read_value_rule_set_stops_after_an_error():
    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"error": {"message": "Unsupported get request"}}

        raw = await read_value_rule_set(rule_set_id="rs_missing", meta_access_token="token")

    assert json.loads(raw)["error"]["message"] == "Unsupported get request"
    assert mock_api.call_count == 1
