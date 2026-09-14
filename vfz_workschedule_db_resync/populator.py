"""Writing the expanded dates into MySQL.

This is the other half of the system: the API reads the non-working-day table,
and this fills it by expanding each user's weekday pattern into dates over a
rolling window.

Re-running it is safe and self-correcting. For every user in the window it
computes the dates that *should* be there, upserts those, and removes any of
that user's rows inside the window that should no longer exist — so a schedule
change (someone moves from a four-day to a five-day week) is reflected on the
next run without leaving stale days behind.

No SQL here: every read and write goes through ``WorkScheduleRepository``, so
this module holds only the decision-making, and a test can drive it against an
in-memory repository without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from typing import Any, Dict, List, Sequence, Set, Tuple

from expansion import expand_non_working_dates, non_working_weekdays
from repository import WorkScheduleRepository
from sources import ScheduleAssignment
from weekdays import format_weekdays


@dataclass
class PopulationResult:
    """What a populate run did."""

    date_from: Date
    date_to: Date
    users: int = 0
    dates_expected: int = 0
    inserted: int = 0
    matched: int = 0
    removed: int = 0
    skipped: List[str] = field(default_factory=list)
    orphans: List[str] = field(default_factory=list)
    orphans_removed: int = 0
    blast_radius_flagged: List[str] = field(default_factory=list)
    # True when the run replaced the table outright rather than reconciling it.
    full_refresh: bool = False
    # Set when a replace was asked for but refused as unsafe -- see populate().
    full_refresh_declined: str = ""

    def summary(self) -> str:
        lines = [
            f"Window: {self.date_from} to {self.date_to}",
            f"Users processed: {self.users}",
            f"Non-working dates expected: {self.dates_expected}",
            (
                f"Replaced the table: inserted {self.inserted}, "
                f"removed {self.removed} pre-existing row(s)"
                if self.full_refresh
                else f"Inserted: {self.inserted}   Already present: {self.matched}   "
                f"Removed as stale: {self.removed}"
            ),
        ]
        if self.full_refresh_declined:
            lines.append(f"NOT replaced: {self.full_refresh_declined}")
        if self.skipped:
            lines.append(f"Skipped ({len(self.skipped)}):")
            lines.extend(f"  - {reason}" for reason in self.skipped)
        if self.blast_radius_flagged:
            lines.append(
                f"Blast-radius guard tripped ({len(self.blast_radius_flagged)}) "
                "-- left untouched, not written:"
            )
            lines.extend(f"  - {reason}" for reason in self.blast_radius_flagged)
        if self.orphans:
            lines.append(
                f"Rows in the window for users the source did not return "
                f"({len(self.orphans)}): {', '.join(self.orphans[:20])}"
                + (" ..." if len(self.orphans) > 20 else "")
            )
            if self.orphans_removed:
                lines.append(f"  Deleted {self.orphans_removed} of them.")
            else:
                lines.append(
                    "  Left in place: these users would still be reported as not "
                    "working. Re-run with --purge-orphans to remove them."
                )
        return "\n".join(lines)

    def as_details(self) -> Dict[str, Any]:
        """The run, as a JSON-safe object for the SolarWinds ``details`` field.

        Counts and reasons both, so a log line is enough to tell a quiet
        successful run from one that wrote nothing because a guard tripped.
        """
        return {
            "dateFrom": self.date_from.isoformat(),
            "dateTo": self.date_to.isoformat(),
            "users": self.users,
            "datesExpected": self.dates_expected,
            "inserted": self.inserted,
            "matched": self.matched,
            "removed": self.removed,
            "skipped": self.skipped,
            "orphans": self.orphans,
            "orphansRemoved": self.orphans_removed,
            "blastRadiusFlagged": self.blast_radius_flagged,
            "fullRefresh": self.full_refresh,
            "fullRefreshDeclined": self.full_refresh_declined,
        }


class NonWorkingDayPopulator:
    def __init__(self, repository: WorkScheduleRepository) -> None:
        self._repository = repository

    def populate(
        self,
        assignments: Sequence[ScheduleAssignment],
        date_from: Date,
        date_to: Date,
        dry_run: bool = False,
        purge_orphans: bool = False,
        full_refresh: bool = False,
        unresolved_user_ids: Sequence[str] = (),
        min_schedule_size_for_guard: int = 10,
        max_non_working_fraction: float = 0.5,
    ) -> PopulationResult:
        result = PopulationResult(date_from=date_from, date_to=date_to)
        handled: List[str] = []

        # Pass 1: expand every resolvable assignment's pattern into dates.
        expanded: List[Tuple[ScheduleAssignment, List[Date]]] = []
        for assignment in assignments:
            if not assignment.working_weekdays:
                result.skipped.append(
                    f"{assignment.user or assignment.user_id}: no working weekdays "
                    "resolved; left untouched rather than marked absent all week"
                )
                continue

            dates = expand_non_working_dates(
                assignment.working_weekdays, date_from, date_to
            )
            expanded.append((assignment, dates))

        # Pass 2: flag any schedule where an unusually large share of a large
        # enough population would newly go non-working on the same date. A
        # misread or a surprising bulk change in Wrike must be reviewed by a
        # human before it reaches a live Wrike task, not written silently --
        # the same principle behind refusing an empty pattern, applied to the
        # opposite extreme.
        guarded_schedule_ids: Set[str] = self._blast_radius_guard(
            expanded, result, min_schedule_size_for_guard, max_non_working_fraction
        )

        # Pass 3: write everything except guarded schedules.
        #
        # The stale cleanup is collected here and run once at the end rather
        # than per user. A query each meant one round trip per person on a
        # schedule -- 224 of them on this account -- which took a resync past
        # two minutes and made it look hung.
        entries: List[Dict[str, Any]] = []
        guarded_user_ids: List[str] = []
        keep_by_user: Dict[str, List[Date]] = {}
        for assignment, dates in expanded:
            if assignment.work_schedule_id in guarded_schedule_ids:
                guarded_user_ids.append(assignment.user_id)
                continue

            result.users += 1
            result.dates_expected += len(dates)
            handled.append(assignment.user_id)
            keep_by_user[assignment.user_id] = dates

            # Built even on a dry run when replacing: dates_expected is what a
            # dry run reports, and it has to match what a live run would write.
            entries.extend(_entry(assignment, day) for day in dates)

        # A cron run replaces the table outright: whatever Wrike says now is the
        # whole truth. The table is not a history -- it holds only the people
        # who are off in this run's window, so there is no stale row, no orphan
        # and no accumulating past date to reason about.
        #
        # But a replace is only safe when the source was read CLEANLY. Every
        # exception below means some user's real pattern is unknown this run,
        # and a blanket delete would erase a correct answer and report them as
        # working:
        #
        #   guarded    a schedule swung too far to write unattended, so its
        #              members were not expanded -- replacing would delete them
        #   skipped    no working weekdays resolved, so the pattern is unknown
        #   unresolved the source could not parse their schedule at all
        #
        # An empty `entries` from a clean read is different and perfectly
        # normal: on a Tuesday nobody on a reduced schedule is off, and the
        # table should genuinely end up empty.
        if full_refresh:
            l_unclean = (
                result.blast_radius_flagged
                + result.skipped
                + [f"unresolved: {uid}" for uid in unresolved_user_ids]
            )
            if l_unclean:
                # Fall back to reconciling, which only ever touches users it
                # actually expanded. Said out loud rather than done quietly:
                # the caller asked for a replace and is not getting one.
                result.full_refresh_declined = (
                    f"{len(l_unclean)} schedule(s) or user(s) could not be read "
                    f"cleanly, so the table was reconciled instead of replaced; "
                    f"rows for those users were left untouched"
                )
            else:
                result.full_refresh = True
                if dry_run:
                    # Every row is about to be rewritten, so the whole table is
                    # what a live run would remove.
                    result.removed = self._repository.count_rows()
                else:
                    inserted, removed = self._repository.replace_all(entries)
                    result.inserted = inserted
                    result.removed = removed
                # Orphans are meaningless after a replace: every row in the
                # table came from this run.
                return result

        if keep_by_user:
            if dry_run:
                # Report what the cleanup would remove without touching anything.
                result.removed = self._repository.count_stale_for(
                    keep_by_user, date_from, date_to
                )
            else:
                result.removed = self._repository.delete_stale_for(
                    keep_by_user, date_from, date_to
                )

        if entries and not dry_run:
            inserted, matched = self._repository.upsert_non_working(entries)
            result.inserted = inserted
            result.matched = matched

        result.orphans = self._find_orphans(
            date_from,
            date_to,
            handled,
            set(unresolved_user_ids) | set(guarded_user_ids),
        )
        if result.orphans and purge_orphans and not dry_run:
            result.orphans_removed = self._repository.delete_users_in_window(
                result.orphans, date_from, date_to
            )

        return result

    def _blast_radius_guard(
        self,
        expanded: Sequence[Tuple[ScheduleAssignment, List[Date]]],
        result: PopulationResult,
        min_schedule_size_for_guard: int,
        max_non_working_fraction: float,
    ) -> Set[str]:
        """Flag schedules where too large a share would newly go non-working.

        Grouped by ``work_schedule_id`` rather than title: two schedules can
        share a title, and this must never merge unrelated populations.
        Schedules below ``min_schedule_size_for_guard`` are never flagged --
        a small team legitimately sharing a day off is normal, not a sign of
        a misread or an unreviewed bulk change.
        """
        by_schedule: Dict[str, List[Tuple[ScheduleAssignment, List[Date]]]] = {}
        for item in expanded:
            by_schedule.setdefault(item[0].work_schedule_id, []).append(item)

        guarded: Set[str] = set()
        for schedule_id, items in by_schedule.items():
            size = len(items)
            if size < min_schedule_size_for_guard:
                continue

            per_date: Dict[Date, int] = {}
            for _, dates in items:
                for day in dates:
                    per_date[day] = per_date.get(day, 0) + 1

            for day in sorted(per_date):
                count = per_date[day]
                fraction = count / size
                if fraction > max_non_working_fraction:
                    title = items[0][0].work_schedule_title or schedule_id
                    result.blast_radius_flagged.append(
                        f"{title!r} ({schedule_id}): {count}/{size} members "
                        f"({fraction:.0%}) would be marked not working on {day}. "
                        "Confirm this in Wrike, then re-run."
                    )
                    guarded.add(schedule_id)
                    break  # one date is enough to guard the whole schedule

        return guarded

    def _find_orphans(
        self,
        date_from: Date,
        date_to: Date,
        handled: Sequence[str],
        unresolved_user_ids: Sequence[str] = (),
    ) -> List[str]:
        """Users with rows in the window that this run did not account for.

        These are the blind spot: cleanup only touches users the source returned,
        so someone dropped from every Wrike schedule keeps whatever rows they
        already had and goes on being reported as not working. Reported by
        default, deleted only on request, because a partial source (a
        hand-written schedules.json) would otherwise wipe everyone it does not
        list.

        ``unresolved_user_ids`` covers two different cases, and neither is ever
        treated as an orphan: a user whose schedule the source mentioned but
        could not parse this run (real pattern unknown, not confirmed absent --
        the exit-code-3 case), and a user whose schedule tripped the
        blast-radius guard (read fine, but the result was too large a swing to
        trust unattended). Purging either would erase a correct answer over a
        transient or unreviewed read, which is exactly what both safeguards
        exist to prevent.
        """
        known = set(handled) | set(unresolved_user_ids)
        present = self._repository.user_ids_in_window(date_from, date_to)
        return sorted(user_id for user_id in present if user_id not in known)


def _entry(assignment: ScheduleAssignment, day: Date) -> Dict[str, Any]:
    """One row to upsert, keyed by the repository on user and date."""
    return {
        "user_id": assignment.user_id,
        "user": assignment.user,
        "work_schedule_title": assignment.work_schedule_title,
        "work_schedule_id": assignment.work_schedule_id,
        "date": day,
    }


def describe_assignment(assignment: ScheduleAssignment) -> str:
    """A one-line rendering of what a schedule means, for logs and CLI output."""
    return (
        f"{assignment.describe()} -> off on "
        f"{format_weekdays(non_working_weekdays(assignment.working_weekdays)) or 'no weekdays'}"
    )
