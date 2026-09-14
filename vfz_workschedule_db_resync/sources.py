"""Where the weekday patterns come from.

Two sources, one interface:

* ``JsonScheduleSource`` reads a local file. It works today and is the right
  choice while the schedules are few and stable.
* ``WrikeScheduleSource`` reads ``GET /workschedules?fields=["userIds"]``.

A caution on the Wrike source: Wrike's public API reference documents the work
schedule endpoints but not the JSON field that carries the working days of the
week, so the parser here accepts the shapes such APIs commonly use and raises a
clear error rather than guessing when it sees something else. Run

    python run_resync.py --dump-raw

once against the real account to see the actual payload, then, if needed, add
its key to ``_WORKWEEK_KEYS`` or ``_DAY_KEYS`` below. That is a one-line change.

HTTP itself is not done here: both sources take a ``WrikeClient`` from
``shared/wrike_helpers.py``, so the retry policy, the batching and the contact
cache are the same ones the Emails job uses.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from wrike_helpers import WrikeClient

from weekdays import UnknownWeekdayError, format_weekdays, parse_weekday


DEFAULT_SCHEDULE_TYPE = "Default"


@dataclass
class ScheduleAssignment:
    """One user, and the weekdays they normally work."""

    user_id: str
    user: Optional[str]
    work_schedule_id: str
    work_schedule_title: Optional[str]
    working_weekdays: Set[int] = field(default_factory=set)
    schedule_type: Optional[str] = None

    @property
    def is_default_schedule(self) -> bool:
        return (self.schedule_type or "").lower() == DEFAULT_SCHEDULE_TYPE.lower()

    def describe(self) -> str:
        return (
            f"{self.user or self.user_id} ({self.work_schedule_title or self.work_schedule_id}): "
            f"works {format_weekdays(self.working_weekdays)}"
        )


class ScheduleSource(Protocol):
    problems: List[str]
    unresolved_user_ids: List[str]

    def assignments(self) -> List[ScheduleAssignment]: ...


def dedupe_assignments(
    assignments: List[ScheduleAssignment],
) -> tuple[List[ScheduleAssignment], List[str]]:
    """Keep one assignment per user, preferring a custom schedule over the default.

    This matters: the populator keys its writes on user and date, and it deletes
    a user's in-window documents that its own expansion did not produce. Two
    assignments for one user would therefore undo each other, and whichever ran
    last would win silently. In the observed Wrike account a user assigned to a
    custom schedule is not also listed on the Default Schedule, so this is a
    guard rather than a routine correction — but a silent wrong answer here
    means a wrong comment on a live task.
    """
    chosen: Dict[str, ScheduleAssignment] = {}
    warnings: List[str] = []

    for assignment in assignments:
        existing = chosen.get(assignment.user_id)
        if existing is None:
            chosen[assignment.user_id] = assignment
            continue

        if existing.is_default_schedule and not assignment.is_default_schedule:
            winner, loser = assignment, existing
        elif assignment.is_default_schedule and not existing.is_default_schedule:
            winner, loser = existing, assignment
        else:
            winner, loser = existing, assignment
            warnings.append(
                f"{assignment.user or assignment.user_id} appears on two schedules of "
                f"the same type ('{existing.work_schedule_title}' and "
                f"'{assignment.work_schedule_title}'); using "
                f"'{winner.work_schedule_title}'. Resolve this in Wrike."
            )
            chosen[assignment.user_id] = winner
            continue

        chosen[assignment.user_id] = winner

    return list(chosen.values()), warnings


# --------------------------------------------------------------------------- #
# Local file source
# --------------------------------------------------------------------------- #
class JsonScheduleSource:
    """Reads schedules and their members from a JSON file.

    See ``schedules.example.json`` for the format.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self.problems: List[str] = []
        # A JSON schedule with an empty/unreadable `workingDays` still produces
        # an assignment (with `working_weekdays=set()`); it never drops a user
        # outright the way an unparseable Wrike schedule does, so this source
        # never populates unresolved_user_ids. Kept for ScheduleSource parity.
        self.unresolved_user_ids: List[str] = []

    def assignments(self) -> List[ScheduleAssignment]:
        self.problems = []
        if not self._path.exists():
            raise FileNotFoundError(f"Schedule file not found: {self._path}")

        data = json.loads(self._path.read_text(encoding="utf-8"))
        schedules = data.get("schedules", data if isinstance(data, list) else [])

        results: List[ScheduleAssignment] = []
        for schedule in schedules:
            weekdays = {parse_weekday(day) for day in schedule.get("workingDays", [])}
            for member in schedule.get("users", []):
                results.append(
                    ScheduleAssignment(
                        user_id=member["userId"],
                        user=member.get("user"),
                        work_schedule_id=schedule["workScheduleId"],
                        work_schedule_title=schedule.get("workScheduleTitle"),
                        working_weekdays=set(weekdays),
                        schedule_type=schedule.get("scheduleType"),
                    )
                )

        results, warnings = dedupe_assignments(results)
        self.problems.extend(warnings)
        return results


# --------------------------------------------------------------------------- #
# Wrike API source
# --------------------------------------------------------------------------- #
# Keys that carry the weekly pattern. Wrike uses "workweek".
_WORKWEEK_KEYS = ("workweek", "workWeek", "workingDays", "days", "weekDays")
# Inside a workweek block, the key holding the list of worked days.
# Wrike uses "workDays": ["Mon", "Tue", ...].
_WORKDAYS_KEYS = ("workDays", "workdays", "days")
# Within a per-day object, the key naming a single day.
_DAY_KEYS = ("day", "dayOfWeek", "weekday", "name")
# Within a block or per-day object, the key saying whether it is worked.
_WORKING_KEYS = ("working", "isWorking", "enabled", "workDay")
# Capacity keys. Wrike uses "capacityMinutes"; zero means the block is not worked.
_HOURS_KEYS = (
    "capacityMinutes",
    "hours",
    "capacity",
    "duration",
    "workingHours",
    "minutes",
)


class WrikeScheduleSource:
    """Reads work schedules and their members from the Wrike API."""

    def __init__(self, client: WrikeClient) -> None:
        """
        :param client: Shared Wrike client, already carrying the token and the
                       base URL from segredo.ini. Passed in rather than built
                       here so the retry policy and the contact cache are the
                       ones the Emails job uses too.
        """
        self._client = client
        self.problems: List[str] = []
        # Users whose whole schedule failed to parse this run — unlike an
        # orphan (a user the source no longer mentions at all), their real
        # pattern is simply unknown right now. Populated by assignments().
        self.unresolved_user_ids: List[str] = []

    # -- HTTP ------------------------------------------------------------- #
    def fetch_raw(self) -> List[Dict[str, Any]]:
        """Return the raw ``/workschedules`` payload, including member IDs."""
        return self._client.get_work_schedules()

    # -- Mapping ---------------------------------------------------------- #
    def assignments(self) -> List[ScheduleAssignment]:
        """Flatten the work schedules into one assignment per user.

        Schedules with no members are skipped, and a schedule whose pattern
        cannot be read is recorded in ``problems`` and skipped rather than
        aborting the run — one unreadable schedule should not stop everyone
        else's dates being written. Its members are also recorded in
        ``unresolved_user_ids``, so the populator can tell "we don't know
        this user's pattern right now" apart from "the source no longer
        mentions this user" and never purge the former.
        """
        self.problems = []
        self.unresolved_user_ids = []
        results: List[ScheduleAssignment] = []

        for schedule in self.fetch_raw():
            title = schedule.get("title")
            schedule_id = schedule.get("id")
            user_ids = schedule.get("userIds") or []

            if not user_ids:
                # No members means nothing to expand; not a problem worth
                # reporting, so it is skipped rather than recorded.
                continue

            try:
                weekdays = parse_workweek(schedule)
            except UnknownWeekdayError as exc:
                self.problems.append(f"Schedule {title!r} ({schedule_id}): {exc}")
                self.unresolved_user_ids.extend(user_ids)
                continue

            for user_id in user_ids:
                results.append(
                    ScheduleAssignment(
                        user_id=user_id,
                        user=None,  # resolved separately, see resolve_user_names
                        work_schedule_id=schedule_id,
                        work_schedule_title=title,
                        working_weekdays=set(weekdays),
                        schedule_type=schedule.get("scheduleType"),
                    )
                )

        results, warnings = dedupe_assignments(results)
        self.problems.extend(warnings)
        return results

    def resolve_user_names(self, user_ids: List[str]) -> Dict[str, str]:
        """
        Map Wrike user IDs to display names.

        The name is stored on each row so the Emails job can build its comment
        without a second API call at comment time. Delegated to the shared
        client, which batches, retries, and falls back to per-contact reads
        when one unreadable id would otherwise cost the whole batch its names.
        """
        if not user_ids:
            return {}
        return self._client.get_contact_names(user_ids)


def parse_workweek(schedule: Dict[str, Any]) -> Set[int]:
    """Extract the working weekdays from a work schedule payload.

    The shape Wrike actually returns, and the one this is built for:

    .. code-block:: json

        {
          "id": "IEAFXAOHMIACBDCL",
          "scheduleType": "Custom",
          "title": "Testing",
          "workweek": [{"workDays": ["Tue", "Wed", "Thu"], "capacityMinutes": 480}],
          "userIds": ["KUAW3EHG", "KUATAIRI"]
        }

    ``workweek`` is a list of blocks; each block names its worked days and the
    daily capacity. Blocks are unioned, and a block with zero capacity is
    treated as not worked. Several looser shapes are accepted too, so a future
    API change is unlikely to break this:

    * ``{"workweek": ["Monday", "Tuesday"]}``
    * ``{"workweek": [{"day": "Monday", "hours": 8}, {"day": "Saturday", "hours": 0}]}``
    * ``{"workweek": {"monday": true, "saturday": false}}``

    Raises ``UnknownWeekdayError`` when nothing recognisable is found, so a
    misread schedule fails loudly instead of silently marking someone absent.
    """
    raw = None
    for key in _WORKWEEK_KEYS:
        if key in schedule:
            raw = schedule[key]
            break

    if raw is None:
        raise UnknownWeekdayError(
            "No working-days field found in the work schedule payload "
            f"(looked for {', '.join(_WORKWEEK_KEYS)}). Keys present: "
            f"{sorted(schedule.keys())}. Run with --dump-raw and add the real key "
            "to _WORKWEEK_KEYS in app/sources.py."
        )

    weekdays: Set[int] = set()

    if isinstance(raw, dict):
        for day, value in raw.items():
            if _is_working_value(value):
                weekdays.add(parse_weekday(day))
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                weekdays.add(parse_weekday(entry))
                continue

            # Wrike's shape: a block listing several days plus its capacity.
            day_list = _first_present(entry, _WORKDAYS_KEYS)
            if isinstance(day_list, (list, tuple, set)):
                if _entry_is_working(entry):
                    weekdays.update(parse_weekday(day) for day in day_list)
                continue

            # One object per day.
            day = _first_present(entry, _DAY_KEYS)
            if day is None:
                raise UnknownWeekdayError(
                    f"No day name in work schedule entry: {entry!r}"
                )
            if _entry_is_working(entry):
                weekdays.add(parse_weekday(day))
    else:
        raise UnknownWeekdayError(f"Unexpected working-days value: {raw!r}")

    if not weekdays:
        raise UnknownWeekdayError(
            f"Work schedule {schedule.get('id') or schedule.get('title')!r} "
            "resolved to zero working days, which is almost certainly a parsing "
            "problem rather than real data."
        )
    return weekdays


def _first_present(entry: Dict[str, Any], keys) -> Any:
    for key in keys:
        if key in entry:
            return entry[key]
    return None


def _entry_is_working(entry: Dict[str, Any]) -> bool:
    for key in _WORKING_KEYS:
        if key in entry:
            return bool(entry[key])
    for key in _HOURS_KEYS:
        if key in entry:
            try:
                return float(entry[key]) > 0
            except (TypeError, ValueError):
                return bool(entry[key])
    # Listed with no qualifier: being present means it is worked.
    return True


def _is_working_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, dict):
        return _entry_is_working(value)
    return bool(value)
