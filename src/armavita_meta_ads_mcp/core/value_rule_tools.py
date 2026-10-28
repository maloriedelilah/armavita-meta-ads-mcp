# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""Value rule sets — read the account's value rules (Ads Manager "value rules")."""

import json
from typing import Any, Dict, Optional

from .graph_client import make_api_request, meta_api_tool
from .mcp_runtime import mcp_server
from mcp.types import ToolAnnotations

_DEFAULT_FIELDS = "id,name,status,product_type,is_default_setting"
_STATUSES = {"ACTIVE", "DELETED", "DRAFT"}
_PRODUCT_TYPES = {"AUDIENCE", "LEADGEN_ADS", "OMNI_CHANNEL"}


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2)


def _normalize_account(ad_account_id: str) -> str:
    value = str(ad_account_id or "").strip()
    if not value:
        return ""
    return value if value.startswith("act_") else f"act_{value}"


def _normalize_enum(value: Optional[str], allowed: set[str], name: str) -> tuple[Optional[str], Optional[str]]:
    if value is None or str(value).strip() == "":
        return None, None
    normalized = str(value).strip().upper()
    if normalized not in allowed:
        return None, f"Invalid {name} '{value}'. Use one of: {', '.join(sorted(allowed))}."
    return normalized, None


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@meta_api_tool
async def list_value_rule_sets(
    ad_account_id: str,
    meta_access_token: Optional[str] = None,
    status: Optional[str] = None,
    product_type: Optional[str] = None,
    page_size: int = 25,
    page_cursor: str = "",
    fields: Optional[str] = None,
) -> str:
    """List the value rule sets defined on an ad account (`act_<id>/value_rule_set`).

    Value rules are the bid adjustments set in Ads Manager (e.g. "bid more for
    women 25-64"). Use the returned `id` with `read_value_rule_set` to see the
    rules inside a set.

    Optional filters: `status` (ACTIVE, DELETED, DRAFT) and `product_type`
    (AUDIENCE, LEADGEN_ADS, OMNI_CHANNEL).
    """
    normalized = _normalize_account(ad_account_id)
    if not normalized:
        return _json({"error": "ad_account_id is required"})

    status_value, status_error = _normalize_enum(status, _STATUSES, "status")
    if status_error:
        return _json({"error": status_error})
    product_value, product_error = _normalize_enum(product_type, _PRODUCT_TYPES, "product_type")
    if product_error:
        return _json({"error": product_error})

    params: Dict[str, Any] = {
        "fields": fields or _DEFAULT_FIELDS,
        "page_size": int(page_size),
    }
    if page_cursor:
        params["page_cursor"] = page_cursor
    if status_value:
        params["status"] = status_value
    if product_value:
        params["product_type"] = product_value

    payload = await make_api_request(f"{normalized}/value_rule_set", meta_access_token, params)
    return _json(payload)


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
@meta_api_tool
async def read_value_rule_set(
    rule_set_id: str,
    meta_access_token: Optional[str] = None,
    include_rules: bool = True,
    fields: Optional[str] = None,
) -> str:
    """Read one value rule set by ID, and by default the rules inside it.

    With `include_rules` (default True) the set's `rules` edge is fetched too and
    returned under `rules`. Meta does not publish a stable field list for that
    edge, so it is returned as Meta sends it.
    """
    rule_set_id = str(rule_set_id or "").strip()
    if not rule_set_id:
        return _json({"error": "rule_set_id is required"})

    payload = await make_api_request(
        rule_set_id,
        meta_access_token,
        {"fields": fields or _DEFAULT_FIELDS},
    )
    if not include_rules or (isinstance(payload, dict) and "error" in payload):
        return _json(payload)

    rules = await make_api_request(f"{rule_set_id}/rules", meta_access_token, {})
    result = dict(payload) if isinstance(payload, dict) else {"rule_set": payload}
    result["rules"] = rules
    return _json(result)
