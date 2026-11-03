# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-106: list_insights takes Graph's time_increment, so daily rows are one call."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.insight_tools import list_insights

_API = "armavita_meta_ads_mcp.core.insight_tools.make_api_request"


async def _call(**kw):
    with patch(_API, new_callable=AsyncMock) as api:
        api.return_value = {"data": []}
        out = json.loads(await list_insights(object_id="act_1", meta_access_token="t", **kw))
    return out, api


@pytest.mark.asyncio
@pytest.mark.parametrize("given, sent", [
    (1, "1"), (7, "7"), (90, "90"), ("1", "1"), (" Monthly ", "monthly"), ("all_days", "all_days"),
])
async def test_a_good_time_increment_is_sent(given, sent):
    out, api = await _call(date_range="last_7d", level="adset", time_increment=given)
    assert "error" not in out and api.call_args.args[2]["time_increment"] == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [0, 91, -1, True, 1.5, "daily", "1d", [1], {"x": 1}])
async def test_a_bad_time_increment_is_refused_before_any_call(given):
    out, api = await _call(time_increment=given)
    assert out["error"] == "invalid_time_increment" and "1 to 90" in out["message"]
    api.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("given", [None, ""])
async def test_no_time_increment_sends_none(given):
    out, api = await _call(time_increment=given)
    assert "time_increment" not in api.call_args.args[2]


def test_the_tool_says_how_to_get_daily_rows():
    doc = list_insights.__doc__ or ""
    assert "time_increment" in doc and "one row per day" in doc and "next" in doc
