"""Weekday parsing and naming.

Wrike work schedules describe a *pattern* ("works Monday to Thursday"), not
dates. Everything downstream needs weekdays as Python integers, where
Monday is 0 and Sunday is 6, so all the messy input forms are normalised here.
"""

from __future__ import annotations

from typing import Iterable, Set, Union

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

BUSINESS_WEEK: Set[int] = {MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY}

# Every spelling seen in schedule data: full names, three- and two-letter
# abbreviations, and the ISO numbers 1 (Monday) to 7 (Sunday).
_LOOKUP = {}
for _index, _name in enumerate(WEEKDAY_NAMES):
    _LOOKUP[_name.lower()] = _index
    _LOOKUP[_name.lower()[:3]] = _index
    _LOOKUP[_name.lower()[:2]] = _index
    _LOOKUP[str(_index + 1)] = _index  # ISO: 1 = Monday
_LOOKUP["thur"] = THURSDAY
_LOOKUP["thurs"] = THURSDAY
_LOOKUP["tues"] = TUESDAY


class UnknownWeekdayError(ValueError):
    """Raised when a weekday value cannot be interpreted."""


def parse_weekday(value: Union[str, int]) -> int:
    """Normalise one weekday to a Python weekday index (Monday = 0).

    Accepts ``"Monday"``, ``"mon"``, ``"MO"``, ``"1"`` and ``1``. Integers are
    read as ISO weekdays (1 = Monday ... 7 = Sunday), which is what schedule
    APIs normally emit; a bare ``0`` is therefore rejected rather than guessed at.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a weekday
        raise UnknownWeekdayError(f"Not a weekday: {value!r}")
    key = str(value).strip().lower().rstrip(".")
    if key in _LOOKUP:
        return _LOOKUP[key]
    raise UnknownWeekdayError(
        f"Not a recognised weekday: {value!r}. "
        "Use a day name, a three-letter abbreviation, or an ISO number 1-7."
    )


def parse_weekdays(values: Iterable[Union[str, int]]) -> Set[int]:
    """Normalise a collection of weekdays, ignoring duplicates."""
    return {parse_weekday(value) for value in values}


def weekday_name(index: int) -> str:
    return WEEKDAY_NAMES[index]


def format_weekdays(indexes: Iterable[int]) -> str:
    """Render weekdays in calendar order, for logs and CLI output."""
    return ", ".join(WEEKDAY_NAMES[i] for i in sorted(set(indexes)))
