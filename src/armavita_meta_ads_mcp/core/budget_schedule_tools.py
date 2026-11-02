# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

"""Campaign budget schedule tooling."""


import json
import time
from typing import Optional

from . import money
from .graph_client import make_api_request, meta_api_tool
from .mcp_runtime import mcp_server
from mcp.types import ToolAnnotations

_ALLOWED_BUDGET_VALUE_TYPES = {"ABSOLUTE", "MULTIPLIER"}
# Meta: a high-demand period lasts at least 3 hours.
_MIN_WINDOW_SECONDS = 3 * 3600
# A start a little in the past is clock skew; more than this would raise the budget at once.
_START_GRACE_SECONDS = 300
# Unix seconds stay below this until the year 5138; a millisecond timestamp is far above it.
_MAX_UNIX_SECONDS = 10**11


def _window_problem(time_start, time_end, now: float) -> Optional[str]:
    for name, value in (("time_start", time_start), ("time_end", time_end)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return f"{name} must be a Unix time in seconds (a whole number)."
        if value >= _MAX_UNIX_SECONDS:
            return f"{name}={value} looks like milliseconds; pass Unix seconds."
    if time_end <= time_start:
        return "time_end must be after time_start. Nothing was created."
    if time_end - time_start < _MIN_WINDOW_SECONDS:
        return "A budget schedule must last at least 3 hours (Meta's minimum). Nothing was created."
    if time_start < now - _START_GRACE_SECONDS:
        return (
            "time_start is in the past. A schedule starting in the past would raise the budget the "
            "moment it's created. Pass a future time_start. Nothing was created."
        )
    return None


@mcp_server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
@meta_api_tool
async def create_campaign_budget_schedule(
    campaign_id: str,
    budget_value: int,
    budget_value_type: str,
    time_start: int,
    time_end: int,
    meta_access_token: Optional[str] = None,
) -> str:
    """Create a high-demand budget schedule for a campaign (only for daily budgets).

    The schedule is live as soon as it's created: there is no paused state, so the budget
    rises automatically at `time_start` and returns at `time_end`, whatever the campaign's
    other settings. `time_start` and `time_end` are Unix times in seconds; the window must be
    at least 3 hours and start in the future.

    `budget_value_type`:
    - ABSOLUTE: `budget_value` is the increase, in the ad account's minor currency units
      (cents for USD, EUR, GBP, CAD, AUD, so 2000 = 20.00; whole units for JPY, KRW and other
      currencies without cents). read_ad_account shows the account's currency.
    - MULTIPLIER: `budget_value` is the increase as a percentage of the daily budget.
    Either way it must be more than 0, and Meta caps the total at 8x the daily budget.
    """
    if not campaign_id:
        return json.dumps({"error": "A campaign ID must be provided"}, indent=2)
    if budget_value is None:
        return json.dumps({"error": "No budget_value was provided"}, indent=2)
    if time_start is None or time_end is None:
        return json.dumps({"error": "time_start and time_end are required"}, indent=2)
    value_error = money.amount_problem("budget_value", budget_value)
    if value_error:
        return json.dumps({"error": value_error}, indent=2)
    window_error = _window_problem(time_start, time_end, time.time())
    if window_error:
        return json.dumps({"error": window_error}, indent=2)

    normalized_type = str(budget_value_type or "").upper().strip()
    if normalized_type not in _ALLOWED_BUDGET_VALUE_TYPES:
        return json.dumps(
            {
                "error": "Invalid budget_value_type",
                "details": "budget_value_type must be ABSOLUTE or MULTIPLIER",
            },
            indent=2,
        )

    payload = {
        "budget_value": budget_value,
        "budget_value_type": normalized_type,
        "time_start": time_start,
        "time_end": time_end,
    }

    result = await make_api_request(f"{campaign_id}/budget_schedules", meta_access_token, payload, method="POST")
    return json.dumps(result, indent=2)
