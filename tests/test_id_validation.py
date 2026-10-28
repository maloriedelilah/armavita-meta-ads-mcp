# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-53: caller-supplied IDs can't reshape Graph requests; token fields never come back."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import graph_client, id_validation
from armavita_meta_ads_mcp.core.ad_tools import update_ad
from armavita_meta_ads_mcp.core.audience_tools import manage_custom_audience_users
from armavita_meta_ads_mcp.core.campaign_tools import create_campaign, read_campaign
from armavita_meta_ads_mcp.core.conversion_tools import read_custom_conversion


def _error(raw):
    reply = json.loads(raw)
    if "data" in reply and isinstance(reply["data"], str):
        reply = json.loads(reply["data"])
    error = reply["error"]
    return error["message"] if isinstance(error, dict) else error


@pytest.mark.asyncio
async def test_bid_edit_cannot_activate_an_ad_through_the_id():
    # Errata's repro: this used to POST /321?status=ACTIVE.
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await update_ad(ad_id="321?status=ACTIVE", bid_amount=50, meta_access_token="token")

    mock_api.assert_not_called()
    assert "ad_id must be a plain Meta ID" in _error(raw)


@pytest.mark.asyncio
async def test_remove_users_cannot_delete_the_audience_through_the_id():
    # Errata's repro: "77#" used to turn /77/users into DELETE /77.
    with patch("armavita_meta_ads_mcp.core.audience_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await manage_custom_audience_users(
            custom_audience_id="77#", operation="remove", schema=["EMAIL"], data=[["a@example.com"]], meta_access_token="token"
        )

    mock_api.assert_not_called()
    assert "custom_audience_id must be a plain Meta ID" in _error(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["act_1/campaigns?status=ACTIVE&x=", "act_1/..", "act_abc", "1 2"])
async def test_ad_account_id_must_be_an_account(account):
    with patch("armavita_meta_ads_mcp.core.campaign_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await create_campaign(ad_account_id=account, name="x", objective="OUTCOME_TRAFFIC", meta_access_token="token")

    mock_api.assert_not_called()
    assert "ad_account_id must be an ad account ID" in _error(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("campaign_id", ["../v19.0/me/accounts", "act_1/campaigns", "123/../456", "12 3"])
async def test_path_traversal_and_edge_swaps_are_refused(campaign_id):
    with patch("armavita_meta_ads_mcp.core.campaign_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await read_campaign(campaign_id=campaign_id, meta_access_token="token")

    mock_api.assert_not_called()
    assert "campaign_id must be a plain Meta ID" in _error(raw)


@pytest.mark.asyncio
async def test_fields_cannot_ask_for_page_tokens():
    # Errata's repro: custom_conversion_id="me/accounts" plus fields="name,access_token" returned Page tokens.
    with patch("armavita_meta_ads_mcp.core.conversion_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        raw = await read_custom_conversion(custom_conversion_id="123", fields="name,access_token", meta_access_token="token")

    mock_api.assert_not_called()
    assert "access_token" in _error(raw)


@pytest.mark.parametrize(
    "arguments",
    [
        {"ad_account_id": "act_123"},
        {"ad_account_id": "123"},
        {"meta_user_id": "me"},
        {"campaign_id": "120249692123020625"},
        {"ad_set_id": "adset_1"},
        {"value_rule_set_id": "keep"},
        {"value_rule_set_id": ""},
        {"ad_id": None},
        {"page_ids": ["123", "456"]},
        {"meta_access_token": "EAAB-not-checked-here"},
    ],
)
def test_plain_ids_pass(arguments):
    assert id_validation.argument_problem(arguments) is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"meta_user_id": "me/accounts"},
        {"page_ids": ["123", "456?x=1"]},
        {"targeting": {"note": "access_token=abc"}},
        {"ad_set_id": "456#"},
        {"ad_set_id": "456.1"},
    ],
)
def test_unsafe_arguments_are_refused(arguments):
    assert id_validation.argument_problem(arguments) is not None


@pytest.mark.parametrize(
    "endpoint, ok",
    [
        ("act_1/campaigns", True),
        ("me/adaccounts", True),
        ("120249692123020625", True),
        ("search", True),
        ("321?status=ACTIVE", False),
        ("77#", False),
        ("../me", False),
        ("act_1//campaigns", False),
        ("", False),
        ("act_1/campaigns/", False),
    ],
)
def test_endpoint_backstop(endpoint, ok):
    assert (id_validation.endpoint_problem(endpoint) is None) is ok


@pytest.mark.asyncio
async def test_make_api_request_refuses_unsafe_paths_without_any_http_call():
    with patch.object(graph_client.httpx, "AsyncClient") as client:
        result = await graph_client.make_api_request("321?status=ACTIVE", "token", {"bid_amount": 50}, method="POST")

    client.assert_not_called()
    assert "Refused to call Meta" in result["error"]["message"]


def test_token_fields_are_redacted_from_responses():
    payload = {
        "data": [
            {"id": "1", "name": "Page", "access_token": "EAAPAGETOKEN"},
            {"id": "2", "page_access_token": "EAAOTHER", "nested": {"Access_Token": "EAANESTED"}},
        ],
        "paging": {"next": "https://graph.facebook.com/v25.0/me/accounts?access_token=EAAURL&after=x"},
    }
    cleaned = graph_client._sanitize_response_payload(payload)
    text = json.dumps(cleaned)

    assert "EAAPAGETOKEN" not in text and "EAAOTHER" not in text and "EAANESTED" not in text and "EAAURL" not in text
    assert cleaned["data"][0]["name"] == "Page"
    assert cleaned["data"][0]["access_token"] == "***REDACTED***"


@pytest.mark.parametrize(
    "arguments",
    [
        {"campaign_id": "act_123"},  # update_campaign(campaign_id="act_123", spend_cap=...) hit the account
        {"ad_set_id": "act_1"},
        {"custom_audience_id": "ME"},
        {"page_ids": ["123", "act_9"]},
    ],
)
def test_accounts_and_me_are_refused_where_an_object_id_belongs(arguments):
    # ADS-64
    assert "not an ad account" in id_validation.argument_problem(arguments)


@pytest.mark.parametrize("arguments", [{"ad_account_id": "act_123"}, {"object_id": "act_123"}, {"meta_user_id": "me"}])
def test_accounts_and_me_still_pass_where_they_belong(arguments):
    assert id_validation.argument_problem(arguments) is None
