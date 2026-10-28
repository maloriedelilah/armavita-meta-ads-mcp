# SPDX-License-Identifier: AGPL-3.0-only

"""Keep caller-supplied IDs from steering Graph requests (ADS-53).

Every tool builds its Graph path from IDs it is given (``f"{ad_id}/copies"``),
and httpx turns ``?``, ``#``, ``&`` and ``..`` in that path into a different
request: ``update_ad(ad_id="321?status=ACTIVE", bid_amount=50)`` would POST
``/321?status=ACTIVE``, and ``custom_audience_id="77#"`` would DELETE the whole
audience instead of its users. An agent can be talked into passing such a value
by text it reads (ad copy, research results, page content).

Two layers, in both stdio and HTTP mode:
- ``argument_problem`` checks a tool's ID arguments before the tool runs
  (applied by ``meta_api_tool``);
- ``endpoint_problem`` checks every Graph path ``make_api_request`` is about to
  call, so an ID that reaches a path some other way is still stopped.
"""

import re
from typing import Any, Mapping, Optional

# One Graph path segment: a node ID, act_<id>, "me", or an edge name such as "adsets".
_SEGMENT = re.compile(r"^[A-Za-z0-9_]+$")
_ACCOUNT_ID = re.compile(r"^(act_)?\d+$")
_USER_ID = re.compile(r"^(me|\d+)$")

# The literal "keep" means "leave the current value rule set" on update_ad_set (ADS-38).
_ID_KEYWORDS = {"value_rule_set_id": {"keep"}}

SMUGGLED_TOKEN = (
    "A tool argument contains 'access_token'. Tokens are never accepted inside arguments, "
    "and token fields are not returned. Remove it and call the tool again."
)


def _id_problem(name: str, value: str) -> Optional[str]:
    lowered = name.lower()
    if value in _ID_KEYWORDS.get(lowered, set()):
        return None
    if lowered == "ad_account_id":
        if not _ACCOUNT_ID.match(value):
            return f"{name} must be an ad account ID like act_1234567890 (or just the digits)."
        return None
    if lowered == "meta_user_id":
        if not _USER_ID.match(value):
            return f"{name} must be 'me' or a numeric user ID."
        return None
    if not _SEGMENT.match(value):
        return (
            f"{name} must be a plain Meta ID (letters, digits and '_' only). "
            "IDs are used as one URL path segment, so '/', '?', '#', '&', '.' and spaces are refused."
        )
    return None


def _contains_token_text(value: Any) -> bool:
    if isinstance(value, str):
        return "access_token" in value.lower()
    if isinstance(value, Mapping):
        return any(_contains_token_text(k) or _contains_token_text(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_token_text(item) for item in value)
    return False


def argument_problem(arguments: Mapping[str, Any]) -> Optional[str]:
    """Return a refusal message for unsafe tool arguments, or None if they're fine.

    - ``*_id`` arguments: one plain path segment (``ad_account_id``: act_<digits>
      or digits; ``meta_user_id``: me or digits).
    - ``*_ids`` arguments: every item follows the same rule.
    - Any argument other than ``meta_access_token`` itself that mentions
      ``access_token`` (for example ``fields="name,access_token"``, which would
      return Page tokens) is refused.
    Empty and missing values are left to the tool's own checks.
    """
    for name, value in arguments.items():
        if name == "meta_access_token" or value is None:
            continue
        if _contains_token_text(value):
            return SMUGGLED_TOKEN
        lowered = name.lower()
        if lowered.endswith("_id"):
            text = str(value).strip()
            if text:
                problem = _id_problem(name, text)
                if problem:
                    return problem
        elif lowered.endswith("_ids") and isinstance(value, (list, tuple)):
            for item in value:
                text = str(item).strip() if item is not None else ""
                if text:
                    problem = _id_problem(name[:-1], text)
                    if problem:
                        return problem
    return None


def endpoint_problem(endpoint: Any) -> Optional[str]:
    """Return a refusal message if a Graph path isn't plain segments joined by '/'."""
    if not isinstance(endpoint, str) or not endpoint:
        return "The Graph path is empty."
    for segment in endpoint.split("/"):
        if not _SEGMENT.match(segment):
            return (
                "Refused to call Meta: the Graph path contains characters an ID can't have "
                "('/', '?', '#', '&', '.', spaces or an empty segment)."
            )
    return None
