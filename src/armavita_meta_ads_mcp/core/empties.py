# SPDX-License-Identifier: AGPL-3.0-only

"""One way to clear a field: `send_empty` (ADS-96, ADS-61).

Some clients fill every unused field with "", [] or {}. Graph takes those as real values, so
a budget edit could also clear frequency caps, the attribution window or a value rule set. The
MCP layer therefore treats an empty value as "not given" (mcp_runtime). To really clear a
field, the caller sends it empty *and* names it in `send_empty`:

    update_ad_set(ad_set_id=..., frequency_control_specs=[], send_empty=["frequency_control_specs"])

Each tool lists the fields it can clear. A name that isn't one of them, or one listed without
an empty value, is refused before anything reaches Meta, and the reply says what was cleared
(`sent_empty`). This is the only clearing mechanism, for every field, on purpose (Malorie,
Nov 2: one paradigm, to avoid confusion).
"""

from typing import Any, Dict, Iterable, List, Optional

SEND_EMPTY_DOC = (
    "`send_empty`: to clear a field, send it empty (\"\", [] or {}) AND list its name here. An empty "
    "value that isn't listed means \"not given\" and changes nothing. Fields this tool can clear: {fields}."
)


def is_empty(value: Any) -> bool:
    """"", spaces, [] or {}: what a padding client sends for a field it isn't using."""
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, (list, dict)) and len(value) == 0


def requested(send_empty: Any) -> List[str]:
    """The names in a send_empty argument (empty for None, [] or anything malformed)."""
    if not isinstance(send_empty, list):
        return []
    return [name for name in send_empty if isinstance(name, str) and name.strip()]


def problem(send_empty: Optional[List[str]], clearable: Iterable[str], given: Dict[str, Any]) -> Optional[str]:
    """Why this send_empty can't be honoured, or None. `given` maps each clearable field to its value."""
    if send_empty is None:
        return None
    if not isinstance(send_empty, list) or not all(isinstance(n, str) for n in send_empty):
        return "send_empty must be a list of field names. Nothing was changed."
    allowed = set(clearable)
    for name in requested(send_empty):
        if name not in allowed:
            return (
                f"send_empty names {name!r}, which this tool can't clear. It can clear: "
                f"{', '.join(sorted(allowed))}. Nothing was changed."
            )
        if name not in given or given[name] is None or not is_empty(given[name]):
            return (
                f"send_empty names {name!r}, so send {name} empty (\"\", [] or {{}}) in the same call. "
                "Nothing was changed."
            )
    return None


def doc(clearable: Iterable[str]) -> str:
    return SEND_EMPTY_DOC.format(fields=", ".join(sorted(clearable)))
