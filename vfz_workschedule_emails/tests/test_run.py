"""End-to-end test of notifier.run() against fake Wrike and store doubles.

FakeStore stands in for the MySQL-backed Store. It implements the same methods
and returns records in the same shape -- camelCase keys, the covered-approver
set and the comment-id list assembled -- so the planning logic under test is
exactly what ships.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

from config import Config  # noqa: E402
from notifier import run  # noqa: E402

SPACE = "MQAAAAEEuHGf"

CONFIG = Config(
    wrike_token="t",
    space_id=SPACE,
    # No log traffic leaves the test process.
    log_mode="do_nothing",
)

CONTACTS = {
    "KUA": "Alice Adams",
    "KUB": "Bob Brown",
    "KUW": "Wendy White",
    "KUX": "Xavier Xu",
    "KUL": "Laura Lee",
}

LEAD_FIELD = "IEAGPROJLEAD"


class FakeWrike:
    def __init__(self, approvals, tasks):
        self._approvals = approvals
        self._tasks = {t["id"]: t for t in tasks}
        self.posted = []

    def iter_pending_approvals(self, approver_ids):
        wanted = set(approver_ids)
        for approval in self._approvals:
            ids = {d["approverId"] for d in approval.get("decisions", [])}
            if ids & wanted:
                yield approval

    def get_tasks(self, task_ids):
        return [self._tasks[tid] for tid in set(task_ids) if tid in self._tasks]

    def get_contact_names(self, contact_ids):
        return {cid: CONTACTS.get(cid, cid) for cid in contact_ids}

    def create_comment(self, task_id, text):
        self.posted.append((task_id, text))
        return f"CMT{len(self.posted)}"


class FakeStore:
    def __init__(self, non_working, notifications=None, baselined=None):
        self._non_working = non_working
        self.notifications = dict(notifications or {})
        self._baselined = set(baselined or ())

    def baselined_ids(self, approval_ids):
        return self._baselined & set(approval_ids)

    def non_working_users_for(self, date_iso):
        return dict(self._non_working)

    def notifications_for_approvals(self, approval_ids):
        wanted = set(approval_ids)
        return {k: v for k, v in self.notifications.items() if k[0] in wanted}

    def record_notification(
        self, approval_id, task_id, notified_approver_id, notified_approver_name,
        non_working_approver_ids, comment_id, recipient_role="approver",
    ):
        key = (approval_id, notified_approver_id)
        doc = self.notifications.setdefault(
            key,
            {
                "approvalId": approval_id,
                "notifiedApproverId": notified_approver_id,
                "nonWorkingApproverIds": [],
                "commentIds": [],
            },
        )
        doc["taskId"] = task_id
        doc["notifiedApproverName"] = notified_approver_name
        doc["recipientRole"] = recipient_role
        doc["nonWorkingApproverIds"] = sorted(
            set(doc["nonWorkingApproverIds"]) | set(non_working_approver_ids)
        )
        doc["commentIds"].append(comment_id)


def build(decisions, tasks=None):
    task = {
        "id": "IEATASK",
        "status": "Active",
        "parentIds": ["IEAFOLDER"],
        "superParentIds": [SPACE],
        "customFields": [],
    }
    approval = {
        "id": "IEAAPPROVAL",
        "taskId": "IEATASK",
        "status": "Pending",
        "decisions": [{"approverId": a, "status": "Pending"} for a in decisions],
    }
    return FakeWrike([approval], tasks or [task])


class EndToEnd(unittest.TestCase):
    def test_first_run_posts_one_comment_and_records_it(self):
        client = build(["KUA", "KUB", "KUW"])
        store = FakeStore({"KUA": "Alice Adams", "KUB": "Bob Brown"})

        summary = run(CONFIG, client, store, force_window=True)

        self.assertEqual(summary.comments_posted, 1)
        task_id, text = client.posted[0]
        self.assertEqual(task_id, "IEATASK")
        self.assertIn('rel="KUW"', text)
        self.assertIn("not working today: Alice Adams, Bob Brown", text)
        self.assertEqual(
            store.notifications[("IEAAPPROVAL", "KUW")]["nonWorkingApproverIds"],
            ["KUA", "KUB"],
        )

    def test_second_run_is_silent(self):
        client = build(["KUA", "KUB", "KUW"])
        store = FakeStore({"KUA": "Alice Adams", "KUB": "Bob Brown"})
        run(CONFIG, client, store, force_window=True)

        client.posted.clear()
        summary = run(CONFIG, client, store, force_window=True)

        self.assertEqual(summary.comments_posted, 0)
        self.assertEqual(client.posted, [])

    def test_approver_added_after_first_run_is_notified_alone(self):
        client = build(["KUA", "KUW"])
        store = FakeStore({"KUA": "Alice Adams"})
        run(CONFIG, client, store, force_window=True)
        client.posted.clear()

        # Xavier joins the approval an hour later.
        client._approvals[0]["decisions"].append(
            {"approverId": "KUX", "status": "Pending"}
        )
        run(CONFIG, client, store, force_window=True)

        self.assertEqual(len(client.posted), 1)
        _, text = client.posted[0]
        self.assertIn('rel="KUX"', text)
        self.assertNotIn('rel="KUW"', text)

    def test_new_non_worker_triggers_one_followup_then_stops(self):
        client = build(["KUA", "KUB", "KUW"])
        store = FakeStore({"KUA": "Alice Adams"})
        run(CONFIG, client, store, force_window=True)
        client.posted.clear()

        # Bob is now also off today.
        store._non_working["KUB"] = "Bob Brown"
        run(CONFIG, client, store, force_window=True)

        self.assertEqual(len(client.posted), 1)
        _, text = client.posted[0]
        self.assertIn("not working today: Bob Brown", text)
        self.assertNotIn("Alice Adams", text)

        client.posted.clear()
        run(CONFIG, client, store, force_window=True)
        self.assertEqual(client.posted, [])

    def test_deleted_task_is_skipped(self):
        deleted = {
            "id": "IEATASK",
            "status": "Active",
            "parentIds": [CONFIG.recycle_bin_id],
            "superParentIds": [SPACE],
            "customFields": [],
        }
        client = build(["KUA", "KUW"], tasks=[deleted])
        store = FakeStore({"KUA": "Alice Adams"})

        summary = run(CONFIG, client, store, force_window=True)

        self.assertEqual(summary.eligible_approvals, 0)
        self.assertEqual(client.posted, [])

    def test_baselined_approval_never_notifies(self):
        client = build(["KUA", "KUW"])
        store = FakeStore({"KUA": "Alice Adams"}, baselined={"IEAAPPROVAL"})

        summary = run(CONFIG, client, store, force_window=True)

        self.assertEqual(summary.baselined, 1)
        self.assertEqual(summary.comments_posted, 0)
        self.assertEqual(client.posted, [])

    def test_unbaselined_approval_still_notifies(self):
        client = build(["KUA", "KUW"])
        store = FakeStore({"KUA": "Alice Adams"}, baselined={"IEAOTHER"})

        summary = run(CONFIG, client, store, force_window=True)

        self.assertEqual(summary.baselined, 0)
        self.assertEqual(summary.comments_posted, 1)

    def test_dry_run_posts_nothing_and_records_nothing(self):
        config = Config(**{**CONFIG.__dict__, "dry_run": True})
        client = build(["KUA", "KUW"])
        store = FakeStore({"KUA": "Alice Adams"})

        summary = run(config, client, store, force_window=True)

        self.assertEqual(summary.comments_planned, 1)
        self.assertEqual(summary.comments_posted, 0)
        self.assertEqual(client.posted, [])
        self.assertEqual(store.notifications, {})

    def test_window_skip_touches_nothing(self):
        config = Config(
            **{**CONFIG.__dict__, "notify_start_hour": 0, "notify_end_hour": 0}
        )
        client = build(["KUA", "KUW"])
        store = FakeStore({"KUA": "Alice Adams"})

        summary = run(config, client, store, force_window=False)

        self.assertIsNotNone(summary.skipped_reason)
        self.assertEqual(client.posted, [])

    def test_the_fallback_role_reaches_the_notification_record(self):
        client = build(["KUA"])
        store = FakeStore({"KUA": "Alice Adams"})

        run(CONFIG, client, store, force_window=True)

        record = store.notifications[("IEAAPPROVAL", CONFIG.fallback_contact_id)]
        self.assertEqual(record["recipientRole"], "fallback")

    def test_the_lead_role_reaches_the_notification_record(self):
        config = Config(**{**CONFIG.__dict__, "project_lead_field_id": LEAD_FIELD})
        client = build(
            ["KUA"],
            tasks=[
                {
                    "id": "IEATASK",
                    "status": "Active",
                    "parentIds": ["IEAFOLDER"],
                    "superParentIds": [SPACE],
                    "customFields": [{"id": LEAD_FIELD, "value": '["KUL"]'}],
                }
            ],
        )
        store = FakeStore({"KUA": "Alice Adams"})

        run(config, client, store, force_window=True)

        record = store.notifications[("IEAAPPROVAL", "KUL")]
        self.assertEqual(record["recipientRole"], "lead")
        self.assertNotIn(("IEAAPPROVAL", config.fallback_contact_id), store.notifications)

    def test_the_lead_mention_resolves_to_a_name(self):
        # The lead is not an approver, so their id only reaches contact
        # resolution if run() asks for it explicitly. Without that the comment
        # renders "@KUL" instead of "@Laura Lee".
        config = Config(**{**CONFIG.__dict__, "project_lead_field_id": LEAD_FIELD})
        client = build(
            ["KUA"],
            tasks=[
                {
                    "id": "IEATASK",
                    "status": "Active",
                    "parentIds": ["IEAFOLDER"],
                    "superParentIds": [SPACE],
                    "customFields": [{"id": LEAD_FIELD, "value": '["KUL"]'}],
                }
            ],
        )
        store = FakeStore({"KUA": "Alice Adams"})

        run(config, client, store, force_window=True)

        self.assertEqual(len(client.posted), 1)
        _, text = client.posted[0]
        self.assertIn("@Laura Lee", text)
        self.assertNotIn("@KUL", text)

    def test_an_unnotifiable_approval_keeps_its_reason_in_the_summary(self):
        config = Config(**{**CONFIG.__dict__, "fallback_contact_id": ""})
        client = build(["KUA"])
        store = FakeStore({"KUA": "Alice Adams"})

        summary = run(config, client, store, force_window=True)

        self.assertEqual(summary.unnotifiable, 1)
        self.assertEqual(client.posted, [])
        (detail,) = summary.details
        # The label used to overwrite the reason, losing the only record of why
        # nobody could be told.
        self.assertTrue(detail["reason"].startswith("unnotifiable: "))
        self.assertIn("no fallback contact is set", detail["reason"])

    def test_empty_non_working_collection_posts_nothing(self):
        client = build(["KUA", "KUW"])
        store = FakeStore({})

        summary = run(CONFIG, client, store, force_window=True)

        self.assertIn("upstream job", summary.skipped_reason)
        self.assertEqual(client.posted, [])


if __name__ == "__main__":
    unittest.main()
