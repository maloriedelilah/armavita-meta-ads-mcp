# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""Shared constants for Meta Ads MCP runtime configuration."""

import os
import re


def _normalize_graph_api_version(raw_version: str) -> str:
    """Normalize API version to vNN.N format, with safe fallback."""
    if not raw_version:
        return "v27.0"

    candidate = raw_version.strip()
    if not candidate:
        return "v27.0"

    if not candidate.startswith("v"):
        candidate = f"v{candidate}"

    if not re.match(r"^v\d+\.\d+$", candidate):
        return "v27.0"

    return candidate


META_GRAPH_API_VERSION = _normalize_graph_api_version(
    os.environ.get("META_GRAPH_API_VERSION", "v27.0")
)
META_GRAPH_API_BASE = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
META_OAUTH_DIALOG_BASE = f"https://www.facebook.com/{META_GRAPH_API_VERSION}/dialog/oauth"
META_OAUTH_TOKEN_BASE = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/oauth/access_token"

def graph_major(version: str = "") -> int:
    """The major number of a vNN.N version (the running one by default); 0 if unreadable."""
    match = re.match(r"^v(\d+)\.", version or META_GRAPH_API_VERSION)
    return int(match.group(1)) if match else 0
