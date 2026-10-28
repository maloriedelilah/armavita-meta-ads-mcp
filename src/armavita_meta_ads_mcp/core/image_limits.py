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
import re
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

_BASE64 = re.compile(r"^[A-Za-z0-9+/\s]*={0,2}\s*$")


class ImageRejected(ValueError):
    """The bytes are too big, too many pixels, or not an allowed format."""


def open_image(raw: bytes) -> PILImage.Image:
    """Open untrusted image bytes with the format and size limits applied, and load them."""
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"image is larger than {MAX_IMAGE_BYTES} bytes")
    try:
        with warnings.catch_warnings():
            # Pillow only warns between MAX_IMAGE_PIXELS and twice that; treat the warning as a refusal.
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            image = PILImage.open(io.BytesIO(raw), formats=list(ALLOWED_IMAGE_FORMATS))
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ImageRejected(f"image has more than {MAX_IMAGE_PIXELS} pixels")
            image.load()
    except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning) as exc:
        raise ImageRejected(f"image has more than {MAX_IMAGE_PIXELS} pixels") from exc
    except PILImage.UnidentifiedImageError as exc:
        raise ImageRejected(f"not a {', '.join(ALLOWED_IMAGE_FORMATS)} image") from exc
    return image


def base64_problem(encoded: str) -> Optional[str]:
    """Return why `encoded` can't be uploaded as image base64, or None if it can."""
    if len(encoded) > MAX_IMAGE_BASE64_CHARS:
        return f"The image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB, Meta's limit for ad images."
    if not encoded.strip() or not _BASE64.match(encoded):
        return "image_file_path is neither a readable file nor base64 / a data: URL."
    try:
        base64.b64decode(re.sub(r"\s", "", encoded), validate=True)
    except (binascii.Error, ValueError):
        return "image_file_path is neither a readable file nor base64 / a data: URL."
    return None


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
        async with httpx.AsyncClient(follow_redirects=follow_redirects, timeout=30.0) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise ImageRejected(f"download is larger than {max_bytes} bytes")
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageRejected(f"download is larger than {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)

    return await asyncio.wait_for(_get(), timeout=deadline)
