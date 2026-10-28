# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP mode never exposes the host: no shared research cache (ADS-48), no local
files (ADS-50), no fetching caller-chosen URLs (ADS-51). stdio is unchanged."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import ad_tools, http_mode, media_helpers, mcp_runtime
from armavita_meta_ads_mcp.core.ad_tools import upload_ad_image_asset, upload_ad_video_asset
from armavita_meta_ads_mcp.core.mcp_runtime import mcp_server


@pytest.fixture
def http_on():
    saved_tools = dict(mcp_server._tool_manager._tools)
    mcp_runtime.configure_http("127.0.0.1", 8765, [])
    yield
    http_mode.disable()
    mcp_server._tool_manager._tools.clear()
    mcp_server._tool_manager._tools.update(saved_tools)


def _error(raw):
    reply = json.loads(raw)
    if "data" in reply and isinstance(reply["data"], str):
        reply = json.loads(reply["data"])
    error = reply["error"]
    return error["message"] if isinstance(error, dict) else error


def test_research_and_export_tools_are_not_offered_in_http_mode(http_on):
    names = {tool.name for tool in mcp_server._tool_manager.list_tools()}
    assert not names & {"search_web_content", "read_web_content", "export_ad_image_file"}
    assert "list_ad_accounts" in names


def test_stdio_still_offers_them():
    mcp_runtime._import_tool_modules()
    names = {tool.name for tool in mcp_server._tool_manager.list_tools()}
    assert {"search_web_content", "read_web_content", "export_ad_image_file"} <= names


@pytest.mark.asyncio
async def test_image_upload_never_fetches_a_url_in_http_mode(http_on):
    with patch.object(ad_tools, "try_multiple_download_methods", new_callable=AsyncMock) as fetch, patch.object(
        ad_tools, "make_api_request", new_callable=AsyncMock
    ) as mock_api:
        raw = await upload_ad_image_asset(
            ad_account_id="act_1", image_source_url="http://192.168.1.1/admin.png", meta_access_token="token"
        )

    fetch.assert_not_called()
    mock_api.assert_not_called()
    assert "does not fetch image_source_url" in _error(raw)


@pytest.mark.asyncio
async def test_image_upload_never_reads_the_host_filesystem_in_http_mode(http_on):
    isfile = MagicMock(return_value=True)
    with patch.object(ad_tools.os.path, "isfile", isfile), patch.object(
        ad_tools, "make_api_request", new_callable=AsyncMock
    ) as mock_api:
        mock_api.return_value = {"images": {}}
        await upload_ad_image_asset(ad_account_id="act_1", image_file_path="/etc/passwd", meta_access_token="token")

    # The value is treated as base64 and sent to Meta as is; the disk is never consulted.
    isfile.assert_not_called()


@pytest.mark.asyncio
async def test_video_upload_refuses_local_paths_in_http_mode(http_on):
    isfile = MagicMock(return_value=True)
    with patch.object(ad_tools.os.path, "isfile", isfile), patch.object(
        ad_tools, "make_api_request", new_callable=AsyncMock
    ) as mock_api:
        raw = await upload_ad_video_asset(ad_account_id="act_1", video_file_path="/etc/passwd", meta_access_token="token")

    isfile.assert_not_called()
    mock_api.assert_not_called()
    assert "local file paths are not accepted" in _error(raw)


@pytest.mark.parametrize(
    "url, allowed",
    [
        ("https://scontent.xx.fbcdn.net/v/t45/abc.jpg?_nc_sid=1", True),
        ("https://www.facebook.com/ads/image/?d=x", True),
        ("https://scontent.cdninstagram.com/v/abc.jpg", True),
        ("http://scontent.xx.fbcdn.net/abc.jpg", False),  # not https
        ("https://scontent.xx.fbcdn.net:8443/abc.jpg", False),  # odd port
        ("https://fbcdn.net.evil.example/abc.jpg", False),
        ("https://evilfbcdn.net/abc.jpg", False),
        ("https://127.0.0.1/abc.jpg", False),
        ("https://[::ffff:127.0.0.1]/abc.jpg", False),
        ("https://169.254.169.254/latest/meta-data/", False),
        ("file:///etc/passwd", False),
    ],
)
def test_http_mode_fetches_only_meta_image_hosts(http_on, url, allowed):
    assert media_helpers.http_mode_fetch_allowed(url) is allowed


def test_stdio_fetch_is_unchanged():
    assert media_helpers.http_mode_fetch_allowed("http://192.168.1.1/x.png") is True


@pytest.mark.asyncio
async def test_download_refuses_internal_urls_without_any_request(http_on):
    with patch.object(media_helpers.httpx, "AsyncClient") as client:
        assert await media_helpers.download_image("http://192.168.1.1/x.png") is None
        assert await media_helpers.try_multiple_download_methods("https://10.0.0.5/x.png") is None

    client.assert_not_called()


@pytest.mark.asyncio
async def test_download_from_meta_hosts_does_not_follow_redirects_in_http_mode(http_on):
    seen = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return MagicMock(content=b"img", raise_for_status=lambda: None)

    with patch.object(media_helpers.httpx, "AsyncClient", _Client):
        assert await media_helpers.download_image("https://scontent.xx.fbcdn.net/a.jpg") == b"img"

    assert seen.get("follow_redirects") is False
