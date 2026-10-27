# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core.ad_tools import create_ad_creative


@pytest.mark.asyncio
async def test_create_ad_creative_simple_image_payload_uses_story_spec_fields():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_1"}, {"id": "creative_1", "name": "Creative"}]

        raw = await create_ad_creative(
            ad_account_id="act_123",
            ad_image_hash="hash_1",
            facebook_page_id="123456",
            link_url="https://example.com",
            primary_text="Primary copy",
            headline_text="Headline",
            description_text="Description text",
            call_to_action_type="LEARN_MORE",
            lead_form_id="lead_form_1",
            meta_access_token="token",
        )

    payload = json.loads(raw)
    assert payload["success"] is True

    params = mock_api.call_args_list[0].args[2]
    story = params["object_story_spec"]
    assert story["page_id"] == "123456"
    assert story["link_data"]["message"] == "Primary copy"
    assert story["link_data"]["description"] == "Description text"
    assert story["link_data"]["call_to_action"]["value"]["lead_gen_form_id"] == "lead_form_1"


@pytest.mark.asyncio
async def test_create_ad_creative_variant_payload_uses_asset_feed_spec():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_2"}, {"id": "creative_2", "name": "Creative"}]

        raw = await create_ad_creative(
            ad_account_id="act_123",
            facebook_page_id="123456",
            link_url="https://example.com",
            ad_image_hashes=["hash_a", "hash_b"],
            primary_text_variants=["Copy A", "Copy B"],
            headline_variants=["Headline A"],
            description_variants=["Desc A"],
            meta_access_token="token",
        )

    payload = json.loads(raw)
    assert payload["success"] is True

    params = mock_api.call_args_list[0].args[2]
    feed = params["asset_feed_spec"]
    assert feed["images"] == [{"hash": "hash_a"}, {"hash": "hash_b"}]
    assert feed["bodies"] == [{"text": "Copy A"}, {"text": "Copy B"}]
    assert feed["titles"] == [{"text": "Headline A"}]
    assert feed["descriptions"] == [{"text": "Desc A"}]


@pytest.mark.asyncio
async def test_create_ad_creative_carousel_builds_child_attachments():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_3"}, {"id": "creative_3", "name": "Carousel"}]

        raw = await create_ad_creative(
            ad_account_id="act_123",
            facebook_page_id="123456",
            link_url="https://example.com",
            primary_text="Shop now",
            carousel_cards=[
                {"link": "https://example.com/1", "name": "Card 1", "image_hash": "hash_1"},
                {"link": "https://example.com/2", "name": "Card 2", "image_hash": "hash_2"},
            ],
            meta_access_token="token",
        )

    payload = json.loads(raw)
    assert payload["success"] is True

    params = mock_api.call_args_list[0].args[2]
    link_data = params["object_story_spec"]["link_data"]
    assert link_data["ad_formats"] == ["CAROUSEL"]
    assert len(link_data["child_attachments"]) == 2


@pytest.mark.asyncio
async def test_create_ad_creative_catalog_sets_product_set_id():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_4"}, {"id": "creative_4", "name": "Catalog"}]

        raw = await create_ad_creative(
            ad_account_id="act_123",
            facebook_page_id="123456",
            link_url="https://example.com",
            primary_text="Catalog ad",
            product_set_id="set_99",
            meta_access_token="token",
        )

    payload = json.loads(raw)
    assert payload["success"] is True

    params = mock_api.call_args_list[0].args[2]
    assert params["product_set_id"] == "set_99"
    assert "template_data" in params["object_story_spec"]

@pytest.mark.asyncio
async def test_create_ad_creative_image_display_link_sets_caption():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_5"}, {"id": "creative_5", "name": "Creative"}]

        raw = await create_ad_creative(
            ad_account_id="act_123",
            ad_image_hash="hash_1",
            facebook_page_id="123456",
            link_url="https://example.com/product?tag=abc",
            primary_text="Primary copy",
            headline_text="Headline",
            display_link="example.com/shop",
            call_to_action_type="LEARN_MORE",
            meta_access_token="token",
        )

    assert json.loads(raw)["success"] is True
    link_data = mock_api.call_args_list[0].args[2]["object_story_spec"]["link_data"]
    assert link_data["caption"] == "example.com/shop"
    assert link_data["link"] == "https://example.com/product?tag=abc"


@pytest.mark.asyncio
async def test_create_ad_creative_video_carries_description_and_display_link():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_6"}, {"id": "creative_6", "name": "Video"}]

        raw = await create_ad_creative(
            ad_account_id="act_123",
            ad_video_id="video_1",
            thumbnail_url="https://example.com/thumb.jpg",
            facebook_page_id="123456",
            link_url="https://example.com",
            primary_text="Primary copy",
            headline_text="Headline",
            description_text="Description text",
            display_link="example.com/shop",
            call_to_action_type="LEARN_MORE",
            meta_access_token="token",
        )

    assert json.loads(raw)["success"] is True
    video_data = mock_api.call_args_list[0].args[2]["object_story_spec"]["video_data"]
    assert video_data["link_description"] == "Description text"
    assert video_data["call_to_action"]["value"]["link_caption"] == "example.com/shop"
    assert video_data["call_to_action"]["value"]["link"] == "https://example.com"


@pytest.mark.asyncio
async def test_create_ad_creative_omits_optional_fields_when_not_given():
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_7"}, {"id": "creative_7", "name": "Video"}]

        await create_ad_creative(
            ad_account_id="act_123",
            ad_video_id="video_1",
            thumbnail_url="https://example.com/thumb.jpg",
            facebook_page_id="123456",
            link_url="https://example.com",
            primary_text="Primary copy",
            call_to_action_type="LEARN_MORE",
            meta_access_token="token",
        )

    video_data = mock_api.call_args_list[0].args[2]["object_story_spec"]["video_data"]
    assert "link_description" not in video_data
    assert "link_caption" not in video_data["call_to_action"]["value"]


@pytest.mark.asyncio
async def test_create_ad_creative_accepts_long_headline_and_description():
    long_headline = "H" * 120
    long_description = "D" * 1500
    with patch("armavita_meta_ads_mcp.core.ad_tools.make_api_request", new_callable=AsyncMock) as mock_api:
        mock_api.side_effect = [{"id": "creative_8"}, {"id": "creative_8", "name": "Creative"}]

        raw = await create_ad_creative(
            ad_account_id="act_123",
            ad_image_hash="hash_1",
            facebook_page_id="123456",
            link_url="https://example.com",
            primary_text="Primary copy",
            headline_text=long_headline,
            description_text=long_description,
            meta_access_token="token",
        )

    assert json.loads(raw)["success"] is True
    link_data = mock_api.call_args_list[0].args[2]["object_story_spec"]["link_data"]
    assert link_data["name"] == long_headline
    assert link_data["description"] == long_description
