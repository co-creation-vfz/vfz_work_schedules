"""Tests for the resync run and its HTTP trigger.

The resync is the half a person actually reaches for: the cron runs it nightly
and the dashboard triggers it by hand. Both go through ``resync.run_resync``,
so these cover that one function plus the thin route around it.
"""

from datetime import date

import pytest

import resync
from database_helpers import DatabaseError
from sources import ScheduleAssignment
from weekdays import MONDAY, THURSDAY, TUESDAY, WEDNESDAY

from .conftest import TEST_TABLE, make_config
from .fakes import BrokenRepository, InMemoryWorkScheduleRepository

MONDAY_DATE = date(2026, 8, 24)
FRIDAY_DATE = date(2026, 8, 28)

FOUR_DAY = ScheduleAssignment(
    user_id="KUABBCDE",
    user="Jan de Vries",
    work_schedule_id="IEAFXAOHMIACBDCM",
    work_schedule_title="Four day week",
    working_weekdays={MONDAY, TUESDAY, WEDNESDAY, THURSDAY},
)


class FakeSource:
    """Stands in for JsonScheduleSource / WrikeScheduleSource."""

    def __init__(self, assignments=(), problems=(), unresolved=(), error=None):
        self._assignments = list(assignments)
        self.problems = list(problems)
        self.unresolved_user_ids = list(unresolved)
        self._error = error

    def assignments(self):
        if self._error:
            raise self._error
        return list(self._assignments)


@pytest.fixture(autouse=True)
def reset_last_run():
    """Each test starts with no recorded run; the module state is global."""
    resync._last_run = None
    yield
    resync._last_run = None


@pytest.fixture
def wired(monkeypatch):
    """Point run_resync at an in-memory repository and a fake source."""

    def install(source, repository=None):
        repo = repository or InMemoryWorkScheduleRepository(table=TEST_TABLE)
        monkeypatch.setattr(resync, "build_repository", lambda c, **kw: repo)
        monkeypatch.setattr(resync, "build_source", lambda c, src, f=None: source)
        return repo

    return install


# --- the run ------------------------------------------------------------- #


def test_a_successful_resync_reports_what_it_wrote(config, wired):
    repository = wired(FakeSource([FOUR_DAY]))

    result = resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="test"
    )

    assert result["status"] == "success"
    assert result["exitCode"] == resync.EXIT_OK
    assert result["inserted"] == 1  # only the Friday in that window
    assert result["triggeredBy"] == "test"
    assert repository.count() == 1


def test_a_dry_run_writes_nothing(config, wired):
    repository = wired(FakeSource([FOUR_DAY]))

    result = resync.run_resync(
        config,
        date_from=MONDAY_DATE,
        date_to=FRIDAY_DATE,
        dry_run=True,
        triggered_by="test",
    )

    assert result["status"] == "success"
    assert result["dryRun"] is True
    assert repository.count() == 0


def test_an_unreadable_schedule_gives_exit_code_3(config, wired):
    wired(FakeSource([FOUR_DAY], problems=["Schedule 'Broken' (S9): no day names"]))

    result = resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="test"
    )

    # Written, but not silently: a partial answer must be noticed.
    assert result["exitCode"] == resync.EXIT_UNREADABLE_SCHEDULE
    assert result["status"] == "attention"
    assert result["problems"]


def test_an_unreachable_database_gives_exit_code_5(config, wired):
    wired(
        FakeSource([FOUR_DAY]),
        repository=BrokenRepository(DatabaseError("connection refused")),
    )

    result = resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="test"
    )

    # Distinct from "nothing to do": a day with no rows reads as "everyone is
    # working" to the availability API, so a silent database failure is the
    # worst outcome there is.
    assert result["exitCode"] == resync.EXIT_DATABASE
    assert "MySQL unavailable" in result["message"]


def test_a_failing_source_gives_exit_code_6(config, wired):
    wired(FakeSource(error=FileNotFoundError("schedules.json")))

    result = resync.run_resync(config, source="json", triggered_by="test")

    assert result["exitCode"] == resync.EXIT_SOURCE_FAILED


def test_no_assignments_gives_exit_code_1(config, wired):
    wired(FakeSource([]))

    result = resync.run_resync(config, triggered_by="test")

    assert result["exitCode"] == resync.EXIT_NOTHING_TO_DO


def test_a_backwards_window_is_refused(config, wired):
    wired(FakeSource([FOUR_DAY]))

    result = resync.run_resync(
        config, date_from=FRIDAY_DATE, date_to=MONDAY_DATE, triggered_by="test"
    )

    assert result["exitCode"] == resync.EXIT_BAD_ARGUMENTS


def test_the_last_run_is_recorded(config, wired):
    wired(FakeSource([FOUR_DAY]))
    assert resync.last_run() is None

    resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="test"
    )

    assert resync.last_run()["triggeredBy"] == "test"


def test_a_finished_run_is_written_to_the_run_log(config, wired):
    """The durable half of "last run": a row survives the process."""
    repository = wired(FakeSource([FOUR_DAY]))

    resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="cron"
    )

    assert repository.count_runs() == 1
    assert repository.runs[0]["triggeredBy"] == "cron"
    assert repository.runs[0]["status"] == "success"


def test_the_last_run_comes_from_the_run_log_not_this_process(config, wired):
    """The whole point of the change.

    A cron run happens in its own process, so nothing it did is in this one's
    memory. Reading the run log is what makes the dashboard show it, and the
    empty in-memory state here stands in for exactly that.
    """
    repository = wired(FakeSource([FOUR_DAY]))
    resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="cron"
    )
    resync._last_run = None  # as if this process had never run one

    assert resync.last_run() is None
    assert resync.last_run(repository)["triggeredBy"] == "cron"


def test_the_last_successful_run_skips_a_failed_one(config, wired):
    """"Last sync" means the last run that actually wrote the table."""
    repository = wired(FakeSource([FOUR_DAY]))
    resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="cron"
    )
    wired(FakeSource(problems=["schedule IEA... could not be read"]), repository)
    resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="cron"
    )

    assert repository.count_runs() == 2
    assert resync.last_run(repository)["status"] == "attention"
    assert resync.last_run(repository, status="success")["exitCode"] == resync.EXIT_OK


def test_a_run_log_failure_does_not_change_the_run_result(config, wired, monkeypatch):
    """Bookkeeping must never be the thing that fails a resync that worked."""
    repository = wired(FakeSource([FOUR_DAY]))
    monkeypatch.setattr(
        repository,
        "record_run",
        lambda d: (_ for _ in ()).throw(DatabaseError("run log unavailable")),
    )

    result = resync.run_resync(
        config, date_from=MONDAY_DATE, date_to=FRIDAY_DATE, triggered_by="cron"
    )

    assert result["status"] == "success"
    assert result["exitCode"] == resync.EXIT_OK


def test_an_ignored_run_is_not_logged_as_a_sync(config, wired):
    """A refused trigger touched nothing; it must not move "last sync"."""
    repository = wired(FakeSource([FOUR_DAY]))
    resync._lock.acquire()
    try:
        resync.run_resync(config, triggered_by="test")
    finally:
        resync._lock.release()

    assert repository.count_runs() == 0


def test_a_second_resync_is_refused_while_one_is_in_flight(config, wired):
    """Two overlapping resyncs would race on the same rows."""
    wired(FakeSource([FOUR_DAY]))
    resync._lock.acquire()
    try:
        result = resync.run_resync(config, triggered_by="test")
    finally:
        resync._lock.release()

    assert result["status"] == "ignored"
    assert "already in progress" in result["message"]


def test_the_default_window_follows_horizon_days(config):
    """horizon_days counts today, so 1 is a single-day window."""
    start, end = resync.resolve_window(config)
    assert start == end

    three_days = make_config(horizon_days=3)
    start, end = resync.resolve_window(three_days)
    assert (end - start).days == 2


# --- the HTTP trigger ---------------------------------------------------- #
# The service reads its config once at import and holds it in a module-level
# CONFIG, so a test swaps that rather than overriding a dependency.


@pytest.fixture
def client(monkeypatch, config):
    """A TestClient whose resync runs against an in-memory repository.

    BackgroundTasks run before TestClient returns, so by the time a call comes
    back the run has finished and the status route has a result to show.
    """
    from fastapi.testclient import TestClient

    import vfz_workschedule_db_resync_main as service

    repository = InMemoryWorkScheduleRepository(table=TEST_TABLE)
    monkeypatch.setattr(service, "CONFIG", config)
    monkeypatch.setattr(resync, "build_repository", lambda c, **kw: repository)
    monkeypatch.setattr(
        resync, "build_source", lambda c, src, f=None: FakeSource([FOUR_DAY])
    )

    with TestClient(service.app) as test_client:
        yield test_client, repository, service


def test_the_trigger_route_runs_a_resync(client):
    test_client, repository, service = client

    response = test_client.post(
        f"{service.RESYNC_PATH}/",
        json={
            "dateFrom": "2026-08-24",
            "dateTo": "2026-08-28",
            "triggeredBy": "test",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["accepted"] is True
    assert body["dateFrom"] == "2026-08-24"
    assert repository.count() == 1
    assert resync.last_run()["triggeredBy"] == "test"


def test_the_trigger_route_defaults_to_a_live_run(client):
    """dryRun must default to False, matching the cron.

    Anything calling this without saying otherwise should get the cron's
    behaviour, not a silent rehearsal that looks like it worked.
    """
    test_client, _, service = client

    response = test_client.post(f"{service.RESYNC_PATH}/", json={})

    assert response.json()["dryRun"] is False


def test_the_trigger_route_rejects_a_backwards_window(client):
    test_client, _, service = client

    response = test_client.post(
        f"{service.RESYNC_PATH}/",
        json={"dateFrom": "2026-08-28", "dateTo": "2026-08-24"},
    )

    assert response.status_code == 422


def test_an_omitted_guard_override_uses_the_configured_value(client, monkeypatch):
    """The dashboard sends neither guard field.

    Defaulting them to literals in the request model would silently override a
    tuned [db_resync] setting on every call from the page.
    """
    test_client, _, service = client
    seen = {}

    def capture(cfg, **kwargs):
        seen.update(kwargs)

    # monkeypatch, not a bare assignment: resync is a module object shared by
    # every test in this file, so an unrestored patch would silently disable
    # the real run for whatever ran next.
    monkeypatch.setattr(resync, "run_resync_in_background", capture)
    test_client.post(f"{service.RESYNC_PATH}/", json={})

    assert seen["min_schedule_size_for_guard"] is None
    assert seen["max_non_working_fraction"] is None


def test_the_status_route_reports_the_last_run(client):
    test_client, _, service = client
    test_client.post(f"{service.RESYNC_PATH}/", json={"triggeredBy": "test"})

    response = test_client.get(f"{service.RESYNC_PATH}/status")

    assert response.status_code == 200
    body = response.json()
    assert body["running"] is False
    assert body["lastRun"]["triggeredBy"] == "test"
    assert body["lastSuccessfulRun"]["triggeredBy"] == "test"
    assert body["table"] == TEST_TABLE


def test_the_status_route_reports_a_run_this_process_never_did(client):
    """A cron run, from the dashboard's point of view: logged, not in memory."""
    test_client, repository, service = client
    repository.record_run(
        {
            "status": "success",
            "exitCode": 0,
            "triggeredBy": "cron",
            "message": "replaced the table: 3 inserted, 3 pre-existing row(s) removed",
            "startedAt": "2026-08-31T03:00:00+02:00",
            "finishedAt": "2026-08-31T03:00:07+02:00",
        }
    )

    body = test_client.get(f"{service.RESYNC_PATH}/status").json()

    assert resync.last_run() is None  # nothing in this process's memory
    assert body["lastRun"]["triggeredBy"] == "cron"
    assert body["lastSuccessfulRun"]["finishedAt"] == "2026-08-31T03:00:07+02:00"


def test_the_non_working_days_route_lists_everyone_off(client):
    test_client, repository, service = client
    repository.insert(userId="KUATAIRI", user="Abigail Hlalele", date="2026-08-25")

    response = test_client.get(
        f"{service.RESYNC_PATH}/non-working-days?date=2026-08-25"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["weekday"] == "Tuesday"
    assert body["users"][0]["userId"] == "KUATAIRI"


def test_the_hello_route_touches_nothing(client):
    test_client, _, service = client

    body = test_client.get("/hello").json()

    assert body["status"] == "alive"
    assert body["port"] == 5007
    assert body["resyncPath"] == f"{service.RESYNC_PATH}/"
