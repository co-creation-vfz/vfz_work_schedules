"""Tests for writing expanded dates into MySQL.

Driven against the in-memory repository from ``fakes.py``: the populator holds
no SQL of its own, so these tests exercise exactly the decision-making that
ships. Whether the SQL underneath is correct is ``test_repository_mysql.py``'s
job.
"""

from datetime import date

import pytest

from populator import NonWorkingDayPopulator
from sources import ScheduleAssignment
from weekdays import MONDAY, THURSDAY, TUESDAY, WEDNESDAY

from .fakes import InMemoryWorkScheduleRepository

WINDOW_FROM = date(2026, 8, 24)  # Monday
WINDOW_TO = date(2026, 9, 6)  # the Sunday two weeks later

FOUR_DAY = ScheduleAssignment(
    user_id="KUABBCDE",
    user="Jan de Vries",
    work_schedule_id="IEAFXAOHMIACBDCM",
    work_schedule_title="Four day week",
    working_weekdays={MONDAY, TUESDAY, WEDNESDAY, THURSDAY},
)


@pytest.fixture
def repository():
    return InMemoryWorkScheduleRepository()


@pytest.fixture
def populator(repository):
    return NonWorkingDayPopulator(repository)


def test_writes_one_row_per_non_working_date(populator, repository):
    result = populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    assert result.inserted == 2
    assert sorted(row["date"] for row in repository.rows) == [
        "2026-08-28",
        "2026-09-04",
    ]


def test_rows_match_the_agreed_structure(populator, repository):
    populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    row = repository.find_one(date="2026-08-28")
    assert set(row) == {
        "userId",
        "user",
        "workScheduleTitle",
        "workScheduleId",
        "date",
        "createdAt",
    }
    assert row["userId"] == "KUABBCDE"
    assert row["user"] == "Jan de Vries"
    assert row["workScheduleTitle"] == "Four day week"
    assert row["workScheduleId"] == "IEAFXAOHMIACBDCM"
    assert row["createdAt"]


def test_no_weekend_rows_are_ever_written(populator, repository):
    lone_monday = ScheduleAssignment(
        user_id="KUAONEDAY",
        user="One Day",
        work_schedule_id="S1",
        work_schedule_title="Mondays only",
        working_weekdays={MONDAY},
    )

    populator.populate([lone_monday], WINDOW_FROM, WINDOW_TO)

    for row in repository.rows:
        assert date.fromisoformat(row["date"]).weekday() < 5


def test_rerunning_is_idempotent(populator, repository):
    populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)
    first = repository.find_one(date="2026-08-28")

    second_run = populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    assert repository.count() == 2
    assert second_run.inserted == 0
    assert second_run.matched == 2
    # created_at is preserved: the upsert never touches it, so a re-run does
    # not rewrite the audit trail.
    assert repository.find_one(date="2026-08-28")["createdAt"] == first["createdAt"]


def test_a_schedule_change_removes_stale_dates(populator, repository):
    populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)
    assert repository.count() == 2

    moved_to_full_week = ScheduleAssignment(
        user_id=FOUR_DAY.user_id,
        user=FOUR_DAY.user,
        work_schedule_id="IEAFXAOHMIACBDCN",
        work_schedule_title="Standard week",
        working_weekdays={0, 1, 2, 3, 4},
    )

    result = populator.populate([moved_to_full_week], WINDOW_FROM, WINDOW_TO)

    assert result.removed == 2
    assert repository.count() == 0


def test_a_changed_schedule_updates_the_title_on_kept_dates(
    populator, repository
):
    populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    renamed = ScheduleAssignment(
        user_id=FOUR_DAY.user_id,
        user="Jan de Vries",
        work_schedule_id="IEAFXAOHMIACBDCM",
        work_schedule_title="Four day week (renamed)",
        working_weekdays=FOUR_DAY.working_weekdays,
    )
    populator.populate([renamed], WINDOW_FROM, WINDOW_TO)

    titles = {row["workScheduleTitle"] for row in repository.rows}
    assert titles == {"Four day week (renamed)"}


def test_dates_outside_the_window_are_left_alone(populator, repository):
    repository.insert({"userId": FOUR_DAY.user_id, "date": "2026-07-31", "user": "Jan de Vries"})

    populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    assert repository.count(date="2026-07-31") == 1


def test_another_users_dates_are_left_alone(populator, repository):
    repository.insert({"userId": "KUAOTHER", "date": "2026-08-28"})

    populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    assert repository.count(userId="KUAOTHER") == 1


def test_dry_run_writes_nothing_but_still_reports(populator, repository):
    result = populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO, dry_run=True)

    assert repository.count() == 0
    assert result.users == 1
    assert result.dates_expected == 2
    assert result.inserted == 0


def test_an_unresolved_pattern_is_skipped_not_expanded(populator, repository):
    broken = ScheduleAssignment(
        user_id="KUABROKEN",
        user="Unknown Pattern",
        work_schedule_id="S9",
        work_schedule_title="Unparsed",
        working_weekdays=set(),
    )

    result = populator.populate([broken, FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    assert repository.count(userId="KUABROKEN") == 0
    assert len(result.skipped) == 1
    assert "Unknown Pattern" in result.skipped[0]
    assert result.users == 1  # the good assignment still ran


def test_all_the_dates_go_in_one_upsert_call():
    """Every expanded date is handed to the repository together.

    The repository turns that into a single transaction. Splitting it per date
    would mean a run that failed half way left the table describing a week that
    never existed -- some days written under the new schedule, the rest under
    the old.
    """

    class RecordingRepository(InMemoryWorkScheduleRepository):
        def __init__(self):
            super().__init__()
            self.upsert_calls = []

        def upsert_non_working(self, entries):
            self.upsert_calls.append(list(entries))
            return super().upsert_non_working(entries)

    repository = RecordingRepository()
    result = NonWorkingDayPopulator(repository).populate(
        [FOUR_DAY], WINDOW_FROM, WINDOW_TO
    )

    assert len(repository.upsert_calls) == 1
    assert len(repository.upsert_calls[0]) == 2
    assert result.inserted == 2


# --- the stale-row question ------------------------------------------------ #
# "If I work Tuesday, but the Tuesday run finds me in the database, am I removed?"
TUESDAY_ONLY = date(2026, 8, 25)

WORKS_TUESDAY = ScheduleAssignment(
    user_id="KUATAIRI",
    user="Abigail Hlalele",
    work_schedule_id="IEAFXAOHMIACBDCL",
    work_schedule_title="Testing",
    working_weekdays={TUESDAY, WEDNESDAY, THURSDAY},
)


def test_a_stale_row_for_a_day_you_work_is_removed(populator, repository):
    """The whole question: a leftover Tuesday document, on a Tuesday run."""
    repository.insert({
            "userId": "KUATAIRI",
            "user": "Abigail Hlalele",
            "workScheduleTitle": "Testing",
            "workScheduleId": "IEAFXAOHMIACBDCL",
            "date": "2026-08-25",
        })

    result = populator.populate([WORKS_TUESDAY], TUESDAY_ONLY, TUESDAY_ONLY)

    assert result.dates_expected == 0  # she works Tuesday, so nothing is expected
    assert result.removed == 1
    assert repository.count(userId="KUATAIRI") == 0


def test_a_dry_run_reports_the_removal_without_doing_it(populator, repository):
    repository.insert({"userId": "KUATAIRI", "date": "2026-08-25"})

    result = populator.populate(
        [WORKS_TUESDAY], TUESDAY_ONLY, TUESDAY_ONLY, dry_run=True
    )

    assert result.removed == 1
    assert repository.count() == 1  # still there


def test_a_correct_row_survives_the_same_run(populator, repository):
    monday = date(2026, 8, 24)
    repository.insert({"userId": "KUATAIRI", "date": "2026-08-24"})

    result = populator.populate([WORKS_TUESDAY], monday, monday)

    assert result.removed == 0
    assert repository.count(date="2026-08-24") == 1


def test_a_stale_row_survives_if_the_user_is_no_longer_in_any_schedule(
    populator, repository
):
    """The blind spot: cleanup only touches users the source returned."""
    repository.insert({"userId": "KUAGONE", "date": "2026-08-25"})

    result = populator.populate([WORKS_TUESDAY], TUESDAY_ONLY, TUESDAY_ONLY)

    assert repository.count(userId="KUAGONE") == 1
    assert result.orphans == ["KUAGONE"]  # reported, not silently ignored
    assert result.orphans_removed == 0
    assert "KUAGONE" in result.summary()


def test_purge_orphans_removes_them_when_asked(populator, repository):
    repository.insert({"userId": "KUAGONE", "date": "2026-08-25"})

    result = populator.populate(
        [WORKS_TUESDAY], TUESDAY_ONLY, TUESDAY_ONLY, purge_orphans=True
    )

    assert result.orphans_removed == 1
    assert repository.count(userId="KUAGONE") == 0


def test_a_skipped_user_is_not_treated_as_handled(populator, repository):
    """An unreadable schedule must not let its user's rows be silently kept."""
    repository.insert({"userId": "KUABROKEN", "date": "2026-08-25"})
    broken = ScheduleAssignment(
        user_id="KUABROKEN",
        user="Unknown Pattern",
        work_schedule_id="S9",
        work_schedule_title="Unparsed",
        working_weekdays=set(),
    )

    result = populator.populate(
        [broken, WORKS_TUESDAY], TUESDAY_ONLY, TUESDAY_ONLY
    )

    assert result.orphans == ["KUABROKEN"]
    assert repository.count(userId="KUABROKEN") == 1


def test_an_unresolved_users_rows_survive_purge_orphans(populator, repository):
    """The Wrike case: an unparseable schedule, unlike the empty-pattern case
    above, drops its users before an assignment is ever built. Their existing
    rows must not be purged just because this run could not re-read their
    schedule -- that would erase a correct answer over a transient read
    failure, defeating the exit-code-3 safeguard.
    """
    repository.insert({"userId": "KUAUNPARSEABLE", "date": "2026-08-25"})

    result = populator.populate(
        [WORKS_TUESDAY],
        TUESDAY_ONLY,
        TUESDAY_ONLY,
        purge_orphans=True,
        unresolved_user_ids=["KUAUNPARSEABLE"],
    )

    assert "KUAUNPARSEABLE" not in result.orphans
    assert repository.count(userId="KUAUNPARSEABLE") == 1


# --- the blast-radius guard ------------------------------------------------- #
# The incident this defends against: a shared schedule's effective pattern
# changes (in Wrike, or via a misread), and most of a large population is
# suddenly written as not working in one run, with nothing to catch it before
# it reaches a live Wrike task.
def _schedule_of(size, off_count, schedule_id="SCHED-BIG", title="Big Team"):
    """``off_count`` members off on TUESDAY_ONLY; the rest working that day."""
    assignments = []
    for i in range(size):
        working_weekdays = (
            {WEDNESDAY, THURSDAY} if i < off_count else {TUESDAY, WEDNESDAY, THURSDAY}
        )
        assignments.append(
            ScheduleAssignment(
                user_id=f"KUABIG{i:03d}",
                user=f"User {i}",
                work_schedule_id=schedule_id,
                work_schedule_title=title,
                working_weekdays=working_weekdays,
            )
        )
    return assignments


def test_the_guard_trips_when_most_of_a_large_schedule_flips(
    populator, repository
):
    assignments = _schedule_of(size=20, off_count=15)  # 75% > the 50% default

    result = populator.populate(assignments, TUESDAY_ONLY, TUESDAY_ONLY)

    assert len(result.blast_radius_flagged) == 1
    assert "SCHED-BIG" in result.blast_radius_flagged[0]
    assert result.users == 0  # none of this schedule's members were processed
    assert repository.count() == 0  # nothing written


def test_a_small_schedule_fully_off_does_not_trip_the_guard(
    populator, repository
):
    """A small team legitimately sharing a day off is normal, not a red flag."""
    assignments = _schedule_of(
        size=3, off_count=3, schedule_id="SCHED-SMALL", title="Small Team"
    )

    result = populator.populate(assignments, TUESDAY_ONLY, TUESDAY_ONLY)

    assert result.blast_radius_flagged == []
    assert result.users == 3
    assert repository.count() == 3


def test_a_large_schedule_mostly_working_does_not_trip_the_guard(
    populator, repository
):
    assignments = _schedule_of(
        size=20, off_count=2, schedule_id="SCHED-LOW", title="Mostly Working"
    )

    result = populator.populate(assignments, TUESDAY_ONLY, TUESDAY_ONLY)

    assert result.blast_radius_flagged == []
    assert result.users == 20


def test_a_guarded_schedules_existing_rows_survive_purge_orphans(
    populator, repository
):
    """Guarded users must never be purged as orphans: their status is simply
    unreviewed this run, not confirmed absent from every schedule."""
    repository.insert({"userId": "KUABIG000", "date": "2026-08-25"})
    assignments = _schedule_of(size=20, off_count=15)

    result = populator.populate(
        assignments, TUESDAY_ONLY, TUESDAY_ONLY, purge_orphans=True
    )

    assert "KUABIG000" not in result.orphans
    assert repository.count(userId="KUABIG000") == 1


def test_the_guard_is_configurable(populator, repository):
    assignments = _schedule_of(size=5, off_count=5)  # below the default min size

    result = populator.populate(
        assignments,
        TUESDAY_ONLY,
        TUESDAY_ONLY,
        min_schedule_size_for_guard=3,
        max_non_working_fraction=0.9,
    )

    assert len(result.blast_radius_flagged) == 1  # 100% > 90%, size 5 >= 3
    assert result.users == 0


def test_the_repository_reads_back_what_the_populator_wrote(populator, repository):
    """End to end: weekday pattern in, non-working rows out.

    This is the contract the Emails job depends on -- it reads exactly these
    rows -- so it is asserted at the repository boundary rather than through a
    service layer.
    """
    populator.populate([FOUR_DAY], WINDOW_FROM, WINDOW_TO)

    off = repository.find_non_working([FOUR_DAY.user_id], date(2026, 8, 28))
    on = repository.find_non_working([FOUR_DAY.user_id], date(2026, 8, 27))

    assert [row["user"] for row in off] == ["Jan de Vries"]  # Friday, not worked
    assert on == []  # Thursday, worked


# --- full refresh: what a cron run does ------------------------------------ #
# The cron replaces the table outright rather than reconciling it, so the result
# is exactly what Wrike says now -- no stale rows, no orphans left behind.


def test_a_full_refresh_replaces_the_whole_table(populator, repository):
    """The table is not a history: only this run's window survives it."""
    repository.insert(userId="KUAGONE", date="2026-08-28", user="Left The Company")
    repository.insert(userId="KUAOLD", date="2026-07-31", user="Some Past Date")

    result = populator.populate(
        [FOUR_DAY], WINDOW_FROM, WINDOW_TO, full_refresh=True
    )

    assert result.full_refresh is True
    assert result.inserted == 2
    assert result.removed == 2  # both pre-existing rows, in or out of window
    # Nothing survives that this run did not write.
    assert {row["userId"] for row in repository.rows} == {FOUR_DAY.user_id}
    assert repository.count(userId="KUAOLD") == 0


def test_a_full_refresh_needs_no_orphan_handling(populator, repository):
    """Orphans are meaningless once every row came from this run."""
    repository.insert(userId="KUAGONE", date="2026-08-28")

    result = populator.populate(
        [FOUR_DAY], WINDOW_FROM, WINDOW_TO, full_refresh=True
    )

    assert result.orphans == []
    assert result.orphans_removed == 0
    assert repository.count(userId="KUAGONE") == 0


def test_a_full_refresh_dry_run_writes_nothing(populator, repository):
    repository.insert(userId="KUAGONE", date="2026-08-28")

    result = populator.populate(
        [FOUR_DAY], WINDOW_FROM, WINDOW_TO, full_refresh=True, dry_run=True
    )

    # Still reports what it would do, and still has not touched the table.
    assert result.dates_expected == 2
    assert result.removed == 1
    assert result.inserted == 0
    assert repository.count(userId="KUAGONE") == 1


def test_a_tripped_guard_stops_a_full_refresh_before_it_deletes(
    populator, repository
):
    """The whole point of an atomic replace: a guard must save the old data.

    A refresh that deleted first and then refused to write would leave an empty
    table, which the Emails job reads as "everyone is working".
    """
    repository.insert(userId="KUAEXISTING", date="2026-08-28", user="Still Here")
    assignments = _schedule_of(size=20, off_count=15)  # 75% > the 50% default

    result = populator.populate(
        assignments, TUESDAY_ONLY, TUESDAY_ONLY, full_refresh=True
    )

    assert len(result.blast_radius_flagged) == 1
    assert result.full_refresh is False        # refused, not attempted
    assert result.full_refresh_declined       # and it says why
    assert result.inserted == 0
    assert repository.count(userId="KUAEXISTING") == 1  # untouched


def test_a_full_refresh_is_refused_when_a_pattern_could_not_be_read(
    populator, repository
):
    """An unknown pattern must not be silently erased into "working"."""
    repository.insert(userId="KUABROKEN", date="2026-08-28", user="Unknown Pattern")
    broken = ScheduleAssignment(
        user_id="KUABROKEN",
        user="Unknown Pattern",
        work_schedule_id="S9",
        work_schedule_title="Unparsed",
        working_weekdays=set(),
    )

    result = populator.populate(
        [broken, FOUR_DAY], WINDOW_FROM, WINDOW_TO, full_refresh=True
    )

    assert result.full_refresh is False
    assert "left untouched" in result.full_refresh_declined
    assert repository.count(userId="KUABROKEN") == 1


def test_an_empty_result_from_a_clean_read_still_replaces(populator, repository):
    """A Tuesday genuinely has nobody off, and the table should say so.

    This is the case that must NOT be confused with an unclean read: an empty
    write from a clean source is the correct answer, not a reason to bail.
    """
    repository.insert(userId="KUASTALE", date="2026-08-25", user="Yesterday's Row")

    result = populator.populate(
        [WORKS_TUESDAY], TUESDAY_ONLY, TUESDAY_ONLY, full_refresh=True
    )

    assert result.full_refresh is True
    assert result.dates_expected == 0
    assert result.inserted == 0
    assert result.removed == 1
    assert repository.count() == 0
