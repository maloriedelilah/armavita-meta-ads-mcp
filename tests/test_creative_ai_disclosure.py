# SPDX-License-Identifier: AGPL-3.0-only

"""AI disclosure (Graph's self_ai_disclosure) on creatives.

ADS-107 let create_ad_creative send it. ADS-213 (Nov 20): Meta accepts OPT_IN through the
API and then drops it (the Ads Manager AI box stayed off for creatives made by this server
and by Graph Explorer, and on for Meta's own connector), and the field reads back blank for
every creative. So OPT_IN is refused, and read_ad_creative no longer reports a value.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.ad_tools import create_ad_creative, read_ad_creative, update_ad_creative

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
@pytest.mark.parametrize("given", ["OPT_IN", " opt_in ", "Opt_In"])
async def test_opt_in_is_refused_before_anything_reaches_meta(given):
    out, api = await _create(self_ai_disclosure=given)
    api.assert_not_called()
    assert "Nothing was created" in out["error"]
    # Points somewhere that works: Meta's connector, or the AI box in Ads Manager.
    assert "Meta's own connector" in out["error"] and "AI box" in out["error"] and "Ads Manager" in out["error"]
    assert out["provided"] == given


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["OPT_OUT", " opt_out "])
async def test_opt_out_is_still_sent_and_echoed(given):
    out, api = await _create(self_ai_disclosure=given)
    assert api.call_args_list[0].args[2]["self_ai_disclosure"] == "OPT_OUT"
    assert out["self_ai_disclosure_sent"] == "OPT_OUT"


@pytest.mark.asyncio
async def test_left_out_nothing_is_sent():
    out, api = await _create()
    assert "self_ai_disclosure" not in api.call_args_list[0].args[2]
    assert out["self_ai_disclosure_sent"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("given", ["yes", "true", "AI", "OPTIN", "1"])
async def test_a_bad_value_is_refused_before_anything_is_created(given):
    out, api = await _create(self_ai_disclosure=given)
    assert "OPT_OUT" in out["error"] and "Nothing was created" in out["error"]
    api.assert_not_called()


def test_the_docs_no_longer_promise_a_declaration():
    create_doc = create_ad_creative.__doc__ or ""
    assert "can't declare AI-made media" in create_doc
    assert "refused before anything is created" in create_doc
    assert "changes nothing" in create_doc  # OPT_OUT
    assert "advertiser's decision" in create_doc and "never guess" in create_doc
    assert '"OPT_IN" declares' not in create_doc
    update_doc = update_ad_creative.__doc__ or ""
    assert "self_ai_disclosure" in update_doc and "AI box" in update_doc


async def _read(disclosure_reply):
    calls = []

    async def api(endpoint, token, params, method="GET"):
        fields = params.get("fields", "")
        calls.append(fields)
        if fields == "self_ai_disclosure":
            return disclosure_reply
        if fields == "dynamic_creative_spec":
            return {"id": "c1"}
        return {"id": "c1", "name": "Creative"}

    with patch(_API, side_effect=api):
        out = json.loads(await read_ad_creative(ad_creative_id="120000000000001", meta_access_token="token"))
    return out, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"id": "c1"}, {"id": "c1", "self_ai_disclosure": "OPT_IN"}])
async def test_read_ad_creative_reports_no_disclosure_value_only_the_note(reply):
    """A blank value used to be read as "off" or as a declaration that failed; it held an
    autopilot ad set Nov 18-20. Now there is no value to misread, whatever Graph says."""
    out, calls = await _read(reply)
    assert out["name"] == "Creative"
    assert "self_ai_disclosure" not in out and "self_ai_disclosure_read" not in out
    assert "self_ai_disclosure" not in calls  # not even asked for
    note = out["ai_disclosure_note"]
    assert "blank for every creative" in note and "Ads Manager" in note and 'not "off"' in note


def test_read_ad_creative_description_says_the_disclosure_always_reads_blank():
    doc = read_ad_creative.__doc__ or ""
    assert "blank for" in doc and "every" in doc and "AI box" in doc
    assert "as Meta stored it" not in doc and "never as OPT_OUT" not in doc
