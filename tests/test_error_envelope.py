# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.targeting_tools import estimate_audience_size, search_interests


@pytest.mark.asyncio
async def test_search_interests_empty_query_returns_the_error_unwrapped():
    """ADS-59: the error stays at the top level (where the MCP layer makes it isError)."""
    raw = await search_interests(query="", meta_access_token="token")
    payload = json.loads(raw)
    assert "data" not in payload and "error" in payload


@pytest.mark.asyncio
async def test_estimate_audience_size_missing_params_returns_the_error_unwrapped():
    raw = await estimate_audience_size(meta_access_token="token")
    payload = json.loads(raw)
    assert "data" not in payload and "error" in payload