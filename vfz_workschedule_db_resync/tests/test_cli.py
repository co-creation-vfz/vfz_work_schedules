"""End-to-end tests for the populate_non_working_days CLI entry point."""

import json

import pytest

import resync
from database_helpers import DatabaseError
from sources import ScheduleAssignment
from weekdays import TUESDAY, WEDNESDAY, THURSDAY
import run_resync as cli
from .conftest import make_config

from .fakes import BrokenRepository, InMemoryWorkScheduleRepository


@pytest.fixture
def big_team_schedule(tmp_path):
    """One 12-member schedule, off Monday/Tuesday/Friday -- above the default
    blast-radius guard's minimum schedule size (10)."""
    path = tmp_path / "schedules.json"
    users = [{"userId": f"U{i:03d}", "user": f"User {i}"} for i in range(12)]
    path.write_text(
        json.dumps(
            {
                "schedules": [
                    {
                        "workScheduleId": "BIG1",
                        "workScheduleTitle": "Big Team",
                        "workingDays": ["Wed", "Thu"],
                        "users": users,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def repository(monkeypatch):
    """Swap the real MySQL pool for the in-memory repository.

    resync.build_repository is the single place a connection is opened -- by the
    CLI and by the service's trigger route alike -- so this one patch keeps the
    whole path (argument parsing, source reading, population, exit codes) off
    the network.
    """
    fake = InMemoryWorkScheduleRepository()
    monkeypatch.setattr(cli.Config, "from_segredo", classmethod(lambda cls, path=None: make_config()))
    monkeypatch.setattr(resync, "build_repository", lambda c, **kw: fake)
    return fake


def test_exit_code_4_when_the_guard_trips(big_team_schedule, repository):
    exit_code = cli.main(
        [
            "--source",
            "json",
            "--file",
            str(big_team_schedule),
            "--from",
            "2026-08-25",  # a Tuesday; Big Team is off Mon/Tue/Fri
            "--to",
            "2026-08-25",
        ]
    )

    assert exit_code == 4


def test_exit_code_0_when_the_schedule_is_actually_working(
    big_team_schedule, repository
):
    exit_code = cli.main(
        [
            "--source",
            "json",
            "--file",
            str(big_team_schedule),
            "--from",
            "2026-08-26",  # a Wednesday; Big Team works Wed/Thu
            "--to",
            "2026-08-26",
        ]
    )

    assert exit_code == 0


def test_the_guard_thresholds_are_configurable_from_the_cli(
    big_team_schedule, repository
):
    exit_code = cli.main(
        [
            "--source",
            "json",
            "--file",
            str(big_team_schedule),
            "--from",
            "2026-08-25",
            "--to",
            "2026-08-25",
            "--min-schedule-size-for-guard",
            "20",  # Big Team (12) is now below the guard's minimum
        ]
    )

    assert exit_code == 0


class _FakeWrikeSource:
    """Stands in for WrikeScheduleSource: no real Wrike API calls."""

    def __init__(self, client):
        self.problems = []
        self.unresolved_user_ids = []

    def assignments(self):
        return [
            ScheduleAssignment(
                user_id="KUAFAKE",
                user=None,  # unresolved, same as the real source before names load
                work_schedule_id="S1",
                work_schedule_title="Fake Schedule",
                working_weekdays={TUESDAY, WEDNESDAY, THURSDAY},  # off Mon/Fri
            )
        ]

    def resolve_user_names(self, user_ids):
        assert user_ids == ["KUAFAKE"]
        return {"KUAFAKE": "Fake Person"}


def test_wrike_source_resolves_names_even_without_the_flag(monkeypatch, repository):
    """A row with user_name=NULL because --resolve-names was left off was a
    silent, easy-to-miss mistake -- names must be resolved unconditionally
    for --source wrike now."""
    monkeypatch.setattr(resync, "WrikeScheduleSource", _FakeWrikeSource)

    exit_code = cli.main(
        [
            "--source",
            "wrike",
            "--from",
            "2026-08-24",  # a Monday; Fake Schedule is off Mon/Fri
            "--to",
            "2026-08-24",
        ]
    )

    assert exit_code == 0
    assert repository.find_one(userId="KUAFAKE")["user"] == "Fake Person"


def test_the_cli_creates_the_table_before_writing(big_team_schedule, repository):
    """A fresh environment must not need a manual migration step."""
    cli.main(
        [
            "--source",
            "json",
            "--file",
            str(big_team_schedule),
            "--from",
            "2026-08-26",
            "--to",
            "2026-08-26",
        ]
    )

    assert repository.schema_calls == 1


def test_exit_code_5_when_the_database_is_unreachable(
    big_team_schedule, monkeypatch
):
    """A cron run that could not reach MySQL must not look like a quiet success.

    Exit code 5 is distinct from 1 (nothing to do) precisely so the cron log
    distinguishes "no schedules today" from "the database was down and today's
    dates were never written" -- the second silently reads as "everyone is
    working" to the availability API.
    """
    monkeypatch.setattr(
        cli.Config, "from_segredo",
        classmethod(lambda cls, path=None: make_config()),
    )
    monkeypatch.setattr(
        resync,
        "build_repository",
        lambda c, **kw: BrokenRepository(DatabaseError("connection refused")),
    )

    exit_code = cli.main(
        [
            "--source",
            "json",
            "--file",
            str(big_team_schedule),
            "--from",
            "2026-08-26",
            "--to",
            "2026-08-26",
        ]
    )

    assert exit_code == 5


def test_the_default_source_is_wrike_not_json():
    """A plain invocation with no --source must not go looking for a local
    schedules.json -- that crashed with an uncaught FileNotFoundError when
    --source was silently defaulting to json."""
    assert cli.parse_args([]).source == "wrike"
