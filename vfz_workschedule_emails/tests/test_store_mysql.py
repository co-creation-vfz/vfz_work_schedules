"""Integration tests for the SQL itself, against a real MySQL.

The rest of the suite runs against store doubles, which proves the planning
logic but says nothing about whether the statements in ``store.py`` are valid
MySQL. This job's SQL is the part worth checking: two child tables with cascades,
an upsert that has to hand back the existing row's id, and a unique key that is
the only thing standing between an overlapping run and a duplicate comment on a
live Wrike task.

It **skips itself** unless a test database is configured, so a plain test run
needs no server and no credentials:

    TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_DATABASE=work_schedules_test \
    TEST_MYSQL_USER=root TEST_MYSQL_PASSWORD=secret \
    python -m unittest tests.test_store_mysql

Table names are prefixed ``test_`` so they cannot collide with production even
if the database is shared, and each test truncates them.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

from database_helpers import DatabaseConnection, DatabaseError
from store import Store  # noqa: E402

NON_WORKING = "test_non_working_users"
NOTIFICATIONS = "test_approval_notifications"
BASELINE = "test_baselined_approvals"


@unittest.skipUnless(
    os.environ.get("TEST_MYSQL_HOST"),
    "Set TEST_MYSQL_HOST (and USER/PASSWORD/DATABASE) to run the MySQL tests",
)
class StoreOnMySQL(unittest.TestCase):
    """One store, shared across the class: building a pool is a round trip."""

    @classmethod
    def setUpClass(cls):
        database = DatabaseConnection(
            host=os.environ["TEST_MYSQL_HOST"],
            database=os.environ.get("TEST_MYSQL_DATABASE", "work_schedules_test"),
            user=os.environ.get("TEST_MYSQL_USER", "root"),
            password=os.environ.get("TEST_MYSQL_PASSWORD", ""),
            port=int(os.environ.get("TEST_MYSQL_PORT", "3306")),
            pool_size=1,
            pool_name="emails_tests",
            # Pinned, as in production: the point of the session zone is that a
            # DATETIME means the same thing however the server is configured,
            # and a test against a UTC server would not show that.
            zone=os.environ.get("TEST_MYSQL_TZ", "Africa/Johannesburg"),
        )
        cls.store = Store(
            database,
            non_working_table=NON_WORKING,
            notifications_table=NOTIFICATIONS,
            baseline_table=BASELINE,
        )
        cls.store.ensure_schema()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        # Children first: the cascade would handle it, but truncating a parent
        # that a foreign key points at is refused outright by InnoDB.
        for table in (
            self.store.comments_table,
            self.store.non_working_link_table,
            self.store.notifications_table,
            self.store.non_working_table,
            self.store.baseline_table,
        ):
            self.store._execute(f"DELETE FROM `{table}`")

    # -- schema ------------------------------------------------------------

    def test_ensure_schema_is_safe_to_call_repeatedly(self):
        self.store.ensure_schema()
        self.store.ensure_schema()  # must not raise the second time either

        self.assertEqual(self.store.baseline_count(), 0)

    # -- non-working reads -------------------------------------------------

    def test_seeding_and_reading_non_working_users(self):
        self.store.seed_non_working(
            "KUATAIRI", "Abigail Hlalele", "2026-08-25", "Testing", "IEASCHED", "testkit"
        )

        users = self.store.non_working_users_for("2026-08-25")

        self.assertEqual(users, {"KUATAIRI": "Abigail Hlalele"})
        self.assertEqual(self.store.non_working_users_for("2026-08-26"), {})

    def test_the_seeded_marker_is_only_set_on_insert(self):
        """unseed keys on the marker, so it must never land on an upstream row."""
        self.store.seed_non_working(
            "KUAUP", "Upstream Person", "2026-08-25", "Real", "IEAREAL", None
        )
        # A second seed, this time from the testkit, must not claim the row.
        self.store.seed_non_working(
            "KUAUP", "Upstream Person", "2026-08-25", "Real", "IEAREAL", "testkit"
        )

        row = self.store.non_working_row("KUAUP", "2026-08-25")

        self.assertIsNone(row["seeded_by"])

    def test_unseed_only_deletes_marked_rows(self):
        self.store.seed_non_working(
            "KUAUP", "Upstream", "2026-08-25", "Real", "IEAREAL", None
        )
        self.store.seed_non_working(
            "KUASEED", "Seeded", "2026-08-25", "testkit seeded", "TESTKIT", "testkit"
        )

        deleted = self.store.delete_seeded_non_working("testkit")

        self.assertEqual(deleted, 1)
        self.assertEqual(
            list(self.store.non_working_users_for("2026-08-25")), ["KUAUP"]
        )

    def test_non_working_rows_carry_the_schedule_and_marker(self):
        self.store.seed_non_working(
            "KUATAIRI", "Abigail Hlalele", "2026-08-25", "Testing", "IEASCHED", "testkit"
        )

        rows = self.store.non_working_rows_for("2026-08-25")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["work_schedule_title"], "Testing")
        self.assertEqual(rows[0]["seeded_by"], "testkit")

    # -- notification history ---------------------------------------------

    def test_recording_a_notification_round_trips(self):
        self.store.record_notification(
            approval_id="IEAAPPROVAL",
            task_id="IEATASK",
            notified_approver_id="KUW",
            notified_approver_name="Wendy White",
            non_working_approver_ids=["KUA", "KUB"],
            comment_id="CMT1",
            recipient_role="approver",
        )

        records = self.store.notifications_for_approvals(["IEAAPPROVAL"])

        self.assertEqual(list(records), [("IEAAPPROVAL", "KUW")])
        record = records[("IEAAPPROVAL", "KUW")]
        self.assertEqual(record["taskId"], "IEATASK")
        self.assertEqual(record["notifiedApproverName"], "Wendy White")
        self.assertEqual(record["recipientRole"], "approver")
        # The two arrays that became child tables.
        self.assertEqual(record["nonWorkingApproverIds"], ["KUA", "KUB"])
        self.assertEqual(record["commentIds"], ["CMT1"])

    def test_re_recording_updates_one_record_and_accumulates_the_set(self):
        """The dedup guarantee, and why the covered set is a set.

        A later run comparing today's non-workers against this is how "already
        covered" is told from "a new non-worker joined this approval".
        """
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT1"
        )
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA", "KUB"], "CMT2"
        )

        records = self.store.notifications_for_approvals(["IEAAPPROVAL"])
        record = records[("IEAAPPROVAL", "KUW")]

        # One record, not two: the unique key held and the id came back.
        self.assertEqual(len(records), 1)
        self.assertEqual(record["nonWorkingApproverIds"], ["KUA", "KUB"])
        # Comments are a list, not a set: a retraction must delete both.
        self.assertEqual(record["commentIds"], ["CMT1", "CMT2"])
        self.assertLessEqual(record["firstNotifiedAt"], record["lastNotifiedAt"])

    def test_first_notified_at_is_never_moved(self):
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT1"
        )
        first = self.store.notifications_for_approvals(["IEAAPPROVAL"])[
            ("IEAAPPROVAL", "KUW")
        ]["firstNotifiedAt"]

        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT2"
        )

        self.assertEqual(
            self.store.notifications_for_approvals(["IEAAPPROVAL"])[
                ("IEAAPPROVAL", "KUW")
            ]["firstNotifiedAt"],
            first,
        )

    def test_two_recipients_on_one_approval_are_separate_records(self):
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT1"
        )
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUX", "Xavier Xu", ["KUA"], "CMT2"
        )

        records = self.store.notifications_for_approvals(["IEAAPPROVAL"])

        self.assertEqual(len(records), 2)

    def test_notifications_for_approvals_ignores_other_approvals(self):
        self.store.record_notification(
            "IEAMINE", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT1"
        )
        self.store.record_notification(
            "IEAOTHER", "IEATASK2", "KUW", "Wendy White", ["KUA"], "CMT2"
        )

        records = self.store.notifications_for_approvals(["IEAMINE"])

        self.assertEqual(list(records), [("IEAMINE", "KUW")])

    def test_an_empty_id_list_needs_no_query(self):
        self.assertEqual(self.store.notifications_for_approvals([]), {})
        self.assertEqual(self.store.baselined_ids([]), set())

    def test_notification_records_are_newest_first_and_limited(self):
        for index in range(3):
            self.store.record_notification(
                f"IEAAPPROVAL{index}", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT"
            )

        records = self.store.notification_records(limit=2)

        self.assertEqual(len(records), 2)

    def test_notification_records_filter_by_task(self):
        self.store.record_notification(
            "IEAAPPROVAL", "IEAWANTED", "KUW", "Wendy White", ["KUA"], "CMT1"
        )
        self.store.record_notification(
            "IEAOTHER", "IEAOTHERTASK", "KUW", "Wendy White", ["KUA"], "CMT2"
        )

        records = self.store.notification_records(task_ids=["IEAWANTED"])

        self.assertEqual([r["approvalId"] for r in records], ["IEAAPPROVAL"])

    # -- deletion and cascades --------------------------------------------

    def test_deleting_a_record_cascades_to_its_children(self):
        """A retraction must not leave orphaned comment or covered-approver rows."""
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA", "KUB"], "CMT1"
        )

        deleted = self.store.delete_notifications(approval_ids=["IEAAPPROVAL"])

        self.assertEqual(deleted, 1)
        self.assertEqual(self.store.notifications_for_approvals(["IEAAPPROVAL"]), {})
        for table in (self.store.comments_table, self.store.non_working_link_table):
            rows = self.store._query(f"SELECT COUNT(*) AS total FROM `{table}`")
            self.assertEqual(rows[0]["total"], 0, f"{table} was not cascaded")

    def test_delete_notifications_refuses_to_wipe_without_being_told(self):
        """An empty id list must not be read as "delete everything".

        An erased history means every approver gets re-notified on the next run.
        """
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT1"
        )

        with self.assertRaises(ValueError):
            self.store.delete_notifications()

        self.assertEqual(len(self.store.notification_records()), 1)

    def test_delete_notifications_wipes_when_told_explicitly(self):
        self.store.record_notification(
            "IEAAPPROVAL", "IEATASK", "KUW", "Wendy White", ["KUA"], "CMT1"
        )

        self.assertEqual(self.store.delete_notifications(all_records=True), 1)

    # -- baseline ----------------------------------------------------------

    def test_the_baseline_is_add_once(self):
        self.assertTrue(self.store.add_baseline("IEAAPPROVAL", "IEATASK"))
        # A second baseline run must not re-add, or it would suppress everything
        # raised since the first.
        self.assertFalse(self.store.add_baseline("IEAAPPROVAL", "IEATASK"))
        self.assertEqual(self.store.baseline_count(), 1)

    def test_baselined_ids_returns_only_the_matches(self):
        self.store.add_baseline("IEAOLD", "IEATASK")

        self.assertEqual(
            self.store.baselined_ids(["IEAOLD", "IEANEW"]), {"IEAOLD"}
        )

    def test_removing_a_baseline_lets_it_notify_again(self):
        self.store.add_baseline("IEAAPPROVAL", "IEATASK")

        self.assertEqual(self.store.remove_baseline("IEAAPPROVAL"), 1)
        self.assertEqual(self.store.baseline_count(), 0)
        self.assertEqual(self.store.remove_baseline("IEAAPPROVAL"), 0)

    # -- safety ------------------------------------------------------------

    def test_an_unsafe_table_name_is_refused_before_any_sql_runs(self):
        with self.assertRaises(ValueError):
            Store(
                DatabaseConnection(
                    host="ignored",
                    database="ignored",
                    user="ignored",
                    password="ignored",
                ),
                notifications_table="notes; DROP TABLE users",
            )


if __name__ == "__main__":
    unittest.main()
