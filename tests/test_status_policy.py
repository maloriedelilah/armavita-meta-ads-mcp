# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-54: everything is created PAUSED; activation only through updates; nothing deleted or archived."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import status_policy
from armavita_meta_ads_mcp.core.ad_tools import create_ad, update_ad
from armavita_meta_ads_mcp.core.adset_tools import create_ad_set, update_ad_set
from armavita_meta_ads_mcp.core.campaign_tools import create_campaign, update_campaign
from armavita_meta_ads_mcp.core.catalog_tools import batch_products
from armavita_meta_ads_mcp.core.duplication_tools import clone_ad, clone_ad_set, clone_campaign


def _error(raw):
    reply = json.loads(raw)
    if "data" in reply and isinstance(reply["data"], str):
        reply = json.loads(reply["data"])
    error = reply["error"]
    return error["message"] if isinstance(error, dict) else error


def _sent_statuses(mock_api):
    return [call.args[2].get("status") for call in mock_api.call_args_list if isinstance(call.args[2], dict) and "status" in call.args[2]]


@pytest.mark.parametrize("value", [None, "", "  ", "PAUSED", "paused"])
def test_create_status_defaults_to_paused(value):
    assert status_policy.create_status(value) == ("PAUSED", None)


@pytest.mark.parametrize("value", ["ACTIVE", "active", "DELETED", "ARCHIVED", "INHERITED_FROM_SOURCE", "LIVE"])
def test_create_status_refuses_anything_but_paused(value):
    sent, error = status_policy.create_status(value)
    assert sent is None and "always created PAUSED" in error


@pytest.mark.parametrize("value, expected", [(None, None), ("ACTIVE", "ACTIVE"), (" active ", "ACTIVE"), ("paused", "PAUSED")])
def test_update_status_allows_active_and_paused(value, expected):
    assert status_policy.update_status(value) == (expected, None)


@pytest.mark.parametrize("value", ["DELETED", "deleted", "ARCHIVED", "Archived"])
def test_update_status_never_deletes_or_archives(value):
    sent, error = status_policy.update_status(value)
    assert sent is None and "never deletes or archives" in error


@pytest.mark.parametrize("value", ["", "   "])
def test_update_status_empty_means_unchanged(value):
    # ADS-66: strict-schema clients send "" for fields they aren't using.
    assert status_policy.update_status(value) == (None, None)


@pytest.mark.parametrize("value", ["LIVE", "INHERITED_FROM_SOURCE"])
def test_update_status_refuses_other_values(value):
    sent, error = status_policy.update_status(value)
    assert sent is None and "ACTIVE or PAUSED" in error


@pytest.mark.asyncio
async def test_create_ad_with_empty_status_is_created_paused():
    # Errata's repro: status="" used to replace the PAUSED default.
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"id": "1"}
        await create_ad(ad_account_id="act_1", name="n", ad_set_id="2", ad_creative_id="3", status="", meta_access_token="token")

    assert _sent_statuses(mock_api) == ["PAUSED"]


@pytest.mark.asyncio
async def test_create_ad_active_is_refused():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await create_ad(ad_account_id="act_1", name="n", ad_set_id="2", ad_creative_id="3", status="ACTIVE", meta_access_token="token")

    mock_api.assert_not_called()
    assert "always created PAUSED" in _error(raw)


@pytest.mark.asyncio
async def test_create_campaign_and_ad_set_active_are_refused():
    with patch("armavita_meta_ads_mcp.core.campaign_tools.make_api_request", new_callable=AsyncMock) as campaign_api:
        campaign_raw = await create_campaign(
            ad_account_id="act_1", name="n", objective="OUTCOME_TRAFFIC", status="ACTIVE", meta_access_token="token"
        )
    with patch("armavita_meta_ads_mcp.core.adset_tools.make_api_request", new_callable=AsyncMock) as adset_api:
        adset_raw = await create_ad_set(
            ad_account_id="act_1",
            campaign_id="2",
            name="n",
            optimization_goal="LINK_CLICKS",
            billing_event="IMPRESSIONS",
            status="active",
            meta_access_token="token",
        )

    campaign_api.assert_not_called()
    adset_api.assert_not_called()
    assert "always created PAUSED" in _error(campaign_raw)
    assert "always created PAUSED" in _error(adset_raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["DELETED", "archived"])
async def test_updates_never_delete_or_archive(value):
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as ad_api, patch(
        "armavita_meta_ads_mcp.core.adset_tools.make_api_request", new_callable=AsyncMock
    ) as adset_api, patch("armavita_meta_ads_mcp.core.campaign_tools.make_api_request", new_callable=AsyncMock) as campaign_api:
        replies = [
            await update_ad(ad_id="1", status=value, meta_access_token="token"),
            await update_ad_set(ad_set_id="1", status=value, meta_access_token="token"),
            await update_campaign(campaign_id="1", status=value, meta_access_token="token"),
        ]

    ad_api.assert_not_called()
    adset_api.assert_not_called()
    campaign_api.assert_not_called()
    assert all("never deletes or archives" in _error(raw) for raw in replies)


@pytest.mark.asyncio
async def test_update_can_activate_after_review():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.return_value = {"success": True}
        await update_ad(ad_id="1", status="active", meta_access_token="token")

    assert mock_api.call_args.args[2]["status"] == "ACTIVE"


@pytest.mark.asyncio
@pytest.mark.parametrize("clone, kwargs", [(clone_campaign, {"campaign_id": "1"}), (clone_ad_set, {"ad_set_id": "1"}), (clone_ad, {"ad_id": "1"})])
async def test_clones_are_paused_and_active_is_refused(clone, kwargs):
    with patch("armavita_meta_ads_mcp.core.duplication_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = lambda *a, **k: {"copied_id": "9"}
        refused = await clone(new_status="ACTIVE", meta_access_token="token", **kwargs)
        assert mock_api.call_count == 0
        await clone(new_status=None, meta_access_token="token", **kwargs)

    assert "always created PAUSED" in _error(refused)
    assert mock_api.call_args.args[2]["status_option"] == "PAUSED"


@pytest.mark.asyncio
async def test_batch_products_refuses_deletes():
    with patch("armavita_meta_ads_mcp.core.catalog_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await batch_products(
            product_catalog_id="1",
            requests=[{"method": "UPDATE", "retailer_id": "A"}, {"method": "delete", "retailer_id": "B"}],
            meta_access_token="token",
        )

    mock_api.assert_not_called()
    assert "never deletes or archives" in _error(raw)
