# =============================================================================
# non_working_days.py
# The canonical read of the shared non-working-day table.
#
# Three integrations read this table -- the DB Resync writes it, the Emails job
# reads today's list to find blocked approvals, and the Availability API serves
# it to Workato. The SELECT lives here rather than in each of them so the
# column names and the row shape are defined once: they have already been
# renamed once, and three copies would have meant three chances to miss one.
#
# Writes stay with the owner (vfz_workschedule_db_resync/repository.py). This is
# read-only on purpose.
# =============================================================================

from datetime import date as Date
from datetime import datetime
from typing import Any, Dict, List, Optional

# The columns every read returns, aliased to the camelCase keys the APIs and
# the Wrike-facing payloads use. One string, so the mapping is in one place.
SELECT_COLUMNS = (
    "user_id AS userId, "
    "user_name AS user, "
    "work_schedule_title AS workScheduleTitle, "
    "work_schedule_id AS workScheduleId, "
    "`date` AS date, "
    "seeded_by AS seededBy"
)


def to_date_key(value: Date) -> str:
    """Render a date the way MySQL takes it in a parameterised comparison."""
    return value.strftime("%Y-%m-%d")


def date_string(value: Any) -> Optional[str]:
    """
    Normalise whatever the driver returned for a DATE column to a string.

    mysql-connector hands back ``datetime.date``; a hand-written row or a test
    double may hold the ISO string already. Callers compare against
    ``"2026-08-25"``, so a ``date`` object slipping through would silently fail
    every comparison.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, Date):
        return value.isoformat()
    return str(value)


def normalise(rows) -> List[Dict[str, Any]]:
    """Render DATE columns as ISO strings, leaving everything else alone."""
    normalised = []
    for row in rows:
        entry = dict(row)
        if "date" in entry:
            entry["date"] = date_string(entry.get("date"))
        normalised.append(entry)
    return normalised


def dedupe_by_user(entries) -> List[Dict[str, Any]]:
    """
    Collapse duplicate rows for one user.

    The unique key on (user_id, date) stops this arising now, but rows written
    before it existed can still be there, and a duplicate would put the same
    name twice in a Wrike comment.
    """
    seen: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        user_id = entry.get("userId")
        if user_id and user_id not in seen:
            seen[user_id] = entry
    return list(seen.values())


def rows_for_date(database, table: str, on_date: Date) -> List[Dict[str, Any]]:
    """
    Everyone recorded as not working on one date.

    :param database: A shared DatabaseConnection.
    :param table:    The non-working-day table name, from segredo.ini.
    :param on_date:  The date to read.
    :returns:        List of row dicts, de-duplicated by user, ordered by name.
    """
    with database.cursor(dictionary=True) as cur:
        cur.execute(
            f"SELECT {SELECT_COLUMNS} FROM `{table}` "
            f"WHERE `date` = %s ORDER BY user_name ASC, user_id ASC",
            (to_date_key(on_date),),
        )
        rows = cur.fetchall()
    return dedupe_by_user(normalise(rows))


def user_names_for_date(database, table: str, on_date: Date) -> Dict[str, str]:
    """
    ``{wrikeUserId: "First Last"}`` for one date.

    Names come from the database rather than Wrike so a message still reads
    correctly for a contact the caller's token cannot see.
    """
    return {
        row["userId"]: (row.get("user") or "").strip()
        for row in rows_for_date(database, table, on_date)
        if row.get("userId")
    }
