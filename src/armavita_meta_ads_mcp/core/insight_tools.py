# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""Insights query tools."""


import json
from typing import Any, Dict, List, Optional, Union

from .graph_client import make_api_request, meta_api_tool
from .insight_query_params import normalize_breakdown_inputs, normalize_time_input
from .meta_v25_guards import append_warning, attribution_window_warning, deprecated_attribution_windows
from .mcp_runtime import mcp_server
from mcp.types import ToolAnnotations

_DEFAULT_FIELDS = (
    "account_id,account_name,campaign_id,campaign_name,adset_id,adset_name,"
    "ad_id,ad_name,impressions,clicks,spend,cpc,cpm,ctr,reach,frequency,"
    "actions,action_values,conversions,unique_clicks,cost_per_action_type,"
    "instagram_profile_visits"
)

_VALID_LEVELS = {"ad", "adset", "campaign", "account"}

# Aggregate "action_type" families that duplicate more specific rows. NOTE:
# "offsite_conversion.fb_pixel_*" is intentionally NOT listed — those are the
# advertiser's primary pixel conversions (purchase/add_to_cart/lead), not
# redundant aggregates, and stripping them silently deletes core conversions.
_REDUNDANT_ACTION_PREFIXES = (
    "omni_",
    "onsite_web_app_",
    "onsite_web_",
    "onsite_app_",
    "web_app_in_store_",
)

def _strip_redundant_actions(row: Dict[str, Any]) -> Dict[str, Any]:
    for container_key in ("actions", "action_values", "cost_per_action_type"):
        values = row.get(container_key)
        if not isinstance(values, list):
            continue

        filtered = []
        for entry in values:
            action_type = ""
            if isinstance(entry, dict):
                action_type = str(entry.get("action_type", ""))
            if any(action_type.startswith(prefix) for prefix in _REDUNDANT_ACTION_PREFIXES):
                continue
            filtered.append(entry)
        row[container_key] = filtered
    return row


def _with_warning(payload: Dict[str, Any], deprecated: List[str]) -> Dict[str, Any]:
    return append_warning(payload, attribution_window_warning(deprecated))


def _append_warnings(payload: Dict[str, Any], warnings: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not warnings:
        return payload
    existing = payload.get("warnings")
    if isinstance(existing, list):
        existing.extend(warnings)
    else:
        payload["warnings"] = list(warnings)
    return payload


_INCREMENT_WORDS = {"monthly", "all_days"}
MAX_PAGE_SIZE = 500


def _time_increment(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Graph's time_increment: a whole number of days 1-90, "monthly" or "all_days" (ADS-106)."""
    why = 'time_increment must be a whole number of days from 1 to 90, "monthly" or "all_days".'
    if isinstance(value, bool):
        return None, why
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _INCREMENT_WORDS:
            return word, None
        if not (word.isascii() and word.isdigit()) or len(word) > 3:
            return None, why
        value = int(word)
    if isinstance(value, int) and 1 <= value <= 90:
        return str(value), None
    return None, why


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@meta_api_tool
async def list_insights(
    object_id: str,
    meta_access_token: Optional[str] = None,
    date_range: Union[str, Dict[str, str]] = "maximum",
    breakdown: str = "",
    breakdowns: Optional[List[str]] = None,
    action_breakdowns: Optional[List[str]] = None,
    summary_action_breakdowns: Optional[List[str]] = None,
    level: str = "ad",
    page_size: int = 25,
    page_cursor: str = "",
    action_attribution_windows: Optional[List[str]] = None,
    compact: bool = False,
    fields: Optional[str] = None,
    time_increment: Optional[Union[int, str]] = None,
) -> str:
    """Fetch insights for an account, campaign, ad set, or ad.

    Pass `fields` to request custom metrics (including names from
    `list_ad_custom_derived_metrics`). Defaults to a standard performance bundle.

    `time_increment` splits the date range into rows: 1 gives one row per day (each row has
    date_start and date_stop), 7 one per week, any whole number of days from 1 to 90;
    "monthly" one per calendar month; "all_days" (Graph's default) one row for the whole
    range. Daily numbers for a week of ad sets are then one call: date_range "last_7d",
    level "adset", time_increment 1. Rows multiply (days x objects), so follow the paging
    cursor until there is no `next`, and send the same arguments with each `page_cursor`
    (the cursor only says where to resume). Without `date_range` the range is "maximum".

    `page_size` is rows per page, 1 to 500.
    """
    if not str(object_id or "").strip():
        return json.dumps({"error": "An object_id (account, campaign, ad set, or ad) is required"}, indent=2)

    normalized_level = str(level or "").strip().lower()
    if normalized_level not in _VALID_LEVELS:
        return json.dumps(
            {
                "error": "invalid_level",
                "message": f"level must be one of {sorted(_VALID_LEVELS)}.",
                "provided": level,
            },
            indent=2,
        )

    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= MAX_PAGE_SIZE:
        return json.dumps(
            {"error": "invalid_page_size", "message": f"page_size must be a whole number from 1 to {MAX_PAGE_SIZE}.", "provided": page_size},
            indent=2,
        )

    params: Dict[str, Any] = {
        "fields": fields or _DEFAULT_FIELDS,
        "level": normalized_level,
        "page_size": int(page_size),
    }

    time_params, time_error, normalization_warnings = normalize_time_input(date_range, default_preset="maximum")
    if time_error:
        return json.dumps(time_error, indent=2)
    params.update(time_params or {})

    breakdown_params, breakdown_warnings = normalize_breakdown_inputs(
        breakdown=breakdown,
        breakdowns=breakdowns,
        action_breakdowns=action_breakdowns,
        summary_action_breakdowns=summary_action_breakdowns,
    )
    params.update(breakdown_params)
    normalization_warnings.extend(breakdown_warnings)

    if time_increment is not None and time_increment != "":
        increment, increment_error = _time_increment(time_increment)
        if increment_error:
            return json.dumps({"error": "invalid_time_increment", "message": increment_error, "provided": time_increment}, indent=2)
        params["time_increment"] = increment

    if page_cursor:
        params["page_cursor"] = page_cursor

    deprecated_windows = deprecated_attribution_windows(action_attribution_windows)
    if action_attribution_windows:
        params["action_attribution_windows"] = list(action_attribution_windows)

    payload = await make_api_request(f"{object_id}/insights", meta_access_token, params)

    if isinstance(payload, dict):
        _append_warnings(payload, normalization_warnings)
        _with_warning(payload, deprecated_windows)

        if compact and isinstance(payload.get("data"), list):
            payload["data"] = [
                _strip_redundant_actions(row) if isinstance(row, dict) else row
                for row in payload["data"]
            ]

    return json.dumps(payload, indent=2)
