"""The zone stamped onto timestamps on the way out of the store.

MySQL returns a DATETIME with no offset. Serialised straight to JSON that is
"2026-08-31T11:47:08", which a browser reads as its own local time -- so a
comment recorded at 11:47 SAST rendered on the dashboard as 09:47, two hours
behind the run that posted it. These cover the fix without needing a server.
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

from store import Store  # noqa: E402


class FakeConnection:
    """Just enough DatabaseConnection for the shaping helpers."""

    def __init__(self, zone: str = "") -> None:
        self.zone = zone


def store_in(zone: str) -> Store:
    return Store(FakeConnection(zone))


class Localising(unittest.TestCase):
    def test_a_naive_timestamp_gets_the_sessions_offset(self):
        rows = store_in("Africa/Johannesburg")._localise(
            [{"lastNotifiedAt": datetime(2026, 8, 31, 11, 47, 8)}]
        )

        moment = rows[0]["lastNotifiedAt"]
        self.assertEqual(moment.utcoffset().total_seconds(), 7200)
        # The wall-clock reading is untouched; only its meaning is pinned down.
        self.assertEqual(moment.isoformat(), "2026-08-31T11:47:08+02:00")

    def test_both_timestamp_columns_are_covered(self):
        rows = store_in("Africa/Johannesburg")._localise(
            [
                {
                    "firstNotifiedAt": datetime(2026, 8, 31, 9, 0, 0),
                    "lastNotifiedAt": datetime(2026, 8, 31, 11, 47, 8),
                }
            ]
        )

        self.assertIsNotNone(rows[0]["firstNotifiedAt"].tzinfo)
        self.assertIsNotNone(rows[0]["lastNotifiedAt"].tzinfo)

    def test_an_already_aware_timestamp_is_left_alone(self):
        aware = datetime(2026, 8, 31, 11, 47, 8, tzinfo=timezone.utc)

        rows = store_in("Africa/Johannesburg")._localise([{"lastNotifiedAt": aware}])

        self.assertEqual(rows[0]["lastNotifiedAt"], aware)

    def test_no_zone_configured_falls_back_to_utc(self):
        """Never left naive: an unlabelled timestamp is the actual bug."""
        rows = store_in("")._localise(
            [{"lastNotifiedAt": datetime(2026, 8, 31, 9, 47, 8)}]
        )

        self.assertEqual(rows[0]["lastNotifiedAt"].utcoffset().total_seconds(), 0)

    def test_a_missing_or_null_timestamp_is_not_invented(self):
        rows = store_in("Africa/Johannesburg")._localise(
            [{"lastNotifiedAt": None}, {"approvalId": "IEAAPPROVAL"}]
        )

        self.assertIsNone(rows[0]["lastNotifiedAt"])
        self.assertNotIn("lastNotifiedAt", rows[1])


if __name__ == "__main__":
    unittest.main()
