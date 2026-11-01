# SPDX-License-Identifier: AGPL-3.0-only
"""ADS-58: a creative runs under the Page you name, or the only Page there is; never a guess."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.ad_tools import create_ad_creative

_API = "armavita_meta_ads_mcp.core.ad_tools.make_api_request"


def _graph(me_accounts, owned=None):
    calls = []

    async def api(endpoint, token, params=None, method="GET"):
        calls.append((endpoint, method, dict(params or {})))
        if endpoint == "me/accounts":
            return {"data": me_accounts}
        if endpoint == "act_555" and method == "GET":
            return {"business": {"id": "b1"}} if owned is not None else {}
        if endpoint == "b1/owned_pages":
            return {"data": owned or []}
        if endpoint.endswith("/adcreatives") and method == "POST":
            return {"id": "cr_1"}
        if endpoint == "cr_1":
            return {"id": "cr_1"}
        return {"data": []}

    return api, calls


def _out(raw):
    out = json.loads(raw)
    if set(out) == {"data"} and isinstance(out["data"], str):
        out = json.loads(out["data"])
    return out


async def _create(api, **over):
    args = dict(ad_account_id="act_555", ad_image_hash="h1", link_url="https://example.com", primary_text="Read it",
                meta_access_token="token")
    args.update(over)
    with patch(_API, side_effect=api):
        return _out(await create_ad_creative(**args))


def _posted_page(calls):
    posts = [c for c in calls if c[0].endswith("/adcreatives") and c[1] == "POST"]
    return posts[0][2]["object_story_spec"]["page_id"] if posts else None


@pytest.mark.asyncio
async def test_with_several_pages_nothing_is_guessed_and_they_are_listed():
    # The audit's case: Malorie's own page first in me/accounts, the client's page owned by the ad account's business.
    api, calls = _graph(
        [{"id": "1001", "name": "Malorie personal page"}, {"id": "1002", "name": "Client B page"}],
        owned=[{"id": "2002", "name": "Client A page"}],
    )
    out = await _create(api)
    assert "won't guess" in out["error"] and "Nothing was created" in out["error"]
    assert {p["facebook_page_id"] for p in out["pages"]} == {"1001", "1002", "2002"}
    assert _posted_page(calls) is None


@pytest.mark.asyncio
async def test_the_only_page_is_used_and_said():
    api, calls = _graph([{"id": "1001", "name": "Only page"}])
    out = await _create(api)
    assert out["success"] is True and out["facebook_page_id_used"] == "1001"
    assert _posted_page(calls) == "1001"


@pytest.mark.asyncio
async def test_a_named_page_is_used_without_looking_any_up():
    api, calls = _graph([{"id": "1001"}, {"id": "1002"}])
    out = await _create(api, facebook_page_id="2002")
    assert out["facebook_page_id_used"] == "2002" and _posted_page(calls) == "2002"
    assert not any(c[0] == "me/accounts" for c in calls)


@pytest.mark.asyncio
async def test_no_page_at_all_is_refused():
    api, calls = _graph([])
    out = await _create(api)
    assert "no Facebook Page could be found" in out["error"] and _posted_page(calls) is None
