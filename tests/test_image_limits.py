# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-62: untrusted image bytes are opened, downloaded and uploaded with limits."""

import asyncio
import base64
import io
import json
import struct
import sys
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import EpsImagePlugin
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import ad_tools, image_limits, media_helpers
from armavita_meta_ads_mcp.core.ad_tools import upload_ad_image_asset, upload_ad_video_asset

_RealAsyncClient = httpx.AsyncClient


def _png(width, height):
    image = PILImage.new("RGB", (width, height), (10, 20, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_header_only(width, height):
    """A tiny PNG that claims huge dimensions (a decompression bomb's header)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b"")


def _error(raw):
    reply = json.loads(raw)
    if "data" in reply and isinstance(reply["data"], str):
        reply = json.loads(reply["data"])
    return reply.get("error")


# --- Pillow -------------------------------------------------------------------


def test_ordinary_image_opens_and_converts():
    result = ad_tools._build_image_from_bytes(_png(40, 30))
    assert result.data[:2] == b"\xff\xd8"  # JPEG


@pytest.mark.parametrize("pixels", [(7000, 7000), (13000, 13000)])  # warn band (1x-2x) and error band (>2x)
def test_too_many_pixels_is_refused_before_decoding(pixels):
    with pytest.raises(image_limits.ImageRejected):
        image_limits.open_image(_png_header_only(*pixels))


def test_eps_is_never_handed_to_ghostscript():
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n%%EOF\n"
    # Pretend gs is installed, as on a Homebrew Mac; unrestricted Pillow would then call it on load().
    with patch.object(EpsImagePlugin, "gs_windows_binary", None), patch("shutil.which", return_value="/usr/bin/gs"), patch(
        "subprocess.check_call"
    ) as check_call, patch("subprocess.Popen") as popen:
        with pytest.raises(image_limits.ImageRejected):
            image_limits.open_image(eps)
    popen.assert_not_called()
    check_call.assert_not_called()


def test_oversized_bytes_are_refused_without_parsing():
    with patch.object(image_limits.PILImage, "open") as pil_open:
        with pytest.raises(image_limits.ImageRejected):
            image_limits.open_image(b"\x00" * (image_limits.MAX_IMAGE_BYTES + 1))
    pil_open.assert_not_called()


# --- downloads ----------------------------------------------------------------


def _client_with(handler):
    transport = httpx.MockTransport(handler)
    return lambda *args, **kwargs: _RealAsyncClient(*args, transport=transport, **kwargs)


@pytest.mark.asyncio
async def test_download_stops_at_the_byte_cap(monkeypatch):
    monkeypatch.setattr(image_limits, "MAX_IMAGE_BYTES", 1000)

    async def body():
        for _ in range(100):
            yield b"x" * 100

    handler = lambda request: httpx.Response(200, content=body())  # noqa: E731  (no content-length)
    with patch.object(image_limits.httpx, "AsyncClient", _client_with(handler)):
        with pytest.raises(image_limits.ImageRejected):
            await image_limits.fetch_capped("https://scontent.xx.fbcdn.net/a.jpg", {}, follow_redirects=False, max_bytes=1000)
        assert await media_helpers.download_image("https://scontent.xx.fbcdn.net/a.jpg") is None


@pytest.mark.asyncio
async def test_declared_length_over_the_cap_is_refused_up_front():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-length": str(image_limits.MAX_IMAGE_BYTES + 1)}, content=b"x")

    with patch.object(image_limits.httpx, "AsyncClient", _client_with(handler)):
        assert await media_helpers.try_multiple_download_methods("https://scontent.xx.fbcdn.net/a.jpg") is None
    assert len(calls) == 1  # too big: no retry with the other header profiles


@pytest.mark.asyncio
async def test_a_trickling_server_hits_the_overall_deadline():
    async def body():
        while True:
            await asyncio.sleep(0.05)
            yield b"x"

    handler = lambda request: httpx.Response(200, content=body())  # noqa: E731
    with patch.object(image_limits.httpx, "AsyncClient", _client_with(handler)):
        with pytest.raises(asyncio.TimeoutError):
            await image_limits.fetch_capped("https://scontent.xx.fbcdn.net/a.jpg", {}, follow_redirects=False, deadline=0.3)


@pytest.mark.asyncio
async def test_retries_share_one_deadline(monkeypatch):
    monkeypatch.setattr(image_limits, "DOWNLOAD_DEADLINE_SECONDS", 0.3)
    seen_deadlines = []

    async def slow_fetch(url, headers, *, follow_redirects, deadline):
        seen_deadlines.append(deadline)
        await asyncio.sleep(0.2)
        raise httpx.ReadTimeout("slow")

    with patch.object(image_limits, "fetch_capped", slow_fetch):
        assert await media_helpers.try_multiple_download_methods("https://scontent.xx.fbcdn.net/a.jpg") is None
    assert len(seen_deadlines) == 2  # the third attempt never starts
    assert seen_deadlines[1] < seen_deadlines[0]


# --- uploads ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_path_is_not_sent_to_meta_as_image_bytes():
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        raw = await upload_ad_image_asset(
            ad_account_id="act_1", image_file_path="/home/someone/private/cover-final.png", meta_access_token="t"
        )
    api.assert_not_called()
    assert _error(raw) == image_limits.NOT_AN_IMAGE
    assert "/home/someone" not in raw


@pytest.mark.asyncio
async def test_oversized_base64_is_refused(monkeypatch):
    monkeypatch.setattr(image_limits, "MAX_IMAGE_BASE64_CHARS", 100)
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        raw = await upload_ad_image_asset(
            ad_account_id="act_1", image_file_path="data:image/png;base64," + "A" * 200, meta_access_token="t"
        )
    api.assert_not_called()
    assert "Meta's limit" in _error(raw)


@pytest.mark.asyncio
async def test_valid_base64_still_uploads():
    encoded = base64.b64encode(_png(4, 4)).decode()
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock, return_value={"images": {}}) as api:
        await upload_ad_image_asset(ad_account_id="act_1", image_file_path=encoded, meta_access_token="t")
        await upload_ad_image_asset(ad_account_id="act_1", image_file_path=f"data:image/png;base64,{encoded}", meta_access_token="t")
    assert api.await_count == 2
    assert all(call.args[2]["bytes"] == encoded for call in api.await_args_list)


@pytest.mark.asyncio
async def test_oversized_local_files_are_not_read(tmp_path, monkeypatch):
    image = tmp_path / "big.png"
    image.write_bytes(b"x" * 200)
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * 200)
    monkeypatch.setattr(image_limits, "MAX_IMAGE_BYTES", 100)
    monkeypatch.setattr(image_limits, "MAX_VIDEO_FILE_BYTES", 100)
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        image_reply = await upload_ad_image_asset(ad_account_id="act_1", image_file_path=str(image), meta_access_token="t")
        video_reply = await upload_ad_video_asset(ad_account_id="act_1", video_file_path=str(video), meta_access_token="t")
    api.assert_not_called()
    assert "Meta's limit" in _error(image_reply)
    assert "video_source_url" in _error(video_reply)


@pytest.mark.parametrize(
    "hostile",
    [
        " " * 40_000 + "!",  # Errata's repro: whitespace then one bad character
        "A " * 20_000 + "!",
        "\n" * 40_000 + "==!",
    ],
)
def test_base64_check_is_linear_on_whitespace_heavy_input(hostile):
    import time

    start = time.perf_counter()
    with pytest.raises(image_limits.ImageRejected):
        image_limits.decode_base64_image(hostile)
    assert time.perf_counter() - start < 0.2


def test_base64_check_stays_fast_at_the_size_cap():
    import time

    hostile = " " * (image_limits.MAX_IMAGE_BASE64_CHARS - 1) + "!"
    start = time.perf_counter()
    with pytest.raises(image_limits.ImageRejected):
        image_limits.decode_base64_image(hostile)
    assert time.perf_counter() - start < 2.0


@pytest.mark.asyncio
async def test_valid_base64_that_is_not_an_image_is_not_sent():
    # "abcd" decodes as base64 but is a path-like string, not an image.
    with patch.object(ad_tools, "make_api_request", new_callable=AsyncMock) as api:
        raw = await upload_ad_image_asset(ad_account_id="act_1", image_file_path="abcd", meta_access_token="t")
    api.assert_not_called()
    assert _error(raw) == image_limits.NOT_AN_IMAGE


@pytest.mark.asyncio
async def test_compressed_responses_are_refused_and_identity_is_requested():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"\x1f\x8b")

    with patch.object(image_limits.httpx, "AsyncClient", _client_with(handler)):
        with pytest.raises(image_limits.ImageRejected):
            await image_limits.fetch_capped("https://scontent.xx.fbcdn.net/a.jpg", {}, follow_redirects=False)
    assert seen[0].headers["accept-encoding"] == "identity"


def test_read_capped_stops_reading_past_the_cap(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 101)
    with pytest.raises(image_limits.ImageRejected):
        image_limits.read_capped(str(path), 100)
    path.write_bytes(b"x" * 100)
    assert image_limits.read_capped(str(path), 100) == b"x" * 100
