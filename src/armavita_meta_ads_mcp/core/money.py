# SPDX-License-Identifier: AGPL-3.0-only

"""Budget and bid amounts: units, ranges and big jumps (ADS-60).

Every budget and bid the tools send is in the ad account's MINOR currency units, the way
Graph takes them: cents for USD/EUR/GBP/CAD/AUD (1000 = 10.00), but whole yen for JPY and
whole won for KRW, which have no minor unit (1000 = 1,000 yen). read_ad_account returns the
account's `currency`. An agent that "multiplies by 100" on a JPY account spends 100x, so the
units are stated on every money parameter and amounts are checked here before anything is
sent.
"""

from typing import Any, Optional

UNITS = (
    "in the ad account's minor currency units, as Graph takes them: cents for USD, EUR, GBP, CAD, "
    "AUD (1000 = 10.00), but whole units for currencies without cents such as JPY and KRW "
    "(1000 = 1,000 yen). read_ad_account shows the account's currency."
)

# A change to a live budget above this multiple of its current value needs
# confirm_budget_increase=true (the 100x mistake is the one this catches).
MAX_UNCONFIRMED_INCREASE = 3

# Graph's amounts are 64-bit; anything near that is a unit or typing mistake, never a budget.
_ABSURD = 10**12

# Graph's documented value for "no spend cap" (it removes an account or campaign spend_cap).
SPEND_CAP_NONE = 922337203685478


def amount_problem(name: str, value: Any, allow_zero: bool = False) -> Optional[str]:
    """Why `value` can't be sent as the money field `name`, or None. None means not given."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{name} must be a whole number {UNITS}"
    if value < 0 or (value == 0 and not allow_zero):
        return f"{name} must be more than 0 ({name}={value} was given). Nothing was changed."
    if value >= _ABSURD:
        return f"{name}={value} is far beyond any real budget; check the units: {name} is {UNITS}"
    return None


def spend_cap_problem(value: Any) -> Optional[str]:
    """spend_cap: a positive amount, or SPEND_CAP_NONE to remove the cap."""
    if value == SPEND_CAP_NONE and not isinstance(value, bool):
        return None
    return amount_problem("spend_cap", value)


def first_problem(*problems: Optional[str]) -> Optional[str]:
    return next((p for p in problems if p), None)


def increase_problems(current: Any, new: dict, confirmed: bool) -> Optional[str]:
    """increase_problem for each of {"daily_budget": n, "lifetime_budget": m} against a Graph read."""
    if not isinstance(current, dict):
        return None
    return first_problem(*(increase_problem(k, current.get(k), v, confirmed) for k, v in new.items()))


def increase_problem(name: str, current: Any, new: Optional[int], confirmed: bool) -> Optional[str]:
    """A budget raised more than MAX_UNCONFIRMED_INCREASE times its current value, unconfirmed."""
    if new is None or confirmed:
        return None
    try:
        current_value = int(str(current))
    except (TypeError, ValueError):
        return None  # no current budget of this kind (e.g. switching daily to lifetime): nothing to compare
    if current_value <= 0 or new <= current_value * MAX_UNCONFIRMED_INCREASE:
        return None
    return (
        f"{name} would go from {current_value} to {new}, more than {MAX_UNCONFIRMED_INCREASE}x its current value. "
        f"Nothing was changed. Check the units ({name} is {UNITS}); if the increase is intended, call again with "
        "confirm_budget_increase=true."
    )
