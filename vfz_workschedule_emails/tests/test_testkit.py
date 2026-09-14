"""Tests for the seed guard: the testkit must never claim an upstream row."""
import argparse
import io
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

import testkit  # noqa: E402
from config import Config  # noqa: E402

CONFIG = Config(wrike_token="t", log_mode="do_nothing")
DATE = "2026-08-25"


class FakeStore:
    """Stands in for the MySQL Store, holding rows in the shape it returns.

    Rows are snake_case here, matching the columns ``non_working_row`` selects,
    because that is what the guard in cmd_seed inspects. A fake that used the
    old camelCase keys would let the guard pass while never actually reading
    ``seeded_by`` in production.
    """

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.seeded = []

    def non_working_row(self, user_id, date_iso):
        for row in self.rows:
            if row.get("user_id") == user_id and row.get("date") == date_iso:
                return row
        return None

    def seed_non_working(
        self,
        user_id,
        user_name,
        date_iso,
        work_schedule_title,
        work_schedule_id,
        marker,
    ):
        self.seeded.append(
            {
                "user_id": user_id,
                "user_name": user_name,
                "date": date_iso,
                "work_schedule_title": work_schedule_title,
                "work_schedule_id": work_schedule_id,
                "marker": marker,
            }
        )


def seed_args(**overrides):
    args = {
        "user": "KUATAIRI",
        "name": "Given Name",  # supplied so no Wrike lookup is attempted
        "date": DATE,
        "schedule_title": "testkit seeded",
        "schedule_id": "TESTKIT",
    }
    args.update(overrides)
    return argparse.Namespace(**args)


class SeedGuard(unittest.TestCase):
    def setUp(self):
        # cmd_seed narrates to stdout; keep that out of the test report.
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(redirect_stdout(io.StringIO()))

    def test_a_fresh_row_is_seeded_and_marked(self):
        store = FakeStore()

        status = testkit.cmd_seed(CONFIG, store, seed_args())

        self.assertEqual(status, 0)
        self.assertEqual(len(store.seeded), 1)
        # The marker is what unseed keys on, so it has to be passed through.
        self.assertEqual(store.seeded[0]["marker"], testkit.MARKER)
        self.assertEqual(store.seeded[0]["user_id"], "KUATAIRI")
        self.assertEqual(store.seeded[0]["date"], DATE)

    def test_an_upstream_row_is_refused_not_overwritten(self):
        store = FakeStore(
            [{"user_id": "KUATAIRI", "date": DATE, "user_name": "Real Person"}]
        )
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            status = testkit.cmd_seed(CONFIG, store, seed_args())

        self.assertEqual(status, 2)
        self.assertEqual(store.seeded, [])
        self.assertIn("upstream job", stderr.getvalue())

    def test_reseeding_the_testkits_own_row_is_allowed(self):
        store = FakeStore(
            [
                {
                    "user_id": "KUATAIRI",
                    "date": DATE,
                    "user_name": "Given Name",
                    "seeded_by": testkit.MARKER,
                }
            ]
        )

        status = testkit.cmd_seed(CONFIG, store, seed_args())

        self.assertEqual(status, 0)
        self.assertEqual(len(store.seeded), 1)

    def test_a_row_for_another_date_does_not_block_seeding(self):
        store = FakeStore(
            [
                {
                    "user_id": "KUATAIRI",
                    "date": "2026-08-24",
                    "user_name": "Real Person",
                }
            ]
        )

        status = testkit.cmd_seed(CONFIG, store, seed_args())

        self.assertEqual(status, 0)
        self.assertEqual(len(store.seeded), 1)


class UnseedSafety(unittest.TestCase):
    """The marker filter is the whole safety property of unseed."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(redirect_stdout(io.StringIO()))

    def test_unseed_always_filters_on_the_marker(self):
        recorded = {}

        class RecordingStore:
            def delete_seeded_non_working(self, marker, user_id="", date_iso=""):
                recorded.update(
                    marker=marker, user_id=user_id, date_iso=date_iso
                )
                return 1

        status = testkit.cmd_unseed(
            CONFIG,
            RecordingStore(),
            argparse.Namespace(user="KUATAIRI", date=DATE),
        )

        self.assertEqual(status, 0)
        # Without the marker this would delete rows the upstream job wrote.
        self.assertEqual(recorded["marker"], testkit.MARKER)
        self.assertEqual(recorded["user_id"], "KUATAIRI")
        self.assertEqual(recorded["date_iso"], DATE)


if __name__ == "__main__":
    unittest.main()
