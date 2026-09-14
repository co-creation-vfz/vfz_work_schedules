"""Integration tests for the SQL itself, against a real MySQL.

The rest of the suite runs against the in-memory repository in ``fakes.py``,
which proves the logic above the repository but says nothing about whether the
statements in ``app/repository.py`` are valid MySQL or whether the unique key
behaves as the code assumes. That is what this file is for.

It **skips itself** unless a test database is configured, so a plain
``pytest`` run needs no server and no credentials:

    TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_DATABASE=work_schedules_test \
    TEST_MYSQL_USER=root TEST_MYSQL_PASSWORD=secret pytest tests/test_repository_mysql.py

Point it at a scratch database, never at the live one: each test truncates the
table it works on. TEST_MYSQL_TABLE (default ``test_non_working_days``) keeps
the table name distinct from production's even if the database is shared.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from database_helpers import DatabaseConnection, DatabaseError
from repository import WorkScheduleRepository

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_MYSQL_HOST"),
    reason="Set TEST_MYSQL_HOST (and USER/PASSWORD/DATABASE) to run the MySQL tests",
)

TUESDAY = date(2026, 8, 25)
WEDNESDAY = date(2026, 8, 26)
FRIDAY = date(2026, 8, 28)


@pytest.fixture
def repository():
    database = DatabaseConnection(
        host=os.environ["TEST_MYSQL_HOST"],
        database=os.environ.get("TEST_MYSQL_DATABASE", "work_schedules_test"),
        user=os.environ.get("TEST_MYSQL_USER", "root"),
        password=os.environ.get("TEST_MYSQL_PASSWORD", ""),
        port=int(os.environ.get("TEST_MYSQL_PORT", "3306")),
        pool_size=1,
        pool_name="work_schedules_tests",
    )
    repo = WorkScheduleRepository(
        database,
        os.environ.get("TEST_MYSQL_TABLE", "test_non_working_days"),
        os.environ.get("TEST_MYSQL_RUNS_TABLE", "test_resync_runs"),
    )
    repo.ensure_schema()
    with database.cursor(dictionary=False, commit=True) as cur:
        cur.execute(f"TRUNCATE TABLE `{repo.table}`")
        cur.execute(f"TRUNCATE TABLE `{repo.runs_table}`")
    yield repo
    repo.close()


def _entry(user_id, day, **overrides):
    return {
        "user_id": user_id,
        "user": "Abigail Hlalele",
        "work_schedule_title": "Testing",
        "work_schedule_id": "IEAFXAOHMIACBDCL",
        "date": day,
        **overrides,
    }


def test_ensure_schema_is_safe_to_call_repeatedly(repository):
    repository.ensure_schema()
    repository.ensure_schema()  # must not raise the second time either

    assert repository.count_rows() == 0


def test_an_upsert_round_trips(repository):
    inserted, matched = repository.upsert_non_working([_entry("KUATAIRI", TUESDAY)])

    assert (inserted, matched) == (1, 0)
    rows = repository.find_non_working(["KUATAIRI"], TUESDAY)
    assert len(rows) == 1
    # DATE comes back as a string, not a datetime.date: the API contract, and
    # what the notifier's comment text is built from.
    assert rows[0]["date"] == "2026-08-25"
    assert rows[0]["user"] == "Abigail Hlalele"
    assert rows[0]["workScheduleTitle"] == "Testing"


def test_re_upserting_updates_rather_than_duplicating(repository):
    repository.upsert_non_working([_entry("KUATAIRI", TUESDAY)])
    inserted, matched = repository.upsert_non_working(
        [_entry("KUATAIRI", TUESDAY, work_schedule_title="Testing (renamed)")]
    )

    assert (inserted, matched) == (0, 1)
    assert repository.count_rows() == 1
    rows = repository.find_non_working(["KUATAIRI"], TUESDAY)
    assert rows[0]["workScheduleTitle"] == "Testing (renamed)"


def test_created_at_survives_an_update(repository):
    """The audit trail is stamped once. An update must not rewrite it."""
    repository.upsert_non_working([_entry("KUATAIRI", TUESDAY)])
    with repository._db.cursor() as cur:  # noqa: SLF001 - checking a column no method exposes
        cur.execute(
            f"SELECT created_at FROM `{repository.table}` WHERE user_id = %s",
            ("KUATAIRI",),
        )
        first = cur.fetchone()["created_at"]

    repository.upsert_non_working(
        [_entry("KUATAIRI", TUESDAY, user="Abigail H")]
    )

    with repository._db.cursor() as cur:  # noqa: SLF001
        cur.execute(
            f"SELECT created_at FROM `{repository.table}` WHERE user_id = %s",
            ("KUATAIRI",),
        )
        assert cur.fetchone()["created_at"] == first


def test_the_unique_key_rejects_a_duplicate(repository):
    """The constraint, not just the read-side collapse, stops a double entry."""
    repository.upsert_non_working([_entry("KUATAIRI", TUESDAY)])

    with repository._db.cursor(dictionary=False, commit=True) as cur:  # noqa: SLF001
        with pytest.raises(DatabaseError):
            cur.execute(
                f"INSERT INTO `{repository.table}` (user_id, `date`) VALUES (%s, %s)",
                ("KUATAIRI", "2026-08-25"),
            )


def test_a_range_query_is_inclusive_and_ordered(repository):
    repository.upsert_non_working(
        [
            _entry("KUABBCDE", FRIDAY),
            _entry("KUABBCDE", TUESDAY),
            _entry("KUABBCDE", WEDNESDAY),
        ]
    )

    rows = repository.find_non_working_days("KUABBCDE", TUESDAY, FRIDAY)

    assert [row["date"] for row in rows] == [
        "2026-08-25",
        "2026-08-26",
        "2026-08-28",
    ]


def test_delete_stale_keeps_the_dates_it_is_told_to(repository):
    repository.upsert_non_working(
        [_entry("KUABBCDE", TUESDAY), _entry("KUABBCDE", WEDNESDAY)]
    )

    removed = repository.delete_stale("KUABBCDE", TUESDAY, FRIDAY, [WEDNESDAY])

    assert removed == 1
    assert [row["date"] for row in repository.find_non_working_days(
        "KUABBCDE", TUESDAY, FRIDAY
    )] == ["2026-08-26"]


def test_delete_stale_with_nothing_to_keep_clears_the_window(repository):
    """The empty-keep case: an all-week worker whose stale rows must all go."""
    repository.upsert_non_working(
        [_entry("KUABBCDE", TUESDAY), _entry("KUABBCDE", WEDNESDAY)]
    )

    removed = repository.delete_stale("KUABBCDE", TUESDAY, FRIDAY, [])

    assert removed == 2
    assert repository.count_rows() == 0


def test_delete_stale_leaves_other_users_and_other_windows_alone(repository):
    repository.upsert_non_working(
        [
            _entry("KUABBCDE", TUESDAY),
            _entry("KUAOTHER", TUESDAY),
            _entry("KUABBCDE", date(2026, 7, 31)),
        ]
    )

    repository.delete_stale("KUABBCDE", TUESDAY, FRIDAY, [])

    assert repository.count_rows() == 2


def test_find_non_working_only_returns_the_requested_users(repository):
    repository.upsert_non_working(
        [_entry("KUATAIRI", TUESDAY), _entry("KUAOTHER", TUESDAY)]
    )

    rows = repository.find_non_working(["KUATAIRI"], TUESDAY)

    assert [row["userId"] for row in rows] == ["KUATAIRI"]


def test_user_ids_in_window(repository):
    repository.upsert_non_working(
        [
            _entry("KUATAIRI", TUESDAY),
            _entry("KUAOTHER", FRIDAY),
            _entry("KUAOUTSIDE", date(2026, 7, 31)),
        ]
    )

    assert repository.user_ids_in_window(TUESDAY, FRIDAY) == ["KUAOTHER", "KUATAIRI"]


def test_delete_users_in_window(repository):
    repository.upsert_non_working(
        [_entry("KUAGONE", TUESDAY), _entry("KUAGONE", date(2026, 7, 31))]
    )

    removed = repository.delete_users_in_window(["KUAGONE"], TUESDAY, FRIDAY)

    assert removed == 1
    assert repository.count_rows() == 1  # the July row is outside the window


# --- the run log --------------------------------------------------------- #


def test_a_recorded_run_comes_back_whole(repository):
    """The JSON column round-trips, which is what the dashboard reads."""
    repository.record_run(
        {
            "status": "success",
            "exitCode": 0,
            "triggeredBy": "cron",
            "message": "replaced the table: 3 inserted, 3 pre-existing removed",
            "startedAt": "2026-08-31T03:00:00+02:00",
            "finishedAt": "2026-08-31T03:00:07+02:00",
            "durationSeconds": 7.412,
            "inserted": 3,
            "dryRun": False,
        }
    )

    recorded = repository.latest_run()

    assert repository.count_runs() == 1
    assert recorded["triggeredBy"] == "cron"
    assert recorded["inserted"] == 3
    assert recorded["durationSeconds"] == 7.412
    assert recorded["runId"] > 0


def test_the_latest_successful_run_ignores_a_later_failure(repository):
    """"Last sync" must not move when a run failed to write anything."""
    repository.record_run(
        {
            "status": "success",
            "exitCode": 0,
            "triggeredBy": "cron",
            "startedAt": "2026-08-31T03:00:00+02:00",
            "finishedAt": "2026-08-31T03:00:07+02:00",
        }
    )
    repository.record_run(
        {
            "status": "attention",
            "exitCode": 6,
            "triggeredBy": "cron",
            "startedAt": "2026-09-01T03:00:00+02:00",
            "finishedAt": "2026-09-01T03:00:02+02:00",
        }
    )

    assert repository.latest_run()["exitCode"] == 6
    assert repository.latest_run("success")["exitCode"] == 0


def test_the_run_log_is_empty_before_anything_runs(repository):
    assert repository.latest_run() is None
    assert repository.latest_run("success") is None
