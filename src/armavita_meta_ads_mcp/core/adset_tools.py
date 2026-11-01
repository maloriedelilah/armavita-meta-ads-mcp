# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""Ad set CRUD tools."""


import json
from typing import Any, Dict, List, Optional

from . import money, status_policy
from .graph_client import make_api_request, meta_api_tool
from .graph_constants import META_GRAPH_API_VERSION
from .mcp_runtime import mcp_server
from mcp.types import ToolAnnotations

_REQUIRED_STORE_HOSTS = ("apps.apple.com", "itunes.apple.com", "play.google.com")
_BID_STRATEGIES_REQUIRING_BID_AMOUNT = {"LOWEST_COST_WITH_BID_CAP", "COST_CAP"}
_BID_STRATEGY_MIN_ROAS = "LOWEST_COST_WITH_MIN_ROAS"


# Default for update_ad_set's value_rule_set_id. It lets the tool tell an
# omitted argument apart from an explicit JSON null: FastMCP turns both into
# None when the default is None. Omitted keeps the current set; null is
# refused (some clients send null for every unused optional field).
_KEEP_VALUE_RULE_SET = "keep"


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2)


def _normalize_bid_strategy(bid_strategy: Optional[str]) -> Optional[str]:
    if bid_strategy is None:
        return None
    return str(bid_strategy).strip().upper()


def _validate_promoted_object_for_app_installs(
    optimization_goal: str,
    promoted_object: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if optimization_goal != "APP_INSTALLS":
        return None

    if not promoted_object:
        return {
            "error": "APP_INSTALLS optimization cannot run without a promoted_object",
            "details": "App-promotion ad sets have to identify the app being advertised",
            "required_fields": ["application_id", "object_store_url"],
        }

    if not isinstance(promoted_object, dict):
        return {
            "error": "promoted_object must be a JSON object",
            "example": {
                "application_id": "987650000012345",
                "object_store_url": "https://apps.apple.com/app/id987650001",
            },
        }

    if not promoted_object.get("application_id"):
        return {
            "error": "promoted_object lacks the mandatory application_id key",
            "details": "application_id is the Facebook app ID for your mobile app",
        }

    store_url = str(promoted_object.get("object_store_url", ""))
    if not store_url:
        return {
            "error": "promoted_object lacks the mandatory object_store_url key",
            "details": "object_store_url should be the App Store or Google Play URL for your app",
        }

    if not any(host in store_url for host in _REQUIRED_STORE_HOSTS):
        return {
            "error": "object_store_url is not a recognized app-store link",
            "details": "Provide an apps.apple.com (App Store) or play.google.com (Google Play) URL",
            "provided_url": store_url,
        }

    return None


def _validate_bid_controls(
    bid_strategy: Optional[str],
    bid_amount: Optional[int],
    bid_constraints: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    normalized = _normalize_bid_strategy(bid_strategy)
    if normalized is None:
        return None

    if normalized == "TARGET_COST":
        return {
            "error": "bid_strategy 'TARGET_COST' is deprecated and not supported",
            "details": "Use one of: LOWEST_COST_WITHOUT_CAP, LOWEST_COST_WITH_BID_CAP, COST_CAP, LOWEST_COST_WITH_MIN_ROAS",
        }

    if normalized == "LOWEST_COST":
        return {
            "error": "bid_strategy 'LOWEST_COST' does not exist in the Marketing API",
            "details": f"The 'LOWEST_COST' bid strategy is not valid in Meta Ads API {META_GRAPH_API_VERSION}",
            "workaround": "Pass 'LOWEST_COST_WITHOUT_CAP' instead; it needs no bid_amount",
            "valid_values": [
                "LOWEST_COST_WITHOUT_CAP",
                "LOWEST_COST_WITH_BID_CAP",
                "COST_CAP",
                "LOWEST_COST_WITH_MIN_ROAS",
            ],
        }

    if normalized in _BID_STRATEGIES_REQUIRING_BID_AMOUNT and bid_amount is None:
        return {
            "error": f"bid_amount is required when using bid_strategy '{normalized}'",
            "details": f"The '{normalized}' bid strategy requires you to specify a bid amount in cents",
            "workaround": "Either provide bid_amount or use LOWEST_COST_WITHOUT_CAP",
            "example_with_bid_amount": f'{{\"bid_strategy\": \"{normalized}\", \"bid_amount\": 500}}',
            "example_without_bid_amount": '{"bid_strategy": "LOWEST_COST_WITHOUT_CAP"}',
        }

    if normalized == _BID_STRATEGY_MIN_ROAS and not bid_constraints:
        return {
            "error": "bid_constraints is required when using bid_strategy 'LOWEST_COST_WITH_MIN_ROAS'",
            "details": "Provide bid_constraints with roas_average_floor (target ROAS * 10000)",
            "example": {
                "bid_strategy": "LOWEST_COST_WITH_MIN_ROAS",
                "bid_constraints": {"roas_average_floor": 20000},
                "optimization_goal": "VALUE",
            },
        }

    return None


def _default_targeting() -> Dict[str, Any]:
    return {
        "age_min": 18,
        "age_max": 65,
        "geo_locations": {"countries": ["US"]},
        "targeting_automation": {"advantage_audience": 1},
    }


_REJECTED_TARGETING_EXCLUSION_KEYS = {"exclusions"}

# Placements removed in v24.0+ — stripped with a warning instead of letting Meta hard-fail.
_DEPRECATED_FACEBOOK_POSITIONS = {"video_feeds"}

_PLACEMENT_SOFT_OPT_OUT_KEYS = frozenset(
    {
        "facebook_positions",
        "instagram_positions",
        "audience_network_positions",
        "messenger_positions",
        "threads_positions",
    }
)

_AD_SET_LIST_FIELDS = (
    "id,name,campaign_id,status,daily_budget,lifetime_budget,targeting,bid_amount,bid_strategy,"
    "bid_constraints,optimization_goal,billing_event,start_time,end_time,created_time,updated_time,"
    "is_dynamic_creative,placement_soft_opt_out,value_rule_set_id,"
    "frequency_control_specs{event,interval_days,max_frequency}"
)

_AD_SET_READ_FIELDS = (
    "id,name,campaign_id,status,frequency_control_specs{event,interval_days,max_frequency},"
    "daily_budget,lifetime_budget,targeting,bid_amount,bid_strategy,bid_constraints,"
    "optimization_goal,billing_event,start_time,end_time,created_time,updated_time,"
    "attribution_spec,destination_type,promoted_object,pacing_type,budget_remaining,"
    "dsa_beneficiary,dsa_payor,is_dynamic_creative,placement_soft_opt_out,value_rule_set_id"
)


def _normalize_placement_soft_opt_out(
    placement_soft_opt_out: Optional[Dict[str, List[str]]],
) -> tuple[Optional[Dict[str, List[str]]], Optional[Dict[str, Any]]]:
    """Validate and normalize placement_soft_opt_out. Returns (normalized, error)."""
    if placement_soft_opt_out is None:
        return None, None
    if not isinstance(placement_soft_opt_out, dict):
        return None, {
            "error": "placement_soft_opt_out must be an object keyed by placement group",
            "example": {
                "facebook_positions": ["marketplace", "profile_feed"],
                "instagram_positions": ["stream"],
            },
            "allowed_keys": sorted(_PLACEMENT_SOFT_OPT_OUT_KEYS),
        }

    invalid_keys = [key for key in placement_soft_opt_out if key not in _PLACEMENT_SOFT_OPT_OUT_KEYS]
    if invalid_keys:
        return None, {
            "error": f"Invalid placement_soft_opt_out keys: {invalid_keys}",
            "allowed_keys": sorted(_PLACEMENT_SOFT_OPT_OUT_KEYS),
        }

    normalized: Dict[str, List[str]] = {}
    for key, values in placement_soft_opt_out.items():
        if not isinstance(values, list) or not values:
            return None, {
                "error": f"placement_soft_opt_out['{key}'] must be a non-empty list of placement strings",
            }
        stripped = [str(value).strip() for value in values if str(value).strip()]
        if not stripped:
            return None, {
                "error": f"placement_soft_opt_out['{key}'] must be a non-empty list of placement strings",
            }
        normalized[key] = stripped

    if not normalized:
        return None, {"error": "placement_soft_opt_out must include at least one placement group with values"}

    return normalized, None


def _validate_placement_soft_opt_out(
    placement_soft_opt_out: Optional[Dict[str, List[str]]],
) -> Optional[Dict[str, Any]]:
    _, error = _normalize_placement_soft_opt_out(placement_soft_opt_out)
    return error


def _encode_placement_soft_opt_out(placement_soft_opt_out: Dict[str, List[str]]) -> str:
    return json.dumps(placement_soft_opt_out)


def _strip_targeting_exclusions(targeting: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
    """Remove the deprecated detailed-targeting `exclusions` field (v22.0+).

    `excluded_custom_audiences` remains the supported channel for excluding
    custom audiences and is preserved here.
    """
    found = [k for k in _REJECTED_TARGETING_EXCLUSION_KEYS if k in targeting]
    if not found:
        return targeting, None
    cleaned = {k: v for k, v in targeting.items() if k not in _REJECTED_TARGETING_EXCLUSION_KEYS}
    warning = (
        f"Removed unsupported targeting keys {found}. "
        "Detailed-targeting `exclusions` are no longer accepted (v22.0+); "
        "use `excluded_custom_audiences` for custom-audience exclusions."
    )
    return cleaned, warning


def _strip_deprecated_facebook_positions(targeting: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
    """Drop placements removed in v24.0+ (e.g. `video_feeds`) and warn.

    Without this Meta returns an opaque "invalid placement" error on create/update.
    Spend would have been auto-shifted to other placements anyway.
    """
    positions = targeting.get("facebook_positions")
    if not isinstance(positions, list) or not positions:
        return targeting, None

    removed = [p for p in positions if str(p).strip().lower() in _DEPRECATED_FACEBOOK_POSITIONS]
    if not removed:
        return targeting, None

    kept = [p for p in positions if str(p).strip().lower() not in _DEPRECATED_FACEBOOK_POSITIONS]
    cleaned = dict(targeting)
    if kept:
        cleaned["facebook_positions"] = kept
    else:
        cleaned.pop("facebook_positions", None)

    warning = (
        f"Removed deprecated facebook_positions {removed} (v24.0+). "
        "Facebook video feeds placement was retired; Reels is the recommended replacement."
    )
    return cleaned, warning


def _combine_warnings(*warnings: Optional[str]) -> Optional[str]:
    parts = [w for w in warnings if w]
    if not parts:
        return None
    return " | ".join(parts)


def _normalize_targeting(targeting: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], Optional[str]]:
    if not targeting:
        return _default_targeting(), None

    normalized = dict(targeting)
    if "targeting_automation" not in normalized:
        normalized["targeting_automation"] = {"advantage_audience": 0}
    normalized, exclusion_warning = _strip_targeting_exclusions(normalized)
    normalized, placement_warning = _strip_deprecated_facebook_positions(normalized)
    return normalized, _combine_warnings(exclusion_warning, placement_warning)


def _money_problem(daily_budget: Any, lifetime_budget: Any, bid_amount: Any) -> Optional[str]:
    """ADS-60: budgets and bids in range, and not both kinds of budget at once."""
    problem = money.first_problem(
        money.amount_problem("daily_budget", daily_budget),
        money.amount_problem("lifetime_budget", lifetime_budget),
        money.amount_problem("bid_amount", bid_amount),
    )
    if problem:
        return problem
    if daily_budget is not None and lifetime_budget is not None:
        return "Pass daily_budget or lifetime_budget, not both. Nothing was changed."
    return None


async def _parent_campaign_bid_strategy(campaign_id: str, meta_access_token: str) -> Optional[str]:
    payload = await make_api_request(campaign_id, meta_access_token, {"fields": "bid_strategy"})
    if isinstance(payload, dict):
        strategy = payload.get("bid_strategy")
        if isinstance(strategy, str) and strategy:
            return strategy
    return None


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@meta_api_tool
async def list_ad_sets(
    ad_account_id: str,
    meta_access_token: Optional[str] = None,
    page_size: int = 10,
    campaign_id: str = "",
    page_cursor: str = "",
) -> str:
    """List ad sets under an account or campaign."""
    if not ad_account_id:
        return _json({"error": "No account ID specified"})

    target = campaign_id or ad_account_id
    endpoint = f"{target}/adsets"
    params: Dict[str, Any] = {
        "fields": _AD_SET_LIST_FIELDS,
        "page_size": int(page_size),
    }
    if page_cursor:
        params["page_cursor"] = page_cursor

    payload = await make_api_request(endpoint, meta_access_token, params)
    return _json(payload)


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@meta_api_tool
async def read_ad_set(ad_set_id: str, meta_access_token: Optional[str] = None) -> str:
    """Fetch full details for one ad set."""
    if not ad_set_id:
        return _json({"error": "No ad set ID provided"})

    payload = await make_api_request(
        ad_set_id,
        meta_access_token,
        {"fields": _AD_SET_READ_FIELDS},
    )

    if isinstance(payload, dict) and "frequency_control_specs" not in payload:
        payload["_meta"] = {
            "note": "No frequency_control_specs were returned. Either none are set or the API omitted the field."
        }

    return _json(payload)


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
@meta_api_tool
async def create_ad_set(
    ad_account_id: str,
    campaign_id: str,
    name: str,
    optimization_goal: str,
    billing_event: str,
    status: str = "PAUSED",
    daily_budget: Optional[int] = None,
    lifetime_budget: Optional[int] = None,
    targeting: Optional[Dict[str, Any]] = None,
    bid_amount: Optional[int] = None,
    bid_strategy: Optional[str] = None,
    bid_constraints: Optional[Dict[str, Any]] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    dsa_beneficiary: Optional[str] = None,
    promoted_object: Optional[Dict[str, Any]] = None,
    destination_type: Optional[str] = None,
    is_dynamic_creative: Optional[bool] = None,
    placement_soft_opt_out: Optional[Dict[str, List[str]]] = None,
    attribution_spec: Optional[List[Dict[str, Any]]] = None,
    frequency_control_specs: Optional[List[Dict[str, Any]]] = None,
    dsa_payor: Optional[str] = None,
    value_rule_set_id: Optional[str] = None,
    meta_access_token: Optional[str] = None,
) -> str:
    """Create an ad set under a campaign.

    Money: `daily_budget`, `lifetime_budget` and `bid_amount` are whole numbers in the ad
    account's minor currency units, as Graph takes them: cents for USD, EUR, GBP, CAD, AUD
    (1000 = 10.00), but whole units for currencies without cents such as JPY and KRW
    (1000 = 1,000 yen). read_ad_account shows the account's currency. Each must be more
    than 0. Pass one of daily_budget or lifetime_budget, and neither when the campaign
    holds the budget.

    `value_rule_set_id` attaches one of the account's value rule sets (from
    `list_value_rule_sets`) to the new ad set.

    `targeting` accepts the standard Meta targeting spec, including
    `excluded_custom_audiences` for audience exclusions. The legacy `exclusions`
    field (detailed-targeting exclusions) is deprecated v22.0+ and stripped
    automatically — use `excluded_custom_audiences` instead. Deprecated v24.0+
    placements (e.g. `facebook_positions: ["video_feeds"]`) are also stripped
    with a warning; use Reels placements instead.

    `placement_soft_opt_out` (Sales/Leads objectives only): object keyed by
    placement group with position lists. Up to 5% spend may flow to these
    otherwise-excluded placements when performance benefits. Example::

        {"facebook_positions": ["marketplace"], "instagram_positions": ["stream"]}
    """
    status, status_error = status_policy.create_status(status)
    if status_error:
        return _json({"error": status_error})
    if not ad_account_id:
        return _json({"error": "No account ID provided"})
    if not campaign_id:
        return _json({"error": "No campaign ID provided"})
    if not name:
        return _json({"error": "No ad set name provided"})
    if not optimization_goal:
        return _json({"error": "No optimization goal provided"})
    if not billing_event:
        return _json({"error": "No billing event provided"})

    app_error = _validate_promoted_object_for_app_installs(optimization_goal, promoted_object)
    if app_error:
        return _json(app_error)

    bid_error = _validate_bid_controls(bid_strategy, bid_amount, bid_constraints)
    if bid_error:
        return _json(bid_error)

    money_error = _money_problem(daily_budget, lifetime_budget, bid_amount)
    if money_error:
        return _json({"error": money_error})

    normalized_bid_strategy = _normalize_bid_strategy(bid_strategy)

    if bid_amount is None:
        try:
            parent_strategy = await _parent_campaign_bid_strategy(campaign_id, meta_access_token)
        except Exception:  # noqa: BLE001
            parent_strategy = None

        if parent_strategy in (_BID_STRATEGIES_REQUIRING_BID_AMOUNT | {"TARGET_COST"}):
            return _json(
                {
                    "error": (
                        f"bid_amount is required because the parent campaign uses bid_strategy "
                        f"'{parent_strategy}'"
                    ),
                    "details": f"Provide bid_amount ({money.UNITS}) or update the parent campaign's strategy.",
                    "example_with_bid_amount": {"bid_amount": 500},
                }
            )

    placement_normalized, placement_error = _normalize_placement_soft_opt_out(placement_soft_opt_out)
    if placement_error:
        return _json(placement_error)

    normalized_targeting, targeting_warning = _normalize_targeting(targeting)

    payload: Dict[str, Any] = {
        "name": name,
        "campaign_id": campaign_id,
        "status": status,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "targeting": json.dumps(normalized_targeting),
    }

    if daily_budget is not None:
        payload["daily_budget"] = str(daily_budget)
    if lifetime_budget is not None:
        payload["lifetime_budget"] = str(lifetime_budget)
    if bid_amount is not None:
        payload["bid_amount"] = str(bid_amount)
    if normalized_bid_strategy is not None:
        payload["bid_strategy"] = normalized_bid_strategy
    if bid_constraints is not None:
        payload["bid_constraints"] = json.dumps(bid_constraints)
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if dsa_beneficiary is not None:
        payload["dsa_beneficiary"] = dsa_beneficiary
    if dsa_payor is not None:
        payload["dsa_payor"] = dsa_payor
    if promoted_object is not None:
        payload["promoted_object"] = json.dumps(promoted_object)
    if destination_type is not None:
        payload["destination_type"] = destination_type
    if is_dynamic_creative is not None:
        payload["is_dynamic_creative"] = "true" if bool(is_dynamic_creative) else "false"
    if placement_normalized is not None:
        payload["placement_soft_opt_out"] = _encode_placement_soft_opt_out(placement_normalized)
    if attribution_spec is not None:
        payload["attribution_spec"] = json.dumps(attribution_spec)
    if frequency_control_specs is not None:
        payload["frequency_control_specs"] = frequency_control_specs
    if value_rule_set_id is not None and str(value_rule_set_id).strip():
        payload["value_rule_set_id"] = str(value_rule_set_id).strip()

    result = await make_api_request(f"{ad_account_id}/adsets", meta_access_token, payload, method="POST")

    if isinstance(result, dict) and result.get("error"):
        rendered_error = json.dumps(result.get("error", {}), default=str).lower()
        if "permission" in rendered_error or "insufficient" in rendered_error:
            return _json(
                {
                    "error": "Insufficient permissions to set DSA beneficiary. Please ensure business_management permissions.",
                    "details": result,
                    "permission_required": True,
                    **({"_warning": targeting_warning} if targeting_warning else {}),
                }
            )
        if "dsa_beneficiary" in rendered_error and ("not supported" in rendered_error or "parameter" in rendered_error):
            return _json(
                {
                    "error": "DSA beneficiary parameter not supported in this API version.",
                    "details": result,
                    "manual_setup_required": True,
                    **({"_warning": targeting_warning} if targeting_warning else {}),
                }
            )
        if "benefits from ads" in rendered_error or "dsa beneficiary" in rendered_error:
            return _json(
                {
                    "error": "DSA beneficiary required for European compliance.",
                    "details": result,
                    "dsa_required": True,
                    **({"_warning": targeting_warning} if targeting_warning else {}),
                }
            )

    if targeting_warning and isinstance(result, dict):
        result["_warning"] = targeting_warning
    return _json(result)


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
@meta_api_tool
async def update_ad_set(
    ad_set_id: str,
    frequency_control_specs: Optional[List[Dict[str, Any]]] = None,
    bid_strategy: Optional[str] = None,
    bid_amount: Optional[int] = None,
    bid_constraints: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    targeting: Optional[Dict[str, Any]] = None,
    optimization_goal: Optional[str] = None,
    daily_budget: Optional[int] = None,
    lifetime_budget: Optional[int] = None,
    is_dynamic_creative: Optional[bool] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    placement_soft_opt_out: Optional[Dict[str, List[str]]] = None,
    attribution_spec: Optional[List[Dict[str, Any]]] = None,
    promoted_object: Optional[Dict[str, Any]] = None,
    destination_type: Optional[str] = None,
    dsa_beneficiary: Optional[str] = None,
    dsa_payor: Optional[str] = None,
    value_rule_set_id: Optional[str] = _KEEP_VALUE_RULE_SET,
    confirm_budget_increase: Optional[bool] = None,
    meta_access_token: Optional[str] = None,
) -> str:
    """Update an ad set's delivery, budgeting, and targeting configuration.

    `value_rule_set_id`:
    - a value rule set ID (from `list_value_rule_sets`) attaches that set;
    - an empty string "" detaches the ad set's current value rule set;
    - leave it out (or pass "keep") to keep whatever is attached now;
    - `null` is refused. Graph treats null as "detach", but many clients send
      null for every optional field they aren't using, and a budget edit must
      never silently strip a value rule. Pass "" to detach.
    The result says what happened to the value rule set. Read the ad set back
    afterwards: `read_ad_set` returns `value_rule_set_id`.

    Money: `daily_budget`, `lifetime_budget` and `bid_amount` are whole numbers in the ad
    account's minor currency units (cents for USD, EUR, GBP, CAD, AUD, so 1000 = 10.00; whole
    units for JPY, KRW and other currencies without cents). read_ad_account shows the
    currency. Each must be more than 0. A budget change takes effect at once on a running ad
    set. Raising a budget to more than 3x its current value is refused unless
    `confirm_budget_increase=true`.
    """
    status, status_error = status_policy.update_status(status)
    if status_error:
        return _json({"error": status_error})
    if not ad_set_id:
        return _json({"error": "No ad set ID provided"})

    if value_rule_set_id is None:
        return _json(
            {
                "error": (
                    "value_rule_set_id is null. Nothing was changed. Pass \"\" to detach the "
                    "ad set's value rule set, a value rule set ID to attach one, or leave the "
                    "field out (or pass \"keep\") to keep the current one."
                )
            }
        )

    placement_normalized, placement_error = _normalize_placement_soft_opt_out(placement_soft_opt_out)
    if placement_error:
        return _json(placement_error)

    bid_error = _validate_bid_controls(bid_strategy, bid_amount, bid_constraints)
    if bid_error:
        return _json(bid_error)

    money_error = _money_problem(daily_budget, lifetime_budget, bid_amount)
    if money_error:
        return _json({"error": money_error})

    new_budgets = {k: v for k, v in (("daily_budget", daily_budget), ("lifetime_budget", lifetime_budget)) if v is not None}
    increase_check: Optional[str] = None
    if new_budgets and not confirm_budget_increase:
        current = await make_api_request(ad_set_id, meta_access_token, {"fields": "daily_budget,lifetime_budget"})
        if isinstance(current, dict) and "error" not in current:
            increase_error = money.increase_problems(current, new_budgets, bool(confirm_budget_increase))
            if increase_error:
                return _json({"error": increase_error})
            increase_check = "passed"
        else:
            increase_check = "skipped: the current budget couldn't be read"

    if optimization_goal == "APP_INSTALLS":
        app_error = _validate_promoted_object_for_app_installs(optimization_goal, promoted_object)
        if app_error:
            return _json(app_error)

    payload: Dict[str, Any] = {}

    if frequency_control_specs is not None:
        payload["frequency_control_specs"] = frequency_control_specs

    normalized_bid_strategy = _normalize_bid_strategy(bid_strategy)
    if normalized_bid_strategy is not None:
        payload["bid_strategy"] = normalized_bid_strategy

    if bid_amount is not None:
        payload["bid_amount"] = str(bid_amount)
    if bid_constraints is not None:
        payload["bid_constraints"] = json.dumps(bid_constraints)
    if status is not None:
        payload["status"] = status
    update_targeting_warning: Optional[str] = None
    if targeting is not None:
        cleaned_targeting, exclusion_warning = _strip_targeting_exclusions(targeting)
        cleaned_targeting, placement_warning = _strip_deprecated_facebook_positions(cleaned_targeting)
        update_targeting_warning = _combine_warnings(exclusion_warning, placement_warning)
        payload["targeting"] = json.dumps(cleaned_targeting)
    if optimization_goal is not None:
        payload["optimization_goal"] = optimization_goal
    if daily_budget is not None:
        payload["daily_budget"] = str(daily_budget)
    if lifetime_budget is not None:
        payload["lifetime_budget"] = str(lifetime_budget)
    # is_dynamic_creative is a CREATE-time-only attribute on the Ad Set node. Meta
    # rejects it on an existing ad set and would fail the whole update, so we drop
    # it here and warn rather than send it. Set it only via create_ad_set.
    dropped_fields_warning: Optional[str] = None
    if is_dynamic_creative is not None:
        dropped_fields_warning = (
            "is_dynamic_creative cannot be changed after ad set creation; it was ignored on update. "
            "Create a new ad set with create_ad_set(is_dynamic_creative=...) to use Dynamic Creative."
        )
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if placement_soft_opt_out is not None and placement_normalized is not None:
        payload["placement_soft_opt_out"] = _encode_placement_soft_opt_out(placement_normalized)
    if attribution_spec is not None:
        payload["attribution_spec"] = json.dumps(attribution_spec)
    if promoted_object is not None:
        payload["promoted_object"] = json.dumps(promoted_object)
    if destination_type is not None:
        payload["destination_type"] = destination_type
    if dsa_beneficiary is not None:
        payload["dsa_beneficiary"] = dsa_beneficiary
    if dsa_payor is not None:
        payload["dsa_payor"] = dsa_payor
    if str(value_rule_set_id).strip() != _KEEP_VALUE_RULE_SET:
        # An ID attaches; an empty string detaches (verified against the live API).
        payload["value_rule_set_id"] = str(value_rule_set_id).strip()

    if not payload:
        # If the only thing the caller passed was a dropped create-only field
        # (is_dynamic_creative), surface that warning rather than a generic error.
        if dropped_fields_warning:
            # Dict error so the tool envelope keeps `warnings` at the top level.
            return _json({"error": {"message": "No applicable update parameters provided"}, "warnings": [dropped_fields_warning]})
        return _json({"error": "No update parameters provided"})

    try:
        result = await make_api_request(ad_set_id, meta_access_token, payload, method="POST")
        if isinstance(result, dict):
            if update_targeting_warning:
                result["_warning"] = update_targeting_warning
            if "value_rule_set_id" in payload and not result.get("error"):
                # Say what happened, so a detach is never silent in the reply.
                sent = payload["value_rule_set_id"]
                result["value_rule_set"] = {"detached": True} if sent == "" else {"attached": sent}
            if increase_check:
                result["budget_increase_check"] = increase_check
            if dropped_fields_warning:
                existing = result.get("warnings")
                result["warnings"] = (
                    existing + [dropped_fields_warning] if isinstance(existing, list) else [dropped_fields_warning]
                )
        return _json(result)
    except Exception as exc:  # noqa: BLE001
        return _json(
            {
                "error": f"Failed to update ad set {ad_set_id}",
                "details": str(exc),
                "params_sent": payload,
            }
        )
