# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""OSS-local duplication tools for Meta Ads API."""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from . import auth_state as auth
from . import status_policy
from .graph_client import McpToolError, make_api_request, meta_api_tool
from .mcp_runtime import mcp_server
from mcp.types import ToolAnnotations

logger = logging.getLogger(__name__)


# Preserve exception class identity across importlib.reload() in tests.
if "RateLimitError" not in globals():
    class RateLimitError(McpToolError):
        """Raised on rate-limit errors so FastMCP sets isError: true."""


if "DuplicationError" not in globals():
    class DuplicationError(McpToolError):
        """Raised on non-success duplication responses."""


from .meta_v25_guards import detect_deprecated_advantage_plus_block


async def _resolve_campaign_id_for_resource(
    resource_type: str,
    resource_id: str,
    meta_access_token: str,
) -> Optional[str]:
    """Resolve parent campaign ID for campaign/adset/ad duplication resources."""
    if resource_type == "campaign":
        return resource_id

    if resource_type == "adset":
        adset_data = await make_api_request(resource_id, meta_access_token, {"fields": "campaign_id"})
        campaign_id = adset_data.get("campaign_id") if isinstance(adset_data, dict) else None
        return str(campaign_id) if campaign_id else None

    if resource_type == "ad":
        ad_data = await make_api_request(resource_id, meta_access_token, {"fields": "campaign_id,adset{campaign_id}"})
        if not isinstance(ad_data, dict):
            return None
        campaign_id = ad_data.get("campaign_id")
        if campaign_id:
            return str(campaign_id)
        adset_obj = ad_data.get("adset")
        if isinstance(adset_obj, dict) and adset_obj.get("campaign_id"):
            return str(adset_obj.get("campaign_id"))

    return None


async def _run_v25_duplication_preflight(
    resource_type: str,
    resource_id: str,
    meta_access_token: str,
) -> Optional[Dict[str, Any]]:
    """Return block payload when duplication targets deprecated Advantage+ campaign types."""
    if not isinstance(meta_access_token, str) or not meta_access_token.strip():
        return None

    if resource_type not in {"campaign", "adset", "ad"}:
        return None

    try:
        campaign_id = await _resolve_campaign_id_for_resource(resource_type, resource_id, meta_access_token)
        if not campaign_id:
            return None

        campaign_data = await make_api_request(
            campaign_id,
            meta_access_token,
            {"fields": "id,name,objective,smart_promotion_type,advantage_state_info"},
        )
        if not isinstance(campaign_data, dict) or "error" in campaign_data:
            return None

        reason = detect_deprecated_advantage_plus_block(campaign_data)
        if not reason:
            return None

        return {
            "campaign_id": campaign_id,
            "campaign_name": campaign_data.get("name"),
            "campaign_objective": campaign_data.get("objective"),
            "smart_promotion_type": campaign_data.get("smart_promotion_type"),
            "advantage_state_info": campaign_data.get("advantage_state_info"),
            "reason": reason,
        }
    except Exception as exc:
        logger.warning("v25 duplication preflight skipped due to lookup failure: %s", exc)
        return None


def _append_warning(warnings: List[Dict[str, Any]], option: str, reason: str) -> None:
    warnings.append({"option": option, "reason": reason})


class _Unsupported(ValueError):
    """An option this copy can't honour. Refused before anything is copied (ADS-57)."""


def _build_copy_params(resource_type: str, options: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Map tool options to Meta copy edge params.

    Every option either goes to Graph or is refused (_Unsupported) before the copy: a copy that
    quietly ignores an option and reports success is what ADS-57 removed. Budget, targeting and
    content can't be changed by the copy edges; copy first, then use the update_* tools.
    """
    params: Dict[str, Any] = {}
    warnings: List[Dict[str, Any]] = []

    name_suffix = options.get("name_suffix")
    if name_suffix:
        params["rename_options"] = {
            "rename_strategy": "DEEP_RENAME",
            "rename_suffix": str(name_suffix),
        }

    new_status = options.get("new_status")
    if new_status:
        params["status_option"] = str(new_status).upper()

    if resource_type == "campaign":
        include_ad_sets = options.get("include_ad_sets", True) is not False
        include_ads = options.get("include_ads", True) is not False
        if include_ad_sets and not include_ads:
            raise _Unsupported(
                "Meta's campaign copy either copies the whole campaign (its ad sets with their ads) or the campaign "
                "alone. It can't copy ad sets without their ads. Use include_ad_sets=false for the campaign alone, "
                "or copy everything and pause or delete the ads you don't want."
            )
        # deep_copy (Graph default false) copies the ad sets and their ads; without it, the campaign alone.
        params["deep_copy"] = include_ad_sets
        if options.get("migrate_to_advantage_plus") is not None:
            params["migrate_to_advantage_plus"] = (
                "true" if bool(options.get("migrate_to_advantage_plus")) else "false"
            )

    elif resource_type == "adset":
        # deep_copy (Graph default false) also copies the ad set's ads.
        params["deep_copy"] = options.get("include_ads", True) is not False

        target_campaign_id = options.get("target_campaign_id")
        if target_campaign_id:
            params["campaign_id"] = target_campaign_id

    elif resource_type == "ad":
        params["deep_copy"] = bool(options.get("clone_ad_creative", True))

        target_ad_set_id = options.get("target_ad_set_id")
        if target_ad_set_id:
            params["adset_id"] = target_ad_set_id

    elif resource_type == "creative":
        params["deep_copy"] = True

    return params, warnings


def _copied_objects(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every object a (deep) copy made, from Graph's ad_object_ids: type, source and new ID."""
    found = response.get("ad_object_ids")
    if not isinstance(found, list) and isinstance(response.get("data"), dict):
        found = response["data"].get("ad_object_ids")
    objects = []
    for item in found if isinstance(found, list) else []:
        if isinstance(item, dict) and item.get("copied_id"):
            objects.append({
                "type": item.get("ad_object_type"),
                "source_id": str(item.get("source_id") or ""),
                "new_id": str(item.get("copied_id")),
            })
    return objects


def _extract_new_id(resource_type: str, response: Dict[str, Any]) -> Optional[str]:
    """Extract duplicated object ID from diverse Graph copy response shapes."""
    preferred_keys = {
        "campaign": ("copied_campaign_id", "campaign_id", "id"),
        "adset": ("copied_adset_id", "adset_id", "id"),
        "ad": ("copied_ad_id", "ad_id", "id"),
        "creative": ("copied_adcreative_id", "creative_id", "id"),
    }

    for key in preferred_keys.get(resource_type, ()):
        value = response.get(key)
        if value:
            return str(value)

    if isinstance(response.get("data"), dict):
        nested = response["data"]
        for key in preferred_keys.get(resource_type, ()):
            value = nested.get(key)
            if value:
                return str(value)

    if isinstance(response.get("data"), list) and response["data"]:
        first = response["data"][0]
        if isinstance(first, dict):
            for key in preferred_keys.get(resource_type, ()):
                value = first.get(key)
                if value:
                    return str(value)

    return None


def _unwrap_graph_error(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the inner Graph API error from a make_api_request error envelope."""
    graph_error = data.get("error")
    if not isinstance(graph_error, dict):
        return None

    message = str(graph_error.get("message") or graph_error.get("primary_text") or "")
    if graph_error.get("code") is not None and not message.startswith("HTTP Error"):
        return graph_error

    details = graph_error.get("details")
    if isinstance(details, dict):
        nested_error = details.get("error")
        if isinstance(nested_error, dict):
            return nested_error

    return graph_error


def _build_graph_error_payload(
    resource_type: str,
    resource_id: str,
    graph_error: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize Graph API error details into a deterministic duplication error envelope."""
    payload: Dict[str, Any] = {
        "success": False,
        "error": "duplication_failed",
        "message": graph_error.get("message") or graph_error.get("primary_text") or f"Failed to duplicate {resource_type}",
        "details": {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "code": graph_error.get("code"),
        },
    }

    for field in ("error_subcode", "error_user_title", "error_user_msg", "type", "fbtrace_id"):
        if graph_error.get(field) is not None:
            payload[field] = graph_error.get(field)

    return payload


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
@meta_api_tool
async def clone_campaign(
    campaign_id: str,
    meta_access_token: Optional[str] = None,
    name_suffix: Optional[str] = " - Copy",
    include_ad_sets: bool = True,
    include_ads: bool = True,
    new_status: Optional[str] = "PAUSED",
    migrate_to_advantage_plus: Optional[bool] = None,
) -> str:
    """Duplicate a campaign with Meta's copy edge. The copy is PAUSED.

    - include_ad_sets=true (default) copies its ad sets and their ads too; false copies the
      campaign alone. Copying ad sets without their ads isn't possible (refused).
    - The copy keeps the source's budget, targeting and content. To change them, copy first,
      then use update_campaign / update_ad_set / update_ad on the new IDs.
    - The reply lists every object made (`copied_objects`: type, source ID, new ID).
    - Meta copies at most 3 ads in one go; a bigger deep copy is refused by Meta.
    """
    new_status, status_error = status_policy.create_status(new_status)
    if status_error:
        return json.dumps({"error": status_error}, indent=2)
    return await _forward_duplication_request(
        "campaign",
        campaign_id,
        meta_access_token,
        {
            "name_suffix": name_suffix,
            "include_ad_sets": include_ad_sets,
            "include_ads": include_ads,
            "new_status": new_status,
            "migrate_to_advantage_plus": migrate_to_advantage_plus,
        },
    )


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
@meta_api_tool
async def clone_ad_set(
    ad_set_id: str,
    meta_access_token: Optional[str] = None,
    target_campaign_id: Optional[str] = None,
    name_suffix: Optional[str] = " - Copy",
    include_ads: bool = True,
    new_status: Optional[str] = "PAUSED",
) -> str:
    """Duplicate an ad set with Meta's copy edge. The copy is PAUSED.

    - include_ads=true (default) copies its ads too; false copies the ad set alone.
    - target_campaign_id puts the copy under another campaign (it then follows that
      campaign's settings, such as a campaign budget).
    - The copy keeps the source's budget and targeting. To change them, copy first, then
      use update_ad_set on the new ID (targeting_patch changes only the keys you give).
    - The reply lists every object made (`copied_objects`).
    """
    new_status, status_error = status_policy.create_status(new_status)
    if status_error:
        return json.dumps({"error": status_error}, indent=2)
    return await _forward_duplication_request(
        "adset",
        ad_set_id,
        meta_access_token,
        {
            "target_campaign_id": target_campaign_id,
            "name_suffix": name_suffix,
            "include_ads": include_ads,
            "new_status": new_status,
        },
    )


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
@meta_api_tool
async def clone_ad(
    ad_id: str,
    meta_access_token: Optional[str] = None,
    target_ad_set_id: Optional[str] = None,
    name_suffix: Optional[str] = " - Copy",
    clone_ad_creative: bool = True,
    new_status: Optional[str] = "PAUSED",
) -> str:
    """Duplicate an ad with Meta's copy edge. The copy is PAUSED.

    The copy keeps the source's creative. To change it, copy first, then use update_ad.
    The reply lists every object made (`copied_objects`).
    """
    new_status, status_error = status_policy.create_status(new_status)
    if status_error:
        return json.dumps({"error": status_error}, indent=2)
    return await _forward_duplication_request(
        "ad",
        ad_id,
        meta_access_token,
        {
            "target_ad_set_id": target_ad_set_id,
            "name_suffix": name_suffix,
            "clone_ad_creative": clone_ad_creative,
            "new_status": new_status,
        },
    )


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
@meta_api_tool
async def clone_ad_creative(
    ad_creative_id: str,
    meta_access_token: Optional[str] = None,
    name_suffix: Optional[str] = " - Copy",
) -> str:
    """Duplicate a creative with Meta's copy edge.

    The copy has the same text, headline, call to action and link. Creatives can't be edited
    in place on Meta for most fields: to change content, make a new creative with
    create_ad_creative instead.
    """
    return await _forward_duplication_request(
        "creative",
        ad_creative_id,
        meta_access_token,
        {
            "name_suffix": name_suffix,
        },
    )


async def _forward_duplication_request(
    resource_type: str,
    resource_id: str,
    meta_access_token: Optional[str],
    options: Dict[str, Any],
) -> str:
    """Execute OSS-local duplication against Graph API copy edges."""
    try:
        if not str(resource_id or "").strip():
            raise DuplicationError(
                json.dumps(
                    {
                        "success": False,
                        "error": "invalid_parameters",
                        "message": f"{resource_type}_id is required",
                        "details": {"resource_type": resource_type, "resource_id": resource_id},
                    },
                    indent=2,
                )
            )

        facebook_token = meta_access_token if meta_access_token else await auth.get_current_access_token()
        if not facebook_token:
            raise DuplicationError(
                json.dumps(
                    {
                        "success": False,
                        "error": "authentication_required",
                        "message": "No Meta access token is available for the duplication request",
                        "details": {
                            "required": "Valid Meta access token",
                            "check": "Authenticate and retry duplication request.",
                        },
                    },
                    indent=2,
                )
            )

        preflight_block = await _run_v25_duplication_preflight(resource_type, resource_id, facebook_token)
        if preflight_block:
            raise DuplicationError(
                json.dumps(
                    {
                        "success": False,
                        "error": "v25_blocked_operation",
                        "message": "Duplication is blocked for deprecated Advantage+ Shopping/App campaign flows in v25.",
                        "details": {
                            "resource_type": resource_type,
                            "resource_id": resource_id,
                            "campaign_id": preflight_block.get("campaign_id"),
                            "campaign_name": preflight_block.get("campaign_name"),
                            "campaign_objective": preflight_block.get("campaign_objective"),
                            "smart_promotion_type": preflight_block.get("smart_promotion_type"),
                            "reason": preflight_block.get("reason"),
                        },
                        "suggestion": (
                            "Use supported Advantage+ migration or rebuild the campaign with v25-compatible "
                            "flows before attempting duplication."
                        ),
                    },
                    indent=2,
                )
            )

        try:
            copy_params, warnings = _build_copy_params(resource_type, options)
        except _Unsupported as refused:
            raise DuplicationError(
                json.dumps(
                    {
                        "success": False,
                        "error": "unsupported_option",
                        "message": f"{refused} Nothing was copied.",
                        "details": {"resource_type": resource_type, "resource_id": resource_id},
                    },
                    indent=2,
                )
            )
        endpoint = f"{resource_id}/copies"

        data = await make_api_request(endpoint, facebook_token, copy_params, method="POST")
        if not isinstance(data, dict):
            raise DuplicationError(
                json.dumps(
                    {
                        "success": False,
                        "error": "duplication_failed",
                        "message": "Unexpected response from Graph API copy edge",
                        "details": {
                            "resource_type": resource_type,
                            "resource_id": resource_id,
                            "response_type": type(data).__name__,
                        },
                    },
                    indent=2,
                )
            )

        graph_error = _unwrap_graph_error(data)
        if graph_error:
            code = graph_error.get("code")
            if code == 4:
                raise RateLimitError(
                    json.dumps(
                        {
                            "error": "rate_limit_exceeded",
                            "message": graph_error.get("message") or graph_error.get("primary_text", "Meta API rate limit exceeded"),
                            "details": {
                                "code": code,
                                "error_subcode": graph_error.get("error_subcode"),
                                "retry_hint": "Retry with backoff.",
                            },
                        },
                        indent=2,
                    )
                )

            raise DuplicationError(
                json.dumps(
                    _build_graph_error_payload(resource_type, resource_id, graph_error),
                    indent=2,
                )
            )

        new_id = _extract_new_id(resource_type, data)
        success = bool(new_id) or bool(data.get("success") is True)

        return json.dumps(
            {
                "success": success,
                "source_id": resource_id,
                "resource_type": resource_type,
                "new_id": new_id,
                "copied_objects": _copied_objects(data),
                "warnings": warnings,
                "meta_response": data,
            },
            indent=2,
        )

    except (RateLimitError, DuplicationError):
        raise
    except Exception as exc:
        raise DuplicationError(
            json.dumps(
                {
                    "success": False,
                    "error": "unexpected_error",
                    "message": f"The {resource_type} duplication failed with an unexpected error",
                    "details": {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "error": str(exc),
                    },
                },
                indent=2,
            )
        )
