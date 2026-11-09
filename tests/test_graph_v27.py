# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only
"""ADS-41 part 2: Graph v27's new defaults and refusals."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import ad_tools, graph_constants
from armavita_meta_ads_mcp.core.audience_tools import create_lookalike_audience


def test_default_is_v27_and_graph_major_reads_it():
    assert graph_constants.META_GRAPH_API_VERSION == "v27.0" or "META_GRAPH_API_VERSION" in __import__("os").environ
    assert graph_constants.graph_major("v27.0") == 27 and graph_constants.graph_major("x") == 0


# --- Product showcase: never bolted on unless asked -------------------------------------


def test_v27_creative_opts_out_of_product_showcase_and_keeps_other_dof_settings():
    payload = {"degrees_of_freedom_spec": {"creative_features_spec": {"standard_enhancements": {"enroll_status": "OPT_OUT"}}}}
    with patch.object(ad_tools, "graph_major", return_value=27):
        assert ad_tools._opt_out_of_product_showcase(payload) == "OPT_OUT"
    features = payload["degrees_of_freedom_spec"]["creative_features_spec"]
    assert features["product_extensions"] == {"enroll_status": "OPT_OUT"}
    assert features["standard_enhancements"] == {"enroll_status": "OPT_OUT"}


def test_a_caller_who_chose_product_showcase_is_left_alone():
    payload = {"degrees_of_freedom_spec": {"creative_features_spec": {"product_extensions": {"enroll_status": "OPT_IN"}}}}
    with patch.object(ad_tools, "graph_major", return_value=27):
        assert ad_tools._opt_out_of_product_showcase(payload) is None
    assert payload["degrees_of_freedom_spec"]["creative_features_spec"]["product_extensions"] == {"enroll_status": "OPT_IN"}


def test_before_v27_nothing_is_added():
    payload = {}
    with patch.object(ad_tools, "graph_major", return_value=25):
        assert ad_tools._opt_out_of_product_showcase(payload) is None
    assert payload == {}


# --- Lookalikes: location comes from the ad set ---------------------------------------


async def _lookalike(spec, major=27):
    with patch("armavita_meta_ads_mcp.core.audience_tools.make_api_request", new_callable=AsyncMock) as api, patch(
        "armavita_meta_ads_mcp.core.audience_tools.graph_major", return_value=major
    ):
        api.return_value = {"id": "lal_1"}
        raw = await create_lookalike_audience(ad_account_id="act_1", name="L", origin_audience_id="1",
                                              lookalike_spec=spec, meta_access_token="t")
    return json.loads(raw), api


@pytest.mark.asyncio
async def test_v27_lookalike_drops_country_and_says_where_location_comes_from():
    payload, api = await _lookalike({"country": "US", "ratio": 0.01, "type": "custom_ratio"})
    sent = json.loads(api.call_args.args[2]["lookalike_spec"])
    assert sent == {"ratio": 0.01, "type": "custom_ratio"}
    assert "country" in payload["_warning"] and "geo_locations" in payload["_warning"]


@pytest.mark.asyncio
async def test_v27_lookalike_with_only_a_location_is_refused_and_nothing_sent():
    payload, api = await _lookalike({"location_spec": {"geo_locations": {"countries": ["US"]}}})
    assert "Nothing was created" in payload["error"]
    api.assert_not_called()


@pytest.mark.asyncio
async def test_before_v27_lookalike_spec_is_sent_as_given():
    _, api = await _lookalike({"country": "US", "ratio": 0.01}, major=25)
    assert json.loads(api.call_args.args[2]["lookalike_spec"]) == {"country": "US", "ratio": 0.01}
