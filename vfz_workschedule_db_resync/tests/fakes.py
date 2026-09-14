"""An in-memory stand-in for WorkScheduleRepository.

The suite used to run against mongomock, which let it exercise real driver
query syntax with no server. MySQL has no equivalent, and pointing the tests at
a real database would make them need credentials and network to run at all.

So the seam moved one layer up: all SQL now lives in ``WorkScheduleRepository``
(``app/repository.py``), and this implements that same small interface over a
list of dicts. Everything above the repository -- the service, the populator,
the API, the CLI -- is therefore tested exactly as it ships.

What this deliberately does NOT cover is whether the SQL itself is right. That
is what ``test_repository_mysql.py`` is for; it runs the real statements against
a real MySQL and skips itself when none is configured.

Rows are stored in the repository's own read shape (camelCase keys, ``date`` as
an ISO string) so a test asserting on what came back sees what the real
repository returns.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Any, Dict, Iterable, List, Sequence

from repository import DEFAULT_RUNS_TABLE, DEFAULT_TABLE, to_date_key


class InMemoryWorkScheduleRepository:
    """Same public surface as WorkScheduleRepository, backed by a list."""

    def __init__(
        self,
        rows: Sequence[Dict[str, Any]] = (),
        table: str = DEFAULT_TABLE,
        runs_table: str = DEFAULT_RUNS_TABLE,
    ) -> None:
        self._rows: List[Dict[str, Any]] = [dict(row) for row in rows]
        self._table = table
        self._runs_table = runs_table
        # The run log, oldest first, exactly as the real table orders by id.
        self._runs: List[Dict[str, Any]] = []
        self.schema_calls = 0
        self.runs_schema_calls = 0

    # -- test helpers (not part of the real interface) ---------------------
    @property
    def rows(self) -> List[Dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def count(self, **match: Any) -> int:
        return len(self.find(**match))

    def find(self, **match: Any) -> List[Dict[str, Any]]:
        return [
            dict(row)
            for row in self._rows
            if all(row.get(key) == value for key, value in match.items())
        ]

    def insert(self, row: Dict[str, Any] = None, **fields: Any) -> None:
        """Put a row in directly, standing in for data written by something else.

        Takes either a dict or keyword arguments, whichever reads better at the
        call site.
        """
        entry = {**(row or {}), **fields}
        if isinstance(entry.get("date"), Date):
            entry["date"] = entry["date"].isoformat()
        self._rows.append(entry)

    def find_one(self, **match: Any) -> Dict[str, Any]:
        """The first matching row, or None."""
        matches = self.find(**match)
        return matches[0] if matches else None

    @property
    def runs(self) -> List[Dict[str, Any]]:
        """The run log as recorded, oldest first."""
        return [dict(run) for run in self._runs]

    # -- the repository interface -----------------------------------------
    @property
    def table(self) -> str:
        return self._table

    @property
    def runs_table(self) -> str:
        return self._runs_table

    def close(self) -> None:
        pass

    def ensure_schema(self) -> None:
        self.schema_calls += 1
        self.ensure_runs_schema()

    def ensure_runs_schema(self) -> None:
        self.runs_schema_calls += 1

    # -- run log -----------------------------------------------------------
    def record_run(self, d_result: Dict[str, Any]) -> None:
        self._runs.append(dict(d_result))

    def latest_run(self, status: str = None) -> Dict[str, Any]:
        for index in range(len(self._runs) - 1, -1, -1):
            run = self._runs[index]
            if status is None or run.get("status") == status:
                # runId is the real table's identity column; the fake numbers
                # rows the same way so a test can tell two runs apart.
                return {**run, "runId": index + 1}
        return None

    def count_runs(self) -> int:
        return len(self._runs)

    def find_non_working(
        self, user_ids: Sequence[str], on_date: Date
    ) -> List[Dict[str, Any]]:
        if not user_ids:
            return []
        wanted = set(user_ids)
        key = to_date_key(on_date)
        matches = [
            dict(row)
            for row in self._rows
            if row.get("date") == key and row.get("userId") in wanted
        ]
        # The real repository collapses a double-entered day; so must this, or a
        # test would pass here and produce a duplicated name in production.
        seen: Dict[str, Dict[str, Any]] = {}
        for row in matches:
            seen.setdefault(row["userId"], row)
        return list(seen.values())

    def find_non_working_days(
        self, user_id: str, date_from: Date, date_to: Date
    ) -> List[Dict[str, Any]]:
        start, end = to_date_key(date_from), to_date_key(date_to)
        matches = [
            dict(row)
            for row in self._rows
            if row.get("userId") == user_id and start <= str(row.get("date")) <= end
        ]
        return sorted(matches, key=lambda row: str(row.get("date")))

    def rows_for_date(self, on_date: Date) -> List[Dict[str, Any]]:
        key = to_date_key(on_date)
        matches = [dict(row) for row in self._rows if row.get("date") == key]
        seen: Dict[str, Dict[str, Any]] = {}
        for row in matches:
            if row.get("userId"):
                seen.setdefault(row["userId"], row)
        return sorted(
            seen.values(), key=lambda row: (row.get("user") or "", row["userId"])
        )

    def count_rows(self) -> int:
        return len(self._rows)

    def user_ids_in_window(self, date_from: Date, date_to: Date) -> List[str]:
        start, end = to_date_key(date_from), to_date_key(date_to)
        return sorted(
            {
                str(row["userId"])
                for row in self._rows
                if row.get("userId") and start <= str(row.get("date")) <= end
            }
        )

    def count_stale(
        self, user_id: str, date_from: Date, date_to: Date, keep: Iterable[Date]
    ) -> int:
        return len(self._stale(user_id, date_from, date_to, keep))

    def count_stale_for(self, keep_by_user, date_from: Date, date_to: Date) -> int:
        return sum(
            len(self._stale(user_id, date_from, date_to, keep))
            for user_id, keep in keep_by_user.items()
        )

    def delete_stale_for(self, keep_by_user, date_from: Date, date_to: Date) -> int:
        return sum(
            self.delete_stale(user_id, date_from, date_to, keep)
            for user_id, keep in keep_by_user.items()
        )

    def upsert_non_working(self, entries: Sequence[Dict[str, Any]]) -> tuple:
        inserted = matched = 0
        for entry in entries:
            key = to_date_key(entry["date"])
            existing = next(
                (
                    row
                    for row in self._rows
                    if row.get("userId") == entry["user_id"] and row.get("date") == key
                ),
                None,
            )
            fields = {
                "userId": entry["user_id"],
                "user": entry.get("user"),
                "workScheduleTitle": entry.get("work_schedule_title"),
                "workScheduleId": entry.get("work_schedule_id"),
                "date": key,
            }
            if existing is None:
                # created_at is stamped once on insert and never rewritten,
                # mirroring the column default the real table relies on.
                self._rows.append({**fields, "createdAt": f"{key} 00:00:00"})
                inserted += 1
            else:
                existing.update(fields)
                matched += 1
        return inserted, matched

    def replace_all(self, entries: Sequence[Dict[str, Any]]) -> tuple:
        removed = len(self._rows)
        self._rows = []
        inserted, _ = self.upsert_non_working(entries)
        return inserted, removed

    def delete_stale(
        self, user_id: str, date_from: Date, date_to: Date, keep: Iterable[Date]
    ) -> int:
        stale = self._stale(user_id, date_from, date_to, keep)
        self._rows = [row for row in self._rows if row not in stale]
        return len(stale)

    def delete_users_in_window(
        self, user_ids: Sequence[str], date_from: Date, date_to: Date
    ) -> int:
        if not user_ids:
            return 0
        wanted = set(user_ids)
        start, end = to_date_key(date_from), to_date_key(date_to)
        doomed = [
            row
            for row in self._rows
            if row.get("userId") in wanted and start <= str(row.get("date")) <= end
        ]
        self._rows = [row for row in self._rows if row not in doomed]
        return len(doomed)

    # -- internals ---------------------------------------------------------
    def _stale(
        self, user_id: str, date_from: Date, date_to: Date, keep: Iterable[Date]
    ) -> List[Dict[str, Any]]:
        start, end = to_date_key(date_from), to_date_key(date_to)
        keep_keys = {to_date_key(day) for day in keep}
        return [
            row
            for row in self._rows
            if row.get("userId") == user_id
            and start <= str(row.get("date")) <= end
            and str(row.get("date")) not in keep_keys
        ]


class BrokenRepository(InMemoryWorkScheduleRepository):
    """An unreachable database: every call raises the driver's error.

    Every method, not only the reads -- a host that is down fails the schema
    check and the writes too, and a test that only broke the reads would let a
    write path quietly "succeed" against nothing.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def ensure_schema(self):
        raise self._error

    def ensure_runs_schema(self):
        raise self._error

    def record_run(self, d_result):
        raise self._error

    def latest_run(self, status=None):
        raise self._error

    def count_runs(self):
        raise self._error

    def find_non_working(self, user_ids, on_date):
        raise self._error

    def find_non_working_days(self, user_id, date_from, date_to):
        raise self._error

    def count_rows(self):
        raise self._error

    def rows_for_date(self, on_date):
        raise self._error

    def user_ids_in_window(self, date_from, date_to):
        raise self._error

    def count_stale(self, user_id, date_from, date_to, keep):
        raise self._error

    def count_stale_for(self, keep_by_user, date_from, date_to):
        raise self._error

    def delete_stale_for(self, keep_by_user, date_from, date_to):
        raise self._error

    def upsert_non_working(self, entries):
        raise self._error

    def replace_all(self, entries):
        raise self._error

    def delete_stale(self, user_id, date_from, date_to, keep):
        raise self._error

    def delete_users_in_window(self, user_ids, date_from, date_to):
        raise self._error
