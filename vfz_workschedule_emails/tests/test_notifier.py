"""Unit tests for the planning logic - no Wrike or database access required."""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

import comments  # noqa: E402
from config import Config  # noqa: E402
from notifier import (  # noqa: E402
    approver_ids,
    plan_for_approval,
    project_lead_ids,
    task_is_eligible,
)

SPACE = "MQAAAAEEuHGf"
BIN = "IEAFXAOHI7777776"

CONFIG = Config(
    wrike_token="t",
    log_mode="do_nothing",
    space_id=SPACE,
    recycle_bin_id=BIN,
    fallback_contact_id="KUSUP",
)

NAMES = {
    "KUSUP": "Co-Creation Support",
    "KUA": "Alice Adams",
    "KUB": "Bob Brown",
    "KUW": "Wendy White",
    "KUX": "Xavier Xu",
    "KUL": "Laura Lee",
}


def task(**overrides):
    base = {
        "id": "IEATASK",
        "status": "Active",
        "parentIds": ["IEAFOLDER"],
        "superParentIds": [SPACE],
        "customFields": [],
    }
    base.update(overrides)
    return base


def approval(decisions, approval_id="IEAAPPROVAL"):
    return {
        "id": approval_id,
        "taskId": "IEATASK",
        "status": "Pending",
        "decisions": [
            {"approverId": aid, "status": status} for aid, status in decisions
        ],
    }


class TaskEligibility(unittest.TestCase):
    def test_active_task_in_space_is_eligible(self):
        self.assertTrue(task_is_eligible(task(), CONFIG))

    def test_space_may_appear_in_parent_ids(self):
        self.assertTrue(
            task_is_eligible(task(parentIds=[SPACE], superParentIds=[]), CONFIG)
        )

    def test_completed_task_is_skipped(self):
        self.assertFalse(task_is_eligible(task(status="Completed"), CONFIG))

    def test_cancelled_task_is_skipped(self):
        self.assertFalse(task_is_eligible(task(status="Cancelled"), CONFIG))

    def test_task_in_recycle_bin_is_skipped(self):
        self.assertFalse(
            task_is_eligible(task(parentIds=[BIN], superParentIds=[SPACE]), CONFIG)
        )

    def test_task_outside_space_is_skipped(self):
        self.assertFalse(task_is_eligible(task(superParentIds=["MQOTHER"]), CONFIG))


class ApproverExtraction(unittest.TestCase):
    def test_not_required_decisions_are_excluded(self):
        a = approval([("KUA", "Pending"), ("KUW", "NotRequired")])
        self.assertEqual(approver_ids(a), ["KUA"])

    def test_already_decided_approvers_are_excluded(self):
        # Someone who has approved has done their part; their absence blocks
        # nobody and they need telling about nobody.
        a = approval([("KUA", "Approved"), ("KUB", "Rejected"), ("KUW", "Pending")])
        self.assertEqual(approver_ids(a), ["KUW"])


class Planning(unittest.TestCase):
    def plan(self, decisions, non_working, existing, t=None):
        return plan_for_approval(
            approval(decisions),
            t or task(),
            non_working,
            existing,
            NAMES,
            CONFIG,
        )

    def test_initial_comment_tags_all_working_approvers_once(self):
        plans = self.plan(
            [("KUA", "Pending"), ("KUW", "Pending"), ("KUX", "Pending")],
            {"KUA": "Alice Adams"},
            {},
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].reason, "initial")
        self.assertEqual(plans[0].recipient_ids, ["KUW", "KUX"])
        self.assertIn('rel="KUW"', plans[0].text)
        self.assertIn('rel="KUX"', plans[0].text)
        # Non-workers are named but never mentioned.
        self.assertNotIn('rel="KUA"', plans[0].text)
        self.assertIn("not working today: Alice Adams", plans[0].text)

    def test_two_non_workers_are_listed_together(self):
        plans = self.plan(
            [("KUA", "Pending"), ("KUB", "Pending"), ("KUW", "Pending")],
            {"KUA": "Alice Adams", "KUB": "Bob Brown"},
            {},
        )
        self.assertEqual(len(plans), 1)
        self.assertIn("not working today: Alice Adams, Bob Brown", plans[0].text)

    def test_nothing_resent_when_already_notified(self):
        existing = {
            ("IEAAPPROVAL", "KUW"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUW",
                "nonWorkingApproverIds": ["KUA"],
            }
        }
        plans = self.plan(
            [("KUA", "Pending"), ("KUW", "Pending")], {"KUA": "Alice Adams"}, existing
        )
        self.assertEqual(plans, [])

    def test_working_approver_added_later_is_tagged_alone(self):
        existing = {
            ("IEAAPPROVAL", "KUW"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUW",
                "nonWorkingApproverIds": ["KUA"],
            }
        }
        plans = self.plan(
            [("KUA", "Pending"), ("KUW", "Pending"), ("KUX", "Pending")],
            {"KUA": "Alice Adams"},
            existing,
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].reason, "new_recipient")
        self.assertEqual(plans[0].recipient_ids, ["KUX"])
        self.assertIn('rel="KUX"', plans[0].text)
        self.assertNotIn('rel="KUW"', plans[0].text)

    def test_new_non_working_approver_triggers_followup_naming_only_them(self):
        existing = {
            ("IEAAPPROVAL", "KUW"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUW",
                "nonWorkingApproverIds": ["KUA"],
            }
        }
        plans = self.plan(
            [("KUA", "Pending"), ("KUB", "Pending"), ("KUW", "Pending")],
            {"KUA": "Alice Adams", "KUB": "Bob Brown"},
            existing,
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].reason, "new_non_working_approver")
        self.assertEqual(plans[0].recipient_ids, ["KUW"])
        self.assertIn("not working today: Bob Brown", plans[0].text)
        self.assertNotIn("Alice Adams", plans[0].text)
        # The record must still absorb the full set, so this fires only once.
        self.assertEqual(plans[0].covered_non_working_ids, ["KUA", "KUB"])

    def test_two_working_approvers_added_later_share_one_comment(self):
        """Multiple recipients means one comment, not one comment each."""
        existing = {
            ("IEAAPPROVAL", "KUW"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUW",
                "nonWorkingApproverIds": ["KUA"],
            }
        }
        plans = self.plan(
            [
                ("KUA", "Pending"),
                ("KUW", "Pending"),
                ("KUX", "Pending"),
                ("KUY", "Pending"),
            ],
            {"KUA": "Alice Adams"},
            existing,
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].reason, "new_recipient")
        self.assertEqual(plans[0].recipient_ids, ["KUX", "KUY"])
        self.assertIn('rel="KUX"', plans[0].text)
        self.assertIn('rel="KUY"', plans[0].text)
        # Already notified, and not told twice.
        self.assertNotIn('rel="KUW"', plans[0].text)

    def test_a_new_non_worker_is_one_comment_for_everyone_owed_it(self):
        """Two recipients owed the same news get one comment between them."""
        existing = {
            ("IEAAPPROVAL", who): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": who,
                "nonWorkingApproverIds": ["KUA"],
            }
            for who in ("KUW", "KUX")
        }
        plans = self.plan(
            [
                ("KUA", "Pending"),
                ("KUB", "Pending"),
                ("KUW", "Pending"),
                ("KUX", "Pending"),
            ],
            {"KUA": "Alice Adams", "KUB": "Bob Brown"},
            existing,
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].reason, "new_non_working_approver")
        self.assertEqual(plans[0].recipient_ids, ["KUW", "KUX"])
        self.assertIn('rel="KUW"', plans[0].text)
        self.assertIn('rel="KUX"', plans[0].text)
        self.assertIn("not working today: Bob Brown", plans[0].text)
        self.assertNotIn("Alice Adams", plans[0].text)

    def test_recipients_owed_different_news_are_not_merged(self):
        """The limit of the grouping, and the reason it keys on the news.

        KUW has already been told about Alice; KUX has not. Merging them into
        one comment would tell KUX about somebody they were told about last
        run, so they stay separate.
        """
        existing = {
            ("IEAAPPROVAL", "KUW"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUW",
                "nonWorkingApproverIds": ["KUA"],
            },
            ("IEAAPPROVAL", "KUX"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUX",
                "nonWorkingApproverIds": [],
            },
        }
        plans = self.plan(
            [
                ("KUA", "Pending"),
                ("KUB", "Pending"),
                ("KUW", "Pending"),
                ("KUX", "Pending"),
            ],
            {"KUA": "Alice Adams", "KUB": "Bob Brown"},
            existing,
        )

        self.assertEqual(len(plans), 2)
        d_by_recipient = {tuple(p.recipient_ids): p for p in plans}
        # KUW is owed Bob only; KUX is owed both.
        self.assertIn("not working today: Bob Brown", d_by_recipient[("KUW",)].text)
        self.assertNotIn("Alice Adams", d_by_recipient[("KUW",)].text)
        self.assertIn(
            "not working today: Alice Adams, Bob Brown", d_by_recipient[("KUX",)].text
        )

    def test_no_plans_when_no_approver_is_off(self):
        plans = self.plan(
            [("KUW", "Pending"), ("KUX", "Pending")], {"KUA": "Alice Adams"}, {}
        )
        self.assertEqual(plans, [])

    def test_a_normal_notice_is_recorded_as_going_to_an_approver(self):
        plans = self.plan(
            [("KUA", "Pending"), ("KUW", "Pending")], {"KUA": "Alice Adams"}, {}
        )
        self.assertEqual(plans[0].recipient_role, "approver")

    def test_all_approvers_off_tags_the_fallback_contact(self):
        plans = self.plan(
            [("KUA", "Pending"), ("KUB", "Pending")],
            {"KUA": "Alice Adams", "KUB": "Bob Brown"},
            {},
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].recipient_ids, ["KUSUP"])
        self.assertIn('rel="KUSUP"', plans[0].text)
        self.assertIn("no working approvers", plans[0].text)
        self.assertIn("Alice Adams, Bob Brown", plans[0].text)

    def test_sole_approver_off_tags_the_fallback_contact(self):
        plans = self.plan([("KUA", "Pending")], {"KUA": "Alice Adams"}, {})
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].recipient_ids, ["KUSUP"])

    def test_the_fallback_contact_is_not_recorded_as_an_approver(self):
        # The support contact is not on the approval, and the history should
        # not claim otherwise.
        plans = self.plan([("KUA", "Pending")], {"KUA": "Alice Adams"}, {})
        self.assertEqual(plans[0].recipient_role, "fallback")

    def test_fallback_not_used_twice_for_the_same_approval(self):
        existing = {
            ("IEAAPPROVAL", "KUSUP"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUSUP",
                "nonWorkingApproverIds": ["KUA"],
            }
        }
        plans = self.plan([("KUA", "Pending")], {"KUA": "Alice Adams"}, existing)
        self.assertEqual(plans, [])

    def test_no_fallback_configured_notifies_nobody(self):
        from dataclasses import replace
        recorded = []
        plans = plan_for_approval(
            approval([("KUA", "Pending")]),
            task(),
            {"KUA": "Alice Adams"},
            {},
            NAMES,
            replace(CONFIG, fallback_contact_id=""),
            unnotifiable=recorded,
        )
        self.assertEqual(plans, [])
        self.assertEqual(len(recorded), 1)


class CommentMarkup(unittest.TestCase):
    def test_mention_uses_stream_user_id_anchor(self):
        self.assertEqual(
            comments.mention("KUW", "Wendy White"),
            '<a class="stream-user-id avatar" rel="KUW">@Wendy White</a>',
        )

    def test_plain_text_rendering_keeps_the_at_names(self):
        # Asserts the rendering contract, not the copy: mentions survive as
        # "@Name" and every tag is stripped. Pinning the full sentence here
        # made this test fail on ordinary copy edits, which is not what it is
        # for — wording is covered by the per-notice tests.
        plain = comments.to_plain_text(comments.approver_notice(["KUW"], ["KUA"], NAMES))
        self.assertTrue(plain.startswith("Hi @Wendy White,"))
        self.assertIn("Alice Adams", plain)
        self.assertNotIn("<", plain)
        self.assertNotIn("rel=", plain)

    def test_plain_text_unescapes_entities(self):
        markup = comments.approver_notice(["KUW"], ["KUA"], {"KUW": "W & Co", "KUA": "A & B"})
        self.assertIn("W & Co", comments.to_plain_text(markup))
        self.assertNotIn("&amp;", comments.to_plain_text(markup))

    def test_names_are_html_escaped(self):
        text = comments.approver_notice(["KUW"], ["KUA"], {"KUW": "W & Co", "KUA": "A <b>"})
        self.assertIn("W &amp; Co", text)
        self.assertIn("A &lt;b&gt;", text)


LEAD_FIELD = "IEAGPROJLEAD"

LEAD_CONFIG = replace(CONFIG, project_lead_field_id=LEAD_FIELD)


def task_with_lead(value, field_id=LEAD_FIELD, **overrides):
    return task(customFields=[{"id": field_id, "value": value}], **overrides)


class ProjectLeadField(unittest.TestCase):
    """Reading the lead out of the task's custom fields."""

    def lead(self, value, field_id=LEAD_FIELD, config=LEAD_CONFIG):
        return project_lead_ids(task_with_lead(value, field_id), config)

    def test_json_array_value(self):
        self.assertEqual(self.lead('["KUL"]'), ["KUL"])

    def test_json_array_with_several_leads(self):
        self.assertEqual(self.lead('["KUL","KUX"]'), ["KUL", "KUX"])

    def test_bare_id_value(self):
        self.assertEqual(self.lead("KUL"), ["KUL"])

    def test_comma_separated_value(self):
        self.assertEqual(self.lead("KUL, KUX"), ["KUL", "KUX"])

    def test_already_parsed_list(self):
        self.assertEqual(self.lead(["KUL"]), ["KUL"])

    def test_empty_value_is_no_lead(self):
        for value in ("", "  ", "[]", None, []):
            with self.subTest(value=value):
                self.assertEqual(self.lead(value), [])

    def test_unparseable_json_degrades_to_no_lead(self):
        # Rather than raising mid-run: no lead means the fallback contact.
        self.assertEqual(self.lead('["KUL"'), [])

    def test_unexpected_type_degrades_to_no_lead(self):
        self.assertEqual(self.lead(42), [])

    def test_a_different_field_is_ignored(self):
        self.assertEqual(self.lead("KUL", field_id="IEAGSOMETHINGELSE"), [])

    def test_no_field_configured_is_no_lead(self):
        self.assertEqual(self.lead('["KUL"]', config=CONFIG), [])

    def test_task_without_custom_fields(self):
        self.assertEqual(project_lead_ids(task(), LEAD_CONFIG), [])


class ProjectLeadPlanning(unittest.TestCase):
    """Who gets told when no approver is working."""

    def plan(self, decisions, non_working, existing, t=None, config=LEAD_CONFIG):
        return plan_for_approval(
            approval(decisions),
            t if t is not None else task_with_lead('["KUL"]'),
            non_working,
            existing,
            NAMES,
            config,
        )

    def test_sole_approver_off_tags_the_project_lead(self):
        plans = self.plan([("KUA", "Pending")], {"KUA": "Alice Adams"}, {})
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].recipient_ids, ["KUL"])
        self.assertEqual(plans[0].recipient_role, "lead")
        self.assertIn('rel="KUL"', plans[0].text)
        self.assertIn("project lead", plans[0].text)
        self.assertIn("Alice Adams", plans[0].text)

    def test_all_approvers_off_tags_the_project_lead(self):
        plans = self.plan(
            [("KUA", "Pending"), ("KUB", "Pending")],
            {"KUA": "Alice Adams", "KUB": "Bob Brown"},
            {},
        )
        self.assertEqual(plans[0].recipient_ids, ["KUL"])
        self.assertEqual(plans[0].recipient_role, "lead")

    def test_a_working_approver_still_wins_over_the_lead(self):
        plans = self.plan(
            [("KUA", "Pending"), ("KUW", "Pending")], {"KUA": "Alice Adams"}, {}
        )
        self.assertEqual(plans[0].recipient_ids, ["KUW"])
        self.assertEqual(plans[0].recipient_role, "approver")

    def test_lead_who_is_also_off_falls_through_to_the_fallback(self):
        plans = self.plan(
            [("KUA", "Pending")], {"KUA": "Alice Adams", "KUL": "Laura Lee"}, {}
        )
        self.assertEqual(plans[0].recipient_ids, ["KUSUP"])
        self.assertEqual(plans[0].recipient_role, "fallback")

    def test_empty_lead_field_falls_through_to_the_fallback(self):
        plans = self.plan(
            [("KUA", "Pending")],
            {"KUA": "Alice Adams"},
            {},
            t=task_with_lead(""),
        )
        self.assertEqual(plans[0].recipient_ids, ["KUSUP"])
        self.assertEqual(plans[0].recipient_role, "fallback")

    def test_lead_lookup_disabled_keeps_the_old_behaviour(self):
        plans = self.plan(
            [("KUA", "Pending")], {"KUA": "Alice Adams"}, {}, config=CONFIG
        )
        self.assertEqual(plans[0].recipient_ids, ["KUSUP"])
        self.assertEqual(plans[0].recipient_role, "fallback")

    def test_only_the_working_lead_is_tagged(self):
        plans = self.plan(
            [("KUA", "Pending")],
            {"KUA": "Alice Adams", "KUX": "Xavier Xu"},
            {},
            t=task_with_lead('["KUL","KUX"]'),
        )
        self.assertEqual(plans[0].recipient_ids, ["KUL"])
        self.assertEqual(plans[0].recipient_role, "lead")

    def test_every_lead_off_falls_through_to_the_fallback(self):
        plans = self.plan(
            [("KUA", "Pending")],
            {"KUA": "Alice Adams", "KUL": "Laura Lee", "KUX": "Xavier Xu"},
            {},
            t=task_with_lead('["KUL","KUX"]'),
        )
        self.assertEqual(plans[0].recipient_ids, ["KUSUP"])

    def test_lead_not_tagged_twice_for_the_same_approval(self):
        existing = {
            ("IEAAPPROVAL", "KUL"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUL",
                "nonWorkingApproverIds": ["KUA"],
            }
        }
        plans = self.plan([("KUA", "Pending")], {"KUA": "Alice Adams"}, existing)
        self.assertEqual(plans, [])

    def test_a_newly_off_approver_re_notifies_the_lead(self):
        existing = {
            ("IEAAPPROVAL", "KUL"): {
                "approvalId": "IEAAPPROVAL",
                "notifiedApproverId": "KUL",
                "nonWorkingApproverIds": ["KUA"],
            }
        }
        plans = self.plan(
            [("KUA", "Pending"), ("KUB", "Pending")],
            {"KUA": "Alice Adams", "KUB": "Bob Brown"},
            existing,
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].recipient_ids, ["KUL"])
        self.assertEqual(plans[0].reason, "new_non_working_approver")
        self.assertEqual(plans[0].non_working_ids, ["KUB"])

    def test_no_lead_and_no_fallback_notifies_nobody(self):
        recorded = []
        plans = plan_for_approval(
            approval([("KUA", "Pending")]),
            task_with_lead(""),
            {"KUA": "Alice Adams"},
            {},
            NAMES,
            replace(LEAD_CONFIG, fallback_contact_id=""),
            unnotifiable=recorded,
        )
        self.assertEqual(plans, [])
        self.assertEqual(len(recorded), 1)


if __name__ == "__main__":
    unittest.main()
