# SPDX-License-Identifier: AGPL-3.0-only

"""Limits for untrusted image and video bytes (ADS-62).

Creative image URLs, uploaded base64 and local files all come from someone
other than this server, so every path that decodes, downloads or buffers them
goes through here:

- Pillow opens only JPEG, PNG, WEBP and GIF. Formats such as EPS hand the bytes
  to Ghostscript in a child process that would inherit the server's
  environment.
- Images over MAX_IMAGE_PIXELS are refused outright. Pillow's default only
  warns up to twice its limit; a 20 KB PNG can otherwise decode to ~900 MB.
- Downloads stream with a byte cap and an overall deadline, so a server that
  trickles bytes can't hold a connection open indefinitely.
- Base64 uploads and local files are size-checked before they're read.
"""

import asyncio
import base64
import binascii
import io
import warnings
from typing import Dict, Optional

import httpx
from PIL import Image as PILImage

# Meta's own ad image limit is 30 MB.
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_BASE64_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4
# 40 megapixels: well above any ad image (Meta recommends 1080-1440 px sides).
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
# One file in one request (no chunked upload); Meta's simple /advideos upload tops out around here.
MAX_VIDEO_FILE_BYTES = 1024 * 1024 * 1024
# Whole download, all bytes, not per read.
DOWNLOAD_DEADLINE_SECONDS = 60.0

PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

class ImageRejected(ValueError):
    """The bytes are too big, too many pixels, or not an allowed format."""


def _open_checked(raw: bytes) -> PILImage.Image:
    """Identify the bytes (header only) with the format, size and pixel limits applied."""
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"image is larger than {MAX_IMAGE_BYTES} bytes")
    try:
        with warnings.catch_warnings():
            # Pillow only warns between MAX_IMAGE_PIXELS and twice that; treat the warning as a refusal.
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            image = PILImage.open(io.BytesIO(raw), formats=list(ALLOWED_IMAGE_FORMATS))
    except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning) as exc:
        raise ImageRejected(f"image has more than {MAX_IMAGE_PIXELS} pixels") from exc
    except (PILImage.UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRejected(f"not a {', '.join(ALLOWED_IMAGE_FORMATS)} image") from exc
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise ImageRejected(f"image has more than {MAX_IMAGE_PIXELS} pixels")
    return image


def open_image(raw: bytes) -> PILImage.Image:
    """Open untrusted image bytes with the format and size limits applied, and load them."""
    image = _open_checked(raw)
    with warnings.catch_warnings():
        warnings.simplefilter("error", PILImage.DecompressionBombWarning)
        image.load()
    return image


def check_image(raw: bytes) -> None:
    """Raise ImageRejected unless `raw` is an allowed image within the limits. Reads the header only."""
    _open_checked(raw).close()


NOT_AN_IMAGE = "image_file_path is not a readable file, and not base64 / a data: URL of a JPEG, PNG, WEBP or GIF image."
TOO_LARGE = f"The image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB, Meta's limit for ad images."


def decode_base64_image(encoded: str) -> str:
    """Validate upload base64 and return it with whitespace removed. Raises ImageRejected with a user-facing message.

    Linear time throughout (ADS-62 review): no regex, whitespace removed with split/join, then a strict decode.
    """
    if len(encoded) > MAX_IMAGE_BASE64_CHARS:
        raise ImageRejected(TOO_LARGE)
    compact = "".join(encoded.split())
    if not compact:
        raise ImageRejected(NOT_AN_IMAGE)
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageRejected(NOT_AN_IMAGE) from exc
    try:
        check_image(raw)
    except ImageRejected as exc:
        # Decodes as base64 but isn't an allowed image (e.g. a path like "abcd"): never sent to Meta.
        raise ImageRejected(TOO_LARGE if "larger" in str(exc) else NOT_AN_IMAGE) from exc
    return compact


def read_capped(path: str, max_bytes: int) -> bytes:
    """Read at most `max_bytes` from a local file; raise ImageRejected if there's more (no stat-then-read race)."""
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ImageRejected(f"file is larger than {max_bytes} bytes")
    return data


async def fetch_capped(
    url: str,
    headers: Dict[str, str],
    *,
    follow_redirects: bool,
    max_bytes: Optional[int] = None,
    deadline: Optional[float] = None,
) -> bytes:
    """GET `url`, streaming, and stop at `max_bytes` or after `deadline` seconds in total."""
    max_bytes = MAX_IMAGE_BYTES if max_bytes is None else max_bytes
    deadline = DOWNLOAD_DEADLINE_SECONDS if deadline is None else deadline

    async def _get() -> bytes:
        # identity only: the cap counts the bytes we hold, so a small gzip body can't inflate past it.
        request_headers = {**headers, "Accept-Encoding": "identity"}
        async with httpx.AsyncClient(follow_redirects=follow_redirects, timeout=30.0) as client:
            async with client.stream("GET", url, headers=request_headers) as response:
                response.raise_for_status()
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise ImageRejected(f"compressed response ({encoding}) refused")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise ImageRejected(f"download is larger than {max_bytes} bytes")
                chunks = []
                total = 0
                # Any content-encoding is refused above, so these are the bytes on the wire, not inflated ones.
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageRejected(f"download is larger than {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)

    return await asyncio.wait_for(_get(), timeout=deadline)
