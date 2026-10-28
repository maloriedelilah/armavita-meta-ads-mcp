# SPDX-License-Identifier: AGPL-3.0-only

"""What status a campaign, ad set or ad may be given through this server (ADS-54).

Malorie's rules (Oct 28):
1. Agents build everything PAUSED. Only after the user has reviewed it may an
   agent activate it.
2. Nothing is deleted or archived through this server.

So creates and clones always produce PAUSED objects, updates may switch
between ACTIVE and PAUSED, and DELETED / ARCHIVED are never sent.
"""

from typing import Any, Optional, Tuple

PAUSED = "PAUSED"
_UPDATE_STATUSES = {"ACTIVE", "PAUSED"}

CREATE_MUST_BE_PAUSED = (
    "New campaigns, ad sets and ads (and copies) are always created PAUSED. "
    "Leave status out (or pass PAUSED). After the user has reviewed it, activate it "
    "with the matching update tool (status=ACTIVE)."
)

NO_DELETE_OR_ARCHIVE = (
    "This server never deletes or archives anything. Pause it instead (status=PAUSED); "
    "deleting or archiving is done by the account owner in Ads Manager."
)

UPDATE_STATUS_INVALID = "status must be ACTIVE or PAUSED. Leave it out (or pass \"\") to keep the current status."


def create_status(status: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return (status_to_send, error). Missing, null, "" and PAUSED all mean PAUSED."""
    text = "" if status is None else str(status).strip()
    if text == "" or text.upper() == PAUSED:
        return PAUSED, None
    return None, CREATE_MUST_BE_PAUSED


def update_status(status: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return (status_to_send, error). None means "don't change the status".

    Missing, null and "" all mean "leave the status as it is": strict-schema
    clients send "" or null for fields they aren't using (ADS-66).
    """
    if status is None:
        return None, None
    text = str(status).strip().upper()
    if text == "":
        return None, None
    if text in ("DELETED", "ARCHIVED"):
        return None, NO_DELETE_OR_ARCHIVE
    if text not in _UPDATE_STATUSES:
        return None, UPDATE_STATUS_INVALID
    return text, None
