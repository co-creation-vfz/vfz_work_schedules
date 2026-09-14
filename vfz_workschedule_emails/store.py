"""MySQL access.

Four tables with deliberately different lifecycles:

  work_schedule_non_working_days
                         written daily by the upstream work-schedule job
                         (vfz_workschedule_db_resync). This job only ever reads
                         it, and its test tooling seeds marked rows into it.
  approval_notifications written by this job, never expires. It is what makes
                         the "notify once per approval, per approver" rule hold
                         across hourly runs and across days.
  approval_notification_non_working_approvers
                         the set of non-working approvers each notification has
                         already covered. A child table rather than a column,
                         because it is a growing set: a later run compares
                         today's non-workers against it to tell "already
                         covered" from "a new non-worker joined".
  approval_notification_comments
                         the Wrike comment ids a notification has posted, so a
                         retraction can delete the comments it actually made.
                         A list, not a set: a corrected re-notification adds a
                         second comment to the same record.
  baselined_approvals    approvals that already existed when the job went live.
                         Wrike approvals carry no creation timestamp, so this
                         snapshot is the only way to tell "already there" from
                         "raised since". Never notified on.

Every SQL statement this job runs lives in this module, and every one is
parameterised -- the CREATE TABLE statements excepted, which live in
shared/database_helpers.py because the non-working-day table is shared with the
DB Resync and two copies of its definition would drift. The planning logic in ``notifier.py`` calls the methods below, so
a test can drive the whole run against an in-memory double.

The unique key on (approval_id, notified_approver_id) is the real dedup
guarantee: even if two runs overlap, the second write for the same pair updates
the first row instead of silently duplicating it.
"""
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import database_helpers
import non_working_days
from database_helpers import (  # noqa: F401 — DatabaseError re-exported
    DatabaseConnection,
    DatabaseError,
)

# (approvalId, notifiedApproverId) -> notification record
NotificationKey = Tuple[str, str]

# Kept camelCase, matching the keys the planning logic and the run summary
# already use, so migrating the storage did not ripple into either.
_NOTIFICATION_COLUMNS = (
    "n.id AS id, "
    "n.approval_id AS approvalId, "
    "n.notified_approver_id AS notifiedApproverId, "
    "n.task_id AS taskId, "
    "n.notified_approver_name AS notifiedApproverName, "
    "n.recipient_role AS recipientRole, "
    "n.first_notified_at AS firstNotifiedAt, "
    "n.last_notified_at AS lastNotifiedAt"
)


class Store:
    """
    Read and write access to the notifier's tables, plus a read of the shared
    non-working-day table the DB Resync owns.
    """

    def __init__(
        self,
        database: DatabaseConnection,
        non_working_table: str = database_helpers.NON_WORKING_DAYS_TABLE,
        notifications_table: str = database_helpers.APPROVAL_NOTIFICATIONS_TABLE,
        baseline_table: str = database_helpers.BASELINED_APPROVALS_TABLE,
    ) -> None:
        """
        :param database: Shared pooled connection, built from segredo.ini.
        """
        # Table names are interpolated into SQL, because an identifier cannot
        # be a bind parameter. Config validates them; this is the second line,
        # so a caller constructing a Store directly cannot smuggle SQL in.
        for name in (non_working_table, notifications_table, baseline_table):
            if not database_helpers.is_safe_table_name(name):
                raise ValueError(f"Unsafe table name: {name!r}")

        self._db = database
        self.non_working_table = non_working_table
        self.notifications_table = notifications_table
        self.baseline_table = baseline_table
        # Child tables are named after the notifications table, so pointing the
        # job at a scratch notifications table moves its children with it.
        self.non_working_link_table = f"{notifications_table}_non_working"
        self.comments_table = f"{notifications_table}_comments"

    def close(self) -> None:
        """Release the pool. Called at the end of a run."""
        self._db.close()

    # -- plumbing ----------------------------------------------------------

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Run one SELECT and return every row as a dict."""
        with self._db.cursor(dictionary=True) as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchall()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Tuple[int, Any]:
        """Run one statement in a transaction, returning (rowcount, lastrowid)."""
        with self._db.cursor(dictionary=False, commit=True) as cur:
            cur.execute(sql, tuple(params))
            return cur.rowcount, cur.lastrowid

    @staticmethod
    def _placeholders(values: Sequence[Any]) -> str:
        return ", ".join(["%s"] * len(values))

    # -- setup -------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create this job's tables if absent. Safe to call on every run.

        The DDL lives in ``shared/database_helpers.all_ddls()``, alongside
        the DB Resync's table. That is deliberate: the non-working-day table is
        the contract between the two integrations, and two copies of its
        definition would eventually disagree -- with this job silently reading
        a column the resync had stopped writing.

        The shared table is included here too, so a fresh environment works
        whichever service starts first. The resync owns it.
        """
        ok, message, _ = self._db.create_tables(
            database_helpers.all_ddls(
                non_working_table=self.non_working_table,
                notifications_table=self.notifications_table,
                baseline_table=self.baseline_table,
            )
        )
        if not ok:
            raise DatabaseError(message)

    # -- reads -------------------------------------------------------------

    def non_working_users_for(self, date_iso: str) -> Dict[str, str]:
        """Return {wrikeUserId: "First Last"} for the given YYYY-MM-DD.

        Names come from the database rather than Wrike so the message still
        reads correctly for a contact the token cannot see. The read itself is
        in ``shared/non_working_days.py``, shared with the DB Resync and the
        Availability API so the column list is defined once.
        """
        from datetime import date as _Date

        return non_working_days.user_names_for_date(
            self._db, self.non_working_table, _Date.fromisoformat(date_iso)
        )

    def notifications_for_approvals(
        self, approval_ids: Sequence[str]
    ) -> Dict[NotificationKey, dict]:
        """Every notification record for these approvals, with its child sets.

        One query per table rather than a join: a join would repeat each parent
        row once per covered approver and once per comment, and the planning
        logic wants the sets whole, keyed the way it looks them up.
        """
        unique_ids = list(set(approval_ids))
        if not unique_ids:
            return {}

        rows = self._query(
            f"SELECT {_NOTIFICATION_COLUMNS} FROM `{self.notifications_table}` AS n "
            f"WHERE n.approval_id IN ({self._placeholders(unique_ids)})",
            unique_ids,
        )
        if not rows:
            return {}

        by_id = {int(row["id"]): row for row in rows}
        record_ids = list(by_id)

        for row in rows:
            row["nonWorkingApproverIds"] = []
            row["commentIds"] = []

        covered = self._query(
            f"SELECT notification_id, approver_id FROM `{self.non_working_link_table}` "
            f"WHERE notification_id IN ({self._placeholders(record_ids)}) "
            f"ORDER BY approver_id ASC",
            record_ids,
        )
        for link in covered:
            by_id[int(link["notification_id"])]["nonWorkingApproverIds"].append(
                link["approver_id"]
            )

        comments = self._query(
            f"SELECT notification_id, comment_id FROM `{self.comments_table}` "
            f"WHERE notification_id IN ({self._placeholders(record_ids)}) "
            f"ORDER BY id ASC",
            record_ids,
        )
        for comment in comments:
            by_id[int(comment["notification_id"])]["commentIds"].append(
                comment["comment_id"]
            )

        return {
            (row["approvalId"], row["notifiedApproverId"]): row for row in rows
        }

    def notification_records(
        self,
        approval_ids: Sequence[str] = (),
        task_ids: Sequence[str] = (),
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Records matching either filter, newest first. For retract and testkit.

        With neither filter, every record. ``limit`` is applied after ordering,
        so "the 20 most recent" means what it says.
        """
        clause = ""
        params: List[Any] = []
        if approval_ids:
            unique = list(set(approval_ids))
            clause = f"WHERE n.approval_id IN ({self._placeholders(unique)})"
            params = unique
        elif task_ids:
            unique = list(set(task_ids))
            clause = f"WHERE n.task_id IN ({self._placeholders(unique)})"
            params = unique

        sql = (
            f"SELECT {_NOTIFICATION_COLUMNS} FROM `{self.notifications_table}` AS n "
            f"{clause} ORDER BY n.last_notified_at DESC, n.id DESC"
        )
        if limit is not None:
            sql += " LIMIT %s"
            params = [*params, int(limit)]

        rows = self._query(sql, params)
        if not rows:
            return []

        record_ids = [int(row["id"]) for row in rows]
        by_id = {int(row["id"]): row for row in rows}
        for row in rows:
            row["nonWorkingApproverIds"] = []
            row["commentIds"] = []

        for link in self._query(
            f"SELECT notification_id, approver_id FROM `{self.non_working_link_table}` "
            f"WHERE notification_id IN ({self._placeholders(record_ids)}) "
            f"ORDER BY approver_id ASC",
            record_ids,
        ):
            by_id[int(link["notification_id"])]["nonWorkingApproverIds"].append(
                link["approver_id"]
            )

        for comment in self._query(
            f"SELECT notification_id, comment_id FROM `{self.comments_table}` "
            f"WHERE notification_id IN ({self._placeholders(record_ids)}) "
            f"ORDER BY id ASC",
            record_ids,
        ):
            by_id[int(comment["notification_id"])]["commentIds"].append(
                comment["comment_id"]
            )

        return self._localise(rows)

    def _localise(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Stamp naive DATETIMEs with the zone they were written in.

        MySQL hands back a DATETIME with no offset, and serialised straight to
        JSON that is "2026-08-31T11:47:08" -- which a browser reads as its own
        local time. Attaching the offset the session writes in is what stops a
        timestamp being displayed two hours away from the run that produced it.

        In place, then returned, because the caller already owns these rows.
        """
        zone = ZoneInfo(self._db.zone) if self._db.zone else timezone.utc
        for row in rows:
            for key in ("firstNotifiedAt", "lastNotifiedAt"):
                value = row.get(key)
                if isinstance(value, datetime) and value.tzinfo is None:
                    row[key] = value.replace(tzinfo=zone)
        return rows

    def baselined_ids(self, approval_ids: Sequence[str]) -> set:
        """Which of these approvals predate go-live and must stay silent."""
        unique_ids = list(set(approval_ids))
        if not unique_ids:
            return set()
        rows = self._query(
            f"SELECT approval_id FROM `{self.baseline_table}` "
            f"WHERE approval_id IN ({self._placeholders(unique_ids)})",
            unique_ids,
        )
        return {row["approval_id"] for row in rows}

    def baseline_count(self) -> int:
        rows = self._query(f"SELECT COUNT(*) AS total FROM `{self.baseline_table}`")
        return int(rows[0]["total"]) if rows else 0

    def non_working_rows_for(self, date_iso: str) -> List[Dict[str, Any]]:
        """Full non-working rows for a date, including the seeded marker.

        ``non_working_users_for`` gives the run what it needs; this gives the
        testkit's ``list`` command enough to say which rows it wrote itself.
        """
        return self._query(
            f"SELECT user_id, user_name, work_schedule_title, work_schedule_id, "
            f"seeded_by FROM `{self.non_working_table}` "
            f"WHERE `date` = %s ORDER BY user_name ASC",
            (date_iso,),
        )

    def non_working_row(self, user_id: str, date_iso: str) -> Optional[Dict[str, Any]]:
        rows = self._query(
            f"SELECT user_id, user_name, seeded_by FROM `{self.non_working_table}` "
            f"WHERE user_id = %s AND `date` = %s",
            (user_id, date_iso),
        )
        return rows[0] if rows else None

    # -- writes ------------------------------------------------------------

    def add_baseline(self, approval_id: str, task_id: str) -> bool:
        """Record an approval as pre-existing. Returns True if newly added."""
        rowcount, _ = self._execute(
            f"INSERT IGNORE INTO `{self.baseline_table}` "
            f"(approval_id, task_id, baselined_at) VALUES (%s, %s, %s)",
            (approval_id, task_id, self._db.now()),
        )
        return rowcount == 1

    def remove_baseline(self, approval_id: str) -> int:
        """Un-suppress one approval so it can notify again."""
        rowcount, _ = self._execute(
            f"DELETE FROM `{self.baseline_table}` WHERE approval_id = %s",
            (approval_id,),
        )
        return rowcount

    def record_notification(
        self,
        approval_id: str,
        task_id: str,
        notified_approver_id: str,
        notified_approver_name: str,
        non_working_approver_ids: Iterable[str],
        comment_id: str,
        recipient_role: str = "approver",
    ) -> None:
        """Upsert the notification record after a comment has been posted.

        The covered-approver set accumulates, so a later run can tell the
        difference between "already covered" and "a new non-worker joined".
        ``first_notified_at`` is written once and never moved.

        The clock comes from the connection, not from ``datetime.now()`` here.
        These columns used to be written in UTC while the session's own
        CURRENT_TIMESTAMP defaults were in the server's zone, which put two
        different meanings in one table and left the dashboard reading times
        two hours behind the run that produced them.
        """
        now = self._db.now()

        # INSERT ... ON DUPLICATE KEY UPDATE, so two overlapping runs cannot
        # both decide the record is new. Touching last_notified_at makes the
        # update non-empty, which is what guarantees lastrowid comes back.
        _, record_id = self._execute(
            f"INSERT INTO `{self.notifications_table}` "
            f"(approval_id, notified_approver_id, task_id, notified_approver_name, "
            f"recipient_role, first_notified_at, last_notified_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) "
            f"ON DUPLICATE KEY UPDATE "
            f"id = LAST_INSERT_ID(id), "
            f"task_id = VALUES(task_id), "
            f"notified_approver_name = VALUES(notified_approver_name), "
            f"recipient_role = VALUES(recipient_role), "
            f"last_notified_at = VALUES(last_notified_at)",
            (
                approval_id,
                notified_approver_id,
                task_id,
                notified_approver_name,
                recipient_role,
                now,
                now,
            ),
        )

        for approver_id in sorted(set(non_working_approver_ids)):
            # INSERT IGNORE: the unique key makes re-covering the same approver
            # a no-op, which is the set semantics the planning logic assumes.
            self._execute(
                f"INSERT IGNORE INTO `{self.non_working_link_table}` "
                f"(notification_id, approver_id, added_at) VALUES (%s, %s, %s)",
                (record_id, approver_id, now),
            )

        if comment_id:
            self._execute(
                f"INSERT INTO `{self.comments_table}` "
                f"(notification_id, comment_id, posted_at) VALUES (%s, %s, %s)",
                (record_id, comment_id, now),
            )

    def delete_notifications(
        self,
        approval_ids: Sequence[str] = (),
        task_ids: Sequence[str] = (),
        all_records: bool = False,
    ) -> int:
        """Delete records by approval or by task. Child rows cascade.

        Wiping the whole table needs ``all_records=True`` said out loud. It is a
        real requirement (``testkit reset --all --yes``), but making "no filters"
        mean "everything" would turn a caller that computed an empty id list
        into a silent history wipe -- and an erased history means every approver
        gets re-notified on the next run.
        """
        if approval_ids:
            unique = list(set(approval_ids))
            sql = (
                f"DELETE FROM `{self.notifications_table}` "
                f"WHERE approval_id IN ({self._placeholders(unique)})"
            )
            params: Sequence[Any] = unique
        elif task_ids:
            unique = list(set(task_ids))
            sql = (
                f"DELETE FROM `{self.notifications_table}` "
                f"WHERE task_id IN ({self._placeholders(unique)})"
            )
            params = unique
        elif all_records:
            sql = f"DELETE FROM `{self.notifications_table}`"
            params = ()
        else:
            raise ValueError(
                "delete_notifications needs approval_ids, task_ids, or an "
                "explicit all_records=True"
            )

        rowcount, _ = self._execute(sql, params)
        return rowcount

    def seed_non_working(
        self,
        user_id: str,
        user_name: str,
        date_iso: str,
        work_schedule_title: str,
        work_schedule_id: str,
        marker: str,
    ) -> None:
        """Mark a user non-working for a date, tagged as seeded by a test tool.

        ``seeded_by`` is set only on insert. Belt and braces with the caller's
        own check: the marker can only ever land on a row this command created,
        so ``unseed`` can never delete real upstream data.
        """
        self._execute(
            f"INSERT INTO `{self.non_working_table}` "
            f"(user_id, user_name, work_schedule_title, work_schedule_id, `date`, seeded_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s) "
            f"ON DUPLICATE KEY UPDATE "
            f"user_name = VALUES(user_name), "
            f"work_schedule_title = VALUES(work_schedule_title), "
            f"work_schedule_id = VALUES(work_schedule_id)",
            (
                user_id,
                user_name,
                work_schedule_title,
                work_schedule_id,
                date_iso,
                marker,
            ),
        )

    def delete_seeded_non_working(
        self, marker: str, user_id: str = "", date_iso: str = ""
    ) -> int:
        """Delete rows carrying ``marker``, optionally narrowed by user or date.

        The marker filter is never optional: an unseed must not be able to
        remove a row the upstream job wrote.
        """
        sql = f"DELETE FROM `{self.non_working_table}` WHERE seeded_by = %s"
        params: List[Any] = [marker]
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        if date_iso:
            sql += " AND `date` = %s"
            params.append(date_iso)
        rowcount, _ = self._execute(sql, params)
        return rowcount
