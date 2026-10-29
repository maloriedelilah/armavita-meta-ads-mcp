# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""Shared utility helpers used across MCP tools."""

import logging
import os
import pathlib
import platform
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

import httpx

from .._version import __version__

PACKAGE_NAME = "armavita-meta-ads-mcp"
USER_AGENT = f"{PACKAGE_NAME}/{__version__}"
LOGGER_NAME = "armavita_meta_ads_mcp"

META_APP_ID = os.environ.get("META_APP_ID", "")
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")


def _resolve_log_file() -> pathlib.Path:
    """Return a writable per-user log file path across OSes."""
    system_name = platform.system().lower()
    if system_name == "windows":
        root = pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home())
    elif system_name == "darwin":
        root = pathlib.Path.home() / "Library" / "Application Support"
    else:
        root = pathlib.Path.home() / ".config"

    log_dir = root / PACKAGE_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "server.log"


class _PrivateFileHandler(logging.FileHandler):
    """A FileHandler whose file is owner-only (0600) from the moment it exists (ADS-45)."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(self.baseFilename, 0o600)  # an older server.log may still be 0644
        except OSError:
            pass
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def _create_logger() -> logging.Logger:
    from .log_redaction import install_on_handlers

    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        install_on_handlers(logger.handlers)
        return logger

    logger.setLevel(logging.INFO)
    # delay=True: the file isn't created until something is logged to it. In HTTP mode the
    # handler is swapped for stderr before anything is, so no server.log appears at all.
    handler = _PrivateFileHandler(_resolve_log_file(), encoding="utf-8", delay=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(formatter)
    install_on_handlers([handler])  # ADS-45: no token reaches server.log
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_startup_config() -> None:
    """Startup lines, logged once the transport (and so the log destination) is known."""
    from .graph_client import META_GRAPH_API_VERSION

    logger.info("logger_initialized")
    logger.info("Core API initialized using Graph version %s", META_GRAPH_API_VERSION)
    if not META_APP_ID:
        logger.warning("META_APP_ID is not configured")
    if not META_APP_SECRET:
        logger.warning("META_APP_SECRET is not configured")


logger = _create_logger()


def _dedupe_preserving_order(values: Iterable[str]) -> List[str]:
    seen = set()
    unique: List[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _extract_asset_feed_urls(asset_feed_spec: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    images = asset_feed_spec.get("images")
    if isinstance(images, list):
        for image_entry in images:
            if isinstance(image_entry, dict):
                url = image_entry.get("url")
                if isinstance(url, str) and url:
                    urls.append(url)
    return urls


def extract_creative_image_urls(creative: Dict[str, Any]) -> List[str]:
    """Extract likely image URLs from creative payloads in quality-first order."""
    if not isinstance(creative, dict):
        return []

    candidates: List[str] = []

    high_priority = creative.get("image_urls_for_viewing")
    if isinstance(high_priority, list):
        candidates.extend([url for url in high_priority if isinstance(url, str) and url])

    direct_image_url = creative.get("image_url") or creative.get("image_source_url")
    if isinstance(direct_image_url, str) and direct_image_url:
        candidates.append(direct_image_url)

    story = creative.get("object_story_spec")
    if isinstance(story, dict):
        link_data = story.get("link_data")
        if isinstance(link_data, dict):
            for key in ("picture", "image_url", "image_source_url"):
                maybe_url = link_data.get(key)
                if isinstance(maybe_url, str) and maybe_url:
                    candidates.append(maybe_url)

        video_data = story.get("video_data")
        if isinstance(video_data, dict):
            maybe_video_image = video_data.get("image_url") or video_data.get("image_source_url")
            if isinstance(maybe_video_image, str) and maybe_video_image:
                candidates.append(maybe_video_image)

    asset_feed = creative.get("asset_feed_spec")
    if isinstance(asset_feed, dict):
        candidates.extend(_extract_asset_feed_urls(asset_feed))

    thumbnail_url = creative.get("thumbnail_url")
    if isinstance(thumbnail_url, str) and thumbnail_url:
        candidates.append(thumbnail_url)

    return _dedupe_preserving_order(candidates)


# Meta's own image hosts. In HTTP mode the server only fetches from these
# (ADS-51): a caller-chosen URL would let a remote caller reach the host's LAN.
_META_IMAGE_HOST_SUFFIXES = (".fbcdn.net", ".facebook.com", ".cdninstagram.com")


def http_mode_fetch_allowed(url: str) -> bool:
    """In HTTP mode, only https URLs on Meta's image hosts may be fetched. Always True in stdio mode."""
    from . import http_mode

    if not http_mode.is_enabled():
        return True
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and parts.port in (None, 443) and host.endswith(_META_IMAGE_HOST_SUFFIXES)


def _follow_redirects() -> bool:
    from . import http_mode

    # A redirect could leave Meta's hosts; in HTTP mode don't follow any.
    return not http_mode.is_enabled()


async def download_image(url: str) -> Optional[bytes]:
    """Download image bytes from a remote URL."""
    if not url:
        return None
    if not http_mode_fetch_allowed(url):
        logger.warning("image_download_refused reason=not_a_meta_image_host_in_http_mode")
        return None

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    from .image_limits import fetch_capped

    try:
        # ADS-62: streamed, capped at Meta's image size, with one overall deadline.
        return await fetch_capped(url, headers, follow_redirects=_follow_redirects())
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_download_failed url=%s error=%s", url, type(exc).__name__)
        return None


async def try_multiple_download_methods(url: str) -> Optional[bytes]:
    """Try multiple header profiles to retrieve image bytes from strict CDNs."""
    if not url:
        return None
    if not http_mode_fetch_allowed(url):
        logger.warning("image_download_refused reason=not_a_meta_image_host_in_http_mode")
        return None

    header_profiles = [
        {
            "User-Agent": USER_AGENT,
            "Accept": "image/*,*/*;q=0.8",
        },
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.facebook.com/",
        },
        {
            "User-Agent": "curl/8.4.0",
            "Accept": "*/*",
        },
    ]

    import time

    from .image_limits import DOWNLOAD_DEADLINE_SECONDS, ImageRejected, fetch_capped

    # ADS-62: one deadline for all attempts together, and no retry once a response is too big.
    give_up_at = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    for profile in header_profiles:
        remaining = give_up_at - time.monotonic()
        if remaining <= 0:
            logger.warning("image_download_failed url=%s error=deadline", url)
            break
        try:
            return await fetch_capped(url, profile, follow_redirects=_follow_redirects(), deadline=remaining)
        except ImageRejected:
            logger.warning("image_download_refused url=%s reason=too_large", url)
            break
        except Exception as exc:  # noqa: BLE001
            logger.debug("image_download_attempt_failed url=%s ua=%s error=%s", url, profile.get("User-Agent"), type(exc).__name__)

    return None