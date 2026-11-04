# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-107: create_ad_creative can declare AI-generated media (Graph's self_ai_disclosure)."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.ad_tools import create_ad_creative, update_ad_creative

_API = "armavita_meta_ads_mcp.core.ad_tools.make_api_request"


async def _create(**kw):
    with patch(_API, new_callable=AsyncMock) as api:
        api.side_effect = [{"id": "creative_1"}, {"id": "creative_1"}]
        raw = await create_ad_creative(
            ad_account_id="act_123", ad_image_hash="hash_1", facebook_page_id="123456",
            link_url="https://example.com", primary_text="Copy", meta_access_token="token", **kw,
        )
    return json.loads(raw), api


@pytest.mark.asyncio
@pytest.mark.parametrize("given, sent", [("OPT_IN", "OPT_IN"), ("OPT_OUT", "OPT_OUT"), (" opt_in ", "OPT_IN")])
async def test_the_disclosure_is_sent_and_echoed(given, sent):
    out, api = await _create(self_ai_disclosure=given)
    assert api.call_args_list[0].args[2]["self_ai_disclosure"] == sent
    assert out["self_ai_disclosure_sent"] == sent


@pytest.mark.asyncio
async def test_left_out_nothing_is_declared():
    out, api = await _create()
    assert "self_ai_disclosure" not in api.call_args_list[0].args[2]
    assert out["self_ai_disclosure_sent"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["yes", "true", "AI", "OPTIN", "1"])
async def test_a_bad_value_is_refused_before_anything_is_created(given):
    out, api = await _create(self_ai_disclosure=given)
    assert "OPT_IN" in out["error"] and "Nothing was created" in out["error"]
    api.assert_not_called()


def test_the_docs_say_it_is_the_advertisers_call_and_set_once():
    create_doc = create_ad_creative.__doc__ or ""
    assert "advertiser's decision" in create_doc and "never set or guess it" in create_doc
    assert "no update can change it" in create_doc
    assert "self_ai_disclosure" in (update_ad_creative.__doc__ or "")
