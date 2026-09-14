"""Test fixtures for the Workschedule DB Resync.

The suite runs against the in-memory repository in ``fakes.py``, so it needs no
database and no credentials. All SQL lives in ``WorkScheduleRepository``, which
that fake stands in for; ``test_repository_mysql.py`` is what checks the SQL
itself, against a real MySQL, and skips when none is configured.
"""

import os
import sys
from pathlib import Path

# The integration directory and shared/, so a test imports exactly what the
# service imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

import pytest  # noqa: E402

import general_helpers  # noqa: E402
from config import Config  # noqa: E402


# No log traffic leaves the test process, and no collector call slows it down.
general_helpers.log_mode = "do_nothing"

TEST_TABLE = "work_schedule_non_working_days"
TEST_RUNS_TABLE = "work_schedule_resync_runs"


def make_config(**overrides) -> Config:
    """A complete Config with no I/O, so tests never read the real segredo.ini."""
    values = dict(
        wrike_token="test-token",
        wrike_base_url="https://app-eu.wrike.com/api/v4",
        wrike_request_timeout=30,
        mysql_host="127.0.0.1",
        mysql_database="vfz-wrike-workschedule-v1",
        mysql_user="admin",
        mysql_password="",
        mysql_port=3306,
        mysql_timeout_seconds=1,
        non_working_table=TEST_TABLE,
        runs_table=TEST_RUNS_TABLE,
        papertrail_url="",
        papertrail_token="",
        log_keyword="vfz_workschedule_db_resync",
        log_mode="do_nothing",
        bind="127.0.0.1",
        port=5007,
        timezone="Europe/Amsterdam",
        skip_weekends=True,
        horizon_days=1,
        schedules_file="schedules.json",
        min_schedule_size_for_guard=10,
        max_non_working_fraction=0.5,
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def config() -> Config:
    return make_config()
