"""Turning a weekday pattern into dates.

A Wrike work schedule says *which days of the week* a person works. The
availability query needs *dates*. This module is the bridge: given the working
weekdays and a date range, it lists the concrete dates on which the person does
not work.

Deliberately out of scope for now, and both easy to add later:

* **Weekends.** Saturday and Sunday are never emitted. They are non-working for
  practically everyone, so writing them would multiply the collection size for
  no decision-making value, and the API skips weekend evaluation anyway.
* **Public holidays and leave.** Wrike keeps these as schedule exceptions
  (``/workschedules/{id}/workschedule_exclusions``). They are not read. To add
  them later, pass the exception dates into ``expand_non_working_dates`` as
  ``extra_dates`` — the plumbing already accepts them.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from typing import Iterable, Iterator, List, Optional, Set

from weekdays import BUSINESS_WEEK, SATURDAY


def iter_dates(date_from: Date, date_to: Date) -> Iterator[Date]:
    """Yield every date from ``date_from`` to ``date_to`` inclusive."""
    if date_to < date_from:
        raise ValueError("date_to must not be earlier than date_from.")
    current = date_from
    while current <= date_to:
        yield current
        current += timedelta(days=1)


def is_weekend(value: Date) -> bool:
    return value.weekday() >= SATURDAY


def expand_non_working_dates(
    working_weekdays: Set[int],
    date_from: Date,
    date_to: Date,
    extra_dates: Optional[Iterable[Date]] = None,
) -> List[Date]:
    """List the dates in the range on which someone is not working.

    A date is included when it is a **weekday** (Monday to Friday) that is not
    one of ``working_weekdays``. Weekends are always excluded.

    ``extra_dates`` is the hook for holidays and leave: any of those dates that
    falls on a weekday inside the range is included too. It is unused today.

    Example: a Monday-to-Thursday schedule over one week yields just the Friday.
    """
    if not working_weekdays:
        # An empty pattern almost always means the source data was misread.
        # Emitting "off every weekday" would flood Wrike with comments.
        raise ValueError(
            "working_weekdays is empty: refusing to mark every weekday as non-working."
        )

    unknown = working_weekdays - set(range(7))
    if unknown:
        raise ValueError(f"working_weekdays contains invalid values: {sorted(unknown)}")

    extras = {d for d in (extra_dates or ())}

    dates = [
        current
        for current in iter_dates(date_from, date_to)
        if not is_weekend(current)
        and (current.weekday() not in working_weekdays or current in extras)
    ]
    return dates


def non_working_weekdays(working_weekdays: Set[int]) -> Set[int]:
    """The Monday-to-Friday weekdays a person does not work.

    Useful for logging what a schedule actually means before expanding it.
    """
    return BUSINESS_WEEK - set(working_weekdays)
