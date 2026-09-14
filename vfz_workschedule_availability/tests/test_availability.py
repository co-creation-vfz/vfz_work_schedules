"""Tests for the Availability API and its cache.

The cache is the reason this service exists rather than pointing Workato at the
database, so most of what is worth testing is its behaviour under the awkward
cases: a date rollover, a database that has gone away, and a burst of requests
arriving on a cold cache.
"""

import os
import sys
import threading
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import general_helpers  # noqa: E402
from cache import NonWorkingCache  # noqa: E402

general_helpers.log_mode = "do_nothing"

MONDAY = date(2026, 8, 24)
TUESDAY = date(2026, 8, 25)

ROWS = [
    {
        "userId": "KUATAIRI",
        "user": "Abigail Hlalele",
        "workScheduleTitle": "Testing",
        "workScheduleId": "IEASCHED",
        "date": "2026-08-24",
    },
    {
        "userId": "KUAJASON",
        "user": "Jason Blignaut",
        "workScheduleTitle": "Testing",
        "workScheduleId": "IEASCHED",
        "date": "2026-08-24",
    },
]


class RecordingLoader:
    """Counts how often the cache actually reaches for the database."""

    def __init__(self, rows=None, error=None):
        self.rows = list(rows if rows is not None else ROWS)
        self.error = error
        self.calls = 0

    def __call__(self, on_date):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.rows)


# --- the cache ----------------------------------------------------------- #


def test_a_second_request_is_served_from_memory():
    """The whole point: Workato calls on every @tag, MySQL should not."""
    loader = RecordingLoader()
    cache = NonWorkingCache(loader, ttl_seconds=60)

    first = cache.get(MONDAY)
    second = cache.get(MONDAY)

    assert loader.calls == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(second["rows"]) == 2


def test_a_new_date_bypasses_the_cache():
    """Keyed by date, not just time.

    A time-only cache would keep serving yesterday's list past midnight, and
    which day the list describes is the entire content of the answer.
    """
    loader = RecordingLoader()
    cache = NonWorkingCache(loader, ttl_seconds=3600)

    cache.get(MONDAY)
    cache.get(TUESDAY)

    assert loader.calls == 2


def test_a_zero_ttl_disables_caching():
    loader = RecordingLoader()
    cache = NonWorkingCache(loader, ttl_seconds=0)

    cache.get(MONDAY)
    cache.get(MONDAY)

    assert loader.calls == 2


def test_a_database_failure_serves_the_last_good_answer():
    """Stale beats wrong.

    An empty list would mean "everyone is working", which suppresses the very
    warning Workato asked for. A few minutes old is far better.
    """
    loader = RecordingLoader()
    cache = NonWorkingCache(loader, ttl_seconds=0)  # always re-read
    cache.get(MONDAY)

    loader.error = RuntimeError("2003 cannot connect")
    result = cache.get(MONDAY)

    assert result["stale"] is True
    assert len(result["rows"]) == 2  # the earlier answer, not an empty list


def test_a_database_failure_with_nothing_cached_raises():
    """With no previous answer there is nothing honest to return."""
    cache = NonWorkingCache(
        RecordingLoader(error=RuntimeError("2003 cannot connect")), ttl_seconds=60
    )

    with pytest.raises(RuntimeError):
        cache.get(MONDAY)


def test_a_stale_answer_is_not_served_for_a_different_date():
    """Yesterday's list must never be passed off as today's."""
    loader = RecordingLoader()
    cache = NonWorkingCache(loader, ttl_seconds=0)
    cache.get(MONDAY)

    loader.error = RuntimeError("2003 cannot connect")

    with pytest.raises(RuntimeError):
        cache.get(TUESDAY)


def test_a_burst_on_a_cold_cache_reads_once():
    """A wave of @tags must not become a wave of identical queries."""
    loader = RecordingLoader()
    cache = NonWorkingCache(loader, ttl_seconds=60)

    threads = [threading.Thread(target=cache.get, args=(MONDAY,)) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert loader.calls == 1


def test_invalidate_forces_a_reread():
    loader = RecordingLoader()
    cache = NonWorkingCache(loader, ttl_seconds=3600)
    cache.get(MONDAY)

    cache.invalidate()
    cache.get(MONDAY)

    assert loader.calls == 2


def test_the_returned_rows_cannot_mutate_the_cache():
    """A caller editing its copy must not corrupt what everyone else gets."""
    cache = NonWorkingCache(RecordingLoader(), ttl_seconds=60)

    cache.get(MONDAY)["rows"].clear()

    assert len(cache.get(MONDAY)["rows"]) == 2


# --- the API ------------------------------------------------------------- #

TOKEN = "test-availability-token"


@pytest.fixture
def client(monkeypatch):
    import dataclasses

    import vfz_workschedule_availability_main as service

    # Config is a frozen dataclass, so swap the module attribute rather than
    # mutating it. require_token reads CONFIG at call time, so this takes.
    monkeypatch.setattr(
        service, "CONFIG", dataclasses.replace(service.CONFIG, api_token=TOKEN)
    )
    loader = RecordingLoader()
    monkeypatch.setattr(service, "CACHE", NonWorkingCache(loader, ttl_seconds=60))
    monkeypatch.setattr(service, "today", lambda: MONDAY)
    monkeypatch.setattr(service, "_served", 0, raising=False)
    # Startup warms the pool with a real ping. Stubbed, or every test in this
    # file would open a connection to the live RDS instance -- which it did,
    # and turned a sub-second suite into a 99-second one.
    monkeypatch.setattr(service._database, "ping", lambda: None)

    with TestClient(service.app) as test_client:
        yield test_client, service, loader


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_the_response_shape_workato_depends_on(client, auth):
    test_client, service, _ = client

    response = test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["date"] == "2026-08-24"
    assert body["count"] == 2
    # userIds is the quick membership check; users carries the detail.
    assert body["userIds"] == ["KUATAIRI", "KUAJASON"]
    assert body["users"][0]["userId"] == "KUATAIRI"
    assert body["users"][0]["user"] == "Abigail Hlalele"
    assert body["stale"] is False
    assert "Cache-Control" in response.headers


def test_a_repeat_call_is_cached(client, auth):
    test_client, service, loader = client

    test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)
    second = test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)

    assert loader.calls == 1
    assert second.json()["cached"] is True


def test_no_token_is_rejected(client):
    test_client, service, _ = client

    assert test_client.get(f"{service.AVAILABILITY_PATH}/").status_code == 401


def test_a_wrong_token_is_rejected(client):
    test_client, service, _ = client

    response = test_client.get(
        f"{service.AVAILABILITY_PATH}/", headers={"Authorization": "Bearer nope"}
    )

    assert response.status_code == 401


def test_a_malformed_header_is_a_401_not_a_500(client):
    test_client, service, _ = client

    response = test_client.get(
        f"{service.AVAILABILITY_PATH}/", headers={"Authorization": "Basic zzz"}
    )

    assert response.status_code == 401


def test_a_database_failure_is_503_not_an_empty_list(client, auth, monkeypatch):
    """The single most important behaviour here.

    An empty list would tell Workato nobody is off, silently suppressing the
    warning it asked for.
    """
    test_client, service, _ = client
    monkeypatch.setattr(
        service,
        "CACHE",
        NonWorkingCache(RecordingLoader(error=RuntimeError("down")), ttl_seconds=60),
    )

    response = test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)

    assert response.status_code == 503
    assert "userIds" not in response.json()


def test_an_empty_list_from_a_healthy_database_is_a_200(client, auth, monkeypatch):
    """Most weekdays nobody is off, and that is a real answer, not a failure."""
    test_client, service, _ = client
    monkeypatch.setattr(
        service, "CACHE", NonWorkingCache(RecordingLoader(rows=[]), ttl_seconds=60)
    )

    response = test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["userIds"] == []


def test_hello_needs_no_token(client):
    test_client, service, _ = client

    body = test_client.get("/hello").json()

    assert body["status"] == "alive"
    assert body["port"] == 5009


def test_invalidate_clears_the_cache(client, auth):
    test_client, service, loader = client
    test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)

    test_client.post(f"{service.AVAILABILITY_PATH}/invalidate", headers=auth)
    test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)

    assert loader.calls == 2


def test_status_reports_the_cache(client, auth):
    test_client, service, _ = client
    test_client.get(f"{service.AVAILABILITY_PATH}/", headers=auth)

    body = test_client.get(f"{service.AVAILABILITY_PATH}/status", headers=auth).json()

    assert body["status"] == "ok"
    assert body["cache"]["cachedRows"] == 2
    assert body["cache"]["ttlSeconds"] == 60
