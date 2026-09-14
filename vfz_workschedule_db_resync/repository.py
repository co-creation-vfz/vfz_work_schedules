"""MySQL access for work-schedule non-working entries.

Row shape (one row per user per non-working date):

    user_id             "KUATAIRI"
    user_name           "Abigail Hlalele"
    work_schedule_title "Testing"
    work_schedule_id    "IEAFXAOHMIACBDCL"
    date                DATE 2026-08-25
    seeded_by           NULL, or "testkit" for a row written by a test tool
    created_at          2026-08-25 00:00:00

The presence of a row means the user is NOT working that date. Absence means
the user is working. That is the whole rule this service enforces, and it
replaces the lookup-table step in the Workato recipe.

Every SQL statement in this service lives in this module, and every one is
parameterised. The populator and the CLI scripts call the methods below rather
than writing their own queries, so there is exactly one place to look when the
schema changes -- and exactly one place for a test double to stand in.

The unique key on (user_id, date) is the real guarantee against a
double-entered day: the read-side de-duplication below is a belt-and-braces
second line for data that predates the constraint.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date as Date
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import database_helpers
import non_working_days
from database_helpers import DatabaseConnection, DatabaseError  # noqa: F401

# Users per stale-cleanup statement. The pair list inside one grows with
# users x window days, so chunking keeps a long window over a large account
# well clear of max_allowed_packet.
_STALE_USER_CHUNK = 100

# The Emails job reads this same table. The name and the DDL both live in
# shared/database_helpers.py, so the two integrations cannot drift apart.
DEFAULT_TABLE = database_helpers.NON_WORKING_DAYS_TABLE

# The run log. Written at the end of every run, read by the dashboard's "last
# sync". Same story: the name and the DDL live in shared/database_helpers.py.
DEFAULT_RUNS_TABLE = database_helpers.RESYNC_RUNS_TABLE

# The columns every read returns, in the shape the service expects. Aliased to
# the camelCase keys the API models and the Workato payloads already use, so the
# mapping lives in one string rather than in every call site.
_SELECT_COLUMNS = (
    "user_id AS userId, "
    "user_name AS user, "
    "work_schedule_title AS workScheduleTitle, "
    "work_schedule_id AS workScheduleId, "
    "`date` AS date"
)


def to_date_key(value: Date) -> str:
    """Render a date the way MySQL takes it in a parameterised comparison."""
    return value.strftime("%Y-%m-%d")


def _date_string(value: Any) -> Optional[str]:
    """Normalise whatever the driver returned for a DATE column to a string.

    mysql-connector hands back ``datetime.date``; a hand-written row or a test
    double may hold the ISO string already. The API contract is a string either
    way, and a caller comparing against ``"2026-08-25"`` must not silently see
    a ``date`` object instead.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, Date):
        return value.isoformat()
    return str(value)


class WorkScheduleRepository:
    """Read and write access to the non-working-day table."""

    def __init__(
        self,
        database: DatabaseConnection,
        table: str = DEFAULT_TABLE,
        runs_table: str = DEFAULT_RUNS_TABLE,
    ) -> None:
        self._db = database
        # Interpolated into SQL rather than parameterised, because an identifier
        # cannot be a bind parameter. It comes from configuration, never from a
        # request, and is quoted; validated here so a typo cannot become an
        # injection point.
        for name in (table, runs_table):
            if not name.replace("_", "").isalnum():
                raise ValueError(f"Unsafe table name: {name!r}")
        self._table = table
        self._runs_table = runs_table

    @property
    def table(self) -> str:
        return self._table

    @property
    def runs_table(self) -> str:
        return self._runs_table

    def close(self) -> None:
        """Drop the underlying connection pool. For a script that owns it."""
        self._db.close()

    # -- setup -------------------------------------------------------------
    def ensure_schema(self) -> None:
        """Create both tables and their indexes if absent. Safe to repeat.

        The DDL itself lives in ``shared/database_helpers``, alongside the
        Emails job's tables. That is deliberate: the non-working-day table is
        the contract between the two integrations, and two copies of its
        definition would eventually disagree -- with the Emails job silently
        reading a column this one had stopped writing.

        The run log is created here too, so an existing deployment grows it on
        the next run rather than needing a migration step somebody has to
        remember.
        """
        ok, message, _ = self._db.create_table(
            self._table, database_helpers.non_working_days_ddl(self._table)
        )
        if not ok:
            raise DatabaseError(message)
        self.ensure_runs_schema()

    def ensure_runs_schema(self) -> None:
        """Create just the run log. Safe to call repeatedly.

        Separate from ``ensure_schema`` because the recorder runs at the end of
        every run, including ones that never opened the non-working table at
        all (an unreadable schedule source, say) -- and those still have a
        result worth showing on the dashboard.
        """
        ok, message, _ = self._db.create_table(
            self._runs_table, database_helpers.resync_runs_ddl(self._runs_table)
        )
        if not ok:
            raise DatabaseError(message)

    # -- run log -----------------------------------------------------------
    def record_run(self, d_result: Dict[str, Any]) -> None:
        """Append one finished run to the run log.

        Called from ``resync._finish``, which is the single exit point every
        run passes through -- cron, dashboard and CLI alike. That is the whole
        point: the service used to keep its last run in a module variable, so a
        cron run (its own process, its own memory) never showed up on the
        dashboard and "last sync" meant "last time somebody clicked".

        The full result goes into ``result`` as JSON, with the handful of fields
        the dashboard sorts and filters on lifted out into real columns.
        """
        started_at = _as_naive_datetime(d_result.get("startedAt"))
        finished_at = _as_naive_datetime(d_result.get("finishedAt"))

        with self._db.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{self._runs_table}` "
                "(status, exit_code, triggered_by, message, started_at, "
                " finished_at, duration_seconds, rows_written, dry_run, result) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(d_result.get("status") or "attention")[:16],
                    int(d_result.get("exitCode") or 0),
                    (str(d_result.get("triggeredBy"))[:32]
                     if d_result.get("triggeredBy") else None),
                    (str(d_result.get("message"))[:1024]
                     if d_result.get("message") else None),
                    started_at,
                    finished_at,
                    d_result.get("durationSeconds"),
                    d_result.get("inserted"),
                    1 if d_result.get("dryRun") else 0,
                    json.dumps(d_result, default=str),
                ),
            )

    def latest_run(self, status: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The most recent logged run, or the most recent one with ``status``.

        ``latest_run()`` answers "when did anything last happen";
        ``latest_run("success")`` answers "when was the table last actually
        written", which is the one a person means by "last sync".
        """
        clause = "WHERE status = %s " if status else ""
        params = (status,) if status else ()
        with self._db.cursor() as cur:
            cur.execute(
                f"SELECT id, status, exit_code, triggered_by, message, "
                f"started_at, finished_at, duration_seconds, rows_written, "
                f"dry_run, result FROM `{self._runs_table}` "
                f"{clause}ORDER BY id DESC LIMIT 1",
                params,
            )
            row = cur.fetchone()
        return _run_row(row)

    def count_runs(self) -> int:
        """Rows in the run log. Used by the tests and by /health."""
        with self._db.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM `{self._runs_table}`")
            row = cur.fetchone()
        return int((row or {}).get("total", 0))

    # -- queries -----------------------------------------------------------
    def find_non_working(
        self, user_ids: Sequence[str], on_date: Date
    ) -> List[Dict[str, Any]]:
        """Return one entry per user (of ``user_ids``) not working on ``on_date``.

        Duplicate rows for the same user and date are collapsed, so a
        double-entered day cannot produce a duplicated name in the Wrike comment.
        """
        if not user_ids:
            return []

        placeholders = ", ".join(["%s"] * len(user_ids))
        with self._db.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` "
                f"WHERE `date` = %s AND user_id IN ({placeholders})",
                (to_date_key(on_date), *user_ids),
            )
            rows = cur.fetchall()
        return _dedupe_by_user(_normalise(rows))

    def find_non_working_days(
        self, user_id: str, date_from: Date, date_to: Date
    ) -> List[Dict[str, Any]]:
        """Return a user's non-working entries between two dates, inclusive."""
        with self._db.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` "
                f"WHERE user_id = %s AND `date` BETWEEN %s AND %s "
                f"ORDER BY `date` ASC",
                (user_id, to_date_key(date_from), to_date_key(date_to)),
            )
            rows = cur.fetchall()
        return _normalise(rows)

    def rows_for_date(self, on_date: Date) -> List[Dict[str, Any]]:
        """Everyone recorded as not working on one date.

        Delegated to ``shared/non_working_days.py``: three integrations read
        this table, and the column list is defined once so a rename cannot
        leave one of them behind.
        """
        return non_working_days.rows_for_date(self._db, self._table, on_date)

    def count_rows(self) -> int:
        """Total rows in the table. Used by /health as a real round trip."""
        with self._db.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM `{self._table}`")
            row = cur.fetchone()
        return int((row or {}).get("total", 0))

    def user_ids_in_window(self, date_from: Date, date_to: Date) -> List[str]:
        """Every user with at least one row inside the window.

        The populator needs this to spot users it did not account for this run.
        """
        with self._db.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT user_id FROM `{self._table}` "
                f"WHERE `date` BETWEEN %s AND %s",
                (to_date_key(date_from), to_date_key(date_to)),
            )
            rows = cur.fetchall()
        return sorted(str(row["user_id"]) for row in rows)

    def count_stale(
        self, user_id: str, date_from: Date, date_to: Date, keep: Iterable[Date]
    ) -> int:
        """How many of this user's in-window rows a cleanup would delete."""
        return self.count_stale_for({user_id: list(keep)}, date_from, date_to)

    def count_stale_for(
        self, keep_by_user: Dict[str, Sequence[Date]], date_from: Date, date_to: Date
    ) -> int:
        """How many rows a cleanup would delete, across every given user.

        Batched for the same reason as ``delete_stale_for`` below.
        """
        total = 0
        for clause, params in self._stale_clauses(keep_by_user, date_from, date_to):
            with self._db.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) AS total FROM `{self._table}` WHERE {clause}",
                    params,
                )
                row = cur.fetchone()
            total += int((row or {}).get("total", 0))
        return total

    # -- writes ------------------------------------------------------------
    def upsert_non_working(self, entries: Sequence[Dict[str, Any]]) -> tuple[int, int]:
        """Insert or refresh one row per entry, keyed on (user_id, date).

        Returns ``(inserted, matched)``. ``created_at`` is left to the column
        default, so it is stamped once on insert by the database's own clock and
        an update never rewrites the audit trail.

        Each entry is a dict with ``user_id``, ``user``, ``work_schedule_title``,
        ``work_schedule_id`` and ``date``.

        Counted per statement rather than from ``cursor.rowcount`` on an
        ``executemany``: MySQL reports 1 for an insert and 2 for an update in
        that total, which cannot be split back into the two numbers the caller
        reports.
        """
        if not entries:
            return 0, 0

        statement = (
            f"INSERT INTO `{self._table}` "
            f"(user_id, user_name, work_schedule_title, work_schedule_id, `date`) "
            f"VALUES (%s, %s, %s, %s, %s) "
            f"ON DUPLICATE KEY UPDATE "
            f"user_name = VALUES(user_name), "
            f"work_schedule_title = VALUES(work_schedule_title), "
            f"work_schedule_id = VALUES(work_schedule_id)"
        )

        inserted = matched = 0
        with self._db.cursor(dictionary=False, commit=True) as cur:
            for entry in entries:
                cur.execute(
                    statement,
                    (
                        entry["user_id"],
                        entry.get("user"),
                        entry.get("work_schedule_title"),
                        entry.get("work_schedule_id"),
                        to_date_key(entry["date"]),
                    ),
                )
                # 1 = inserted, 2 = an existing row was changed, 0 = unchanged.
                if cur.rowcount == 1:
                    inserted += 1
                else:
                    matched += 1
        return inserted, matched

    def replace_all(self, entries: Sequence[Dict[str, Any]]) -> tuple:
        """Delete EVERY row and insert the given set, in ONE transaction.

        What a cron run does. The table is not a history: it holds exactly the
        people who are not working in the window this run covers -- today, for
        the cron -- and nothing else. No stale rows, no orphans left behind by
        a user dropped from every schedule, and no accumulating past dates.

        The consequence, stated plainly because it is easy to trip over: a run
        for one date wipes every other date. That is intended here, but it does
        mean the window is the whole content of the table, so a support run
        with ``--from/--to`` replaces today's answer with that window's.

        DELETE, not TRUNCATE. TRUNCATE is DDL in MySQL and commits implicitly,
        so it cannot be rolled back -- a failure part-way through the inserts
        would leave the table empty, and an empty table reads as "everyone is
        working" to the Emails job. DELETE keeps the whole swap atomic, so a
        failed run leaves yesterday's answer in place rather than no answer.

        ``created_at`` is stamped fresh on every run here, which is inherent to
        a replace: there is no surviving row to preserve it from.

        :returns: (inserted, removed)
        """
        with self._db.cursor(dictionary=False, commit=True) as cur:
            cur.execute(f"DELETE FROM `{self._table}`")
            removed = cur.rowcount

            # executemany, not a statement each: the driver sends one
            # multi-row INSERT, so a schedule change that puts a hundred people
            # off costs one round trip rather than a hundred. Safe here because
            # the table was just emptied, so every row is an insert and the
            # count is simply how many were handed in -- no need for a
            # per-statement rowcount to tell insert from update.
            l_values = [
                (
                    entry["user_id"],
                    entry.get("user"),
                    entry.get("work_schedule_title"),
                    entry.get("work_schedule_id"),
                    to_date_key(entry["date"]),
                )
                for entry in entries
            ]
            if l_values:
                cur.executemany(
                    f"INSERT INTO `{self._table}` "
                    f"(user_id, user_name, work_schedule_title, "
                    f"work_schedule_id, `date`) VALUES (%s, %s, %s, %s, %s)",
                    l_values,
                )

        return len(l_values), removed

    def delete_stale(
        self, user_id: str, date_from: Date, date_to: Date, keep: Iterable[Date]
    ) -> int:
        """Delete this user's in-window rows other than the ones to keep."""
        return self.delete_stale_for({user_id: list(keep)}, date_from, date_to)

    def delete_stale_for(
        self, keep_by_user: Dict[str, Sequence[Date]], date_from: Date, date_to: Date
    ) -> int:
        """Delete every given user's in-window rows other than the ones to keep.

        This is what corrects a stale row: if someone works Tuesday but a
        Tuesday row exists for them, the Tuesday run expands their pattern to
        *no* dates, so the row is not in ``keep`` and is deleted.

        Batched rather than one statement per user. The Wrike account has 224
        people on a schedule, and a query each meant 224 sequential round trips
        to RDS -- which took a resync well past two minutes and made it look
        hung. Grouped, it is a handful of statements regardless of headcount.
        """
        deleted = 0
        for clause, params in self._stale_clauses(keep_by_user, date_from, date_to):
            with self._db.cursor(dictionary=False, commit=True) as cur:
                cur.execute(f"DELETE FROM `{self._table}` WHERE {clause}", params)
                deleted += cur.rowcount
        return deleted

    def delete_users_in_window(
        self, user_ids: Sequence[str], date_from: Date, date_to: Date
    ) -> int:
        """Delete every in-window row belonging to the given users.

        Only reached by an explicit ``--purge-orphans``: it removes days for
        users the source no longer mentions at all.
        """
        if not user_ids:
            return 0
        placeholders = ", ".join(["%s"] * len(user_ids))
        with self._db.cursor(dictionary=False, commit=True) as cur:
            cur.execute(
                f"DELETE FROM `{self._table}` "
                f"WHERE user_id IN ({placeholders}) AND `date` BETWEEN %s AND %s",
                (*user_ids, to_date_key(date_from), to_date_key(date_to)),
            )
            return cur.rowcount

    # -- internals ---------------------------------------------------------
    def _stale_clauses(
        self,
        keep_by_user: Dict[str, Sequence[Date]],
        date_from: Date,
        date_to: Date,
    ):
        """Build the WHERE clauses selecting rows a cleanup should remove.

        One clause per chunk of users: "in the window, belonging to one of
        these users, and not one of the (user, date) pairs to keep". Chunked so
        a long window over a large account cannot build a statement big enough
        to hit max_allowed_packet -- the pair list grows with users x days.

        The table is assumed to hold schedule-derived days only. Once leave or
        holidays are stored here too, tag them (for example
        ``seeded_by = 'leave'``) and exclude that tag here, or a run will
        remove them.

        :returns: Iterator of (clause, params) to run in turn.
        """
        l_users = sorted(keep_by_user)
        for start in range(0, len(l_users), _STALE_USER_CHUNK):
            chunk = l_users[start : start + _STALE_USER_CHUNK]
            placeholders = ", ".join(["%s"] * len(chunk))
            clause = (
                f"user_id IN ({placeholders}) AND `date` BETWEEN %s AND %s"
            )
            params: List[Any] = [
                *chunk,
                to_date_key(date_from),
                to_date_key(date_to),
            ]

            l_pairs = sorted(
                {
                    (user_id, to_date_key(day))
                    for user_id in chunk
                    for day in keep_by_user[user_id]
                }
            )
            if l_pairs:
                pair_sql = ", ".join(["(%s, %s)"] * len(l_pairs))
                clause += f" AND (user_id, `date`) NOT IN ({pair_sql})"
                for user_id, date_key in l_pairs:
                    params.extend((user_id, date_key))

            yield clause, tuple(params)


def _normalise(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Render DATE columns as ISO strings, leaving the rest untouched."""
    normalised = []
    for row in rows:
        entry = dict(row)
        entry["date"] = _date_string(entry.get("date"))
        normalised.append(entry)
    return normalised


def _dedupe_by_user(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        user_id = entry.get("userId")
        if user_id and user_id not in seen:
            seen[user_id] = entry
    return list(seen.values())


__all__ = [
    "DEFAULT_TABLE",
    "DatabaseError",
    "WorkScheduleRepository",
    "to_date_key",
]


def _as_naive_datetime(value: Any) -> Optional[datetime]:
    """ISO string (or datetime) to a naive local datetime MySQL will accept.

    The run result carries timezone-aware ISO strings, because the run itself
    reasons in the configured zone. A DATETIME column stores no offset, so the
    offset is applied and then dropped rather than silently rejected by the
    driver.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return moment.replace(tzinfo=None) if moment.tzinfo else moment


def _run_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One run-log row in the shape the API returns.

    The stored ``result`` is the run's own dict, so it is returned as-is when it
    parses -- a dashboard reading it sees exactly what the log line carried.
    The columns are the fallback for a row written by an older version, or one
    whose JSON somehow will not parse.
    """
    if not row:
        return None

    d_result: Dict[str, Any] = {}
    raw = row.get("result")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if isinstance(parsed, dict):
                d_result = dict(parsed)
        except (ValueError, TypeError):
            d_result = {}

    d_result.setdefault("status", row.get("status"))
    d_result.setdefault("exitCode", row.get("exit_code"))
    d_result.setdefault("triggeredBy", row.get("triggered_by"))
    d_result.setdefault("message", row.get("message"))
    d_result.setdefault("durationSeconds", row.get("duration_seconds"))
    for key, column in (("startedAt", "started_at"), ("finishedAt", "finished_at")):
        moment = row.get(column)
        d_result.setdefault(
            key, moment.isoformat() if isinstance(moment, datetime) else moment
        )

    # Always from the row, never from the blob: this is the durable record's
    # own identity, and it is what tells two runs apart in the dashboard.
    d_result["runId"] = row.get("id")
    # Where this came from, so a reader never has to wonder whether they are
    # looking at live memory or the table.
    d_result["recordedAt"] = (
        row["finished_at"].isoformat()
        if isinstance(row.get("finished_at"), datetime)
        else row.get("finished_at")
    )
    return d_result
