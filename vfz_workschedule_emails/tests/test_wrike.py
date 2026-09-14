"""Tests for the client's retry, batching and degradation behaviour.

No network: the transport is replaced with a scripted responder, so these cover
the decisions the client makes rather than Wrike's actual replies.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

from wrike_helpers import (  # noqa: E402
    APPROVER_FILTER_BATCH,
    MAX_RETRY_DELAY,
    WrikeClient,
    WrikeError,
    _retry_delay,
)


class FakeResponse:
    def __init__(self, headers=None):
        self.headers = headers or {}


class RecordingClient(WrikeClient):
    """A client whose _request is a scripted responder, recording every call."""

    def __init__(self, responder):
        super().__init__("token", "https://example.invalid/api/v4")
        self.calls = []
        self._responder = responder

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self._responder(method, path, kwargs)


class RetryDelay(unittest.TestCase):
    def test_numeric_retry_after_is_honoured(self):
        self.assertEqual(_retry_delay(FakeResponse({"Retry-After": "5"}), 0), 5)

    def test_http_date_retry_after_falls_back_to_backoff(self):
        # Retry-After is allowed to be an HTTP-date, which int() cannot read.
        # The old code raised ValueError here and killed the run.
        delay = _retry_delay(
            FakeResponse({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 3
        )
        self.assertEqual(delay, 8)

    def test_missing_header_falls_back_to_backoff(self):
        self.assertEqual(_retry_delay(FakeResponse(), 2), 4)

    def test_long_retry_after_is_capped(self):
        # An hourly job must not sit out an hour on the server's say-so.
        self.assertEqual(
            _retry_delay(FakeResponse({"Retry-After": "3600"}), 0), MAX_RETRY_DELAY
        )

    def test_zero_still_waits_a_moment(self):
        self.assertEqual(_retry_delay(FakeResponse({"Retry-After": "0"}), 0), 1)


class ApprovalBatching(unittest.TestCase):
    def test_approver_filter_is_split_into_batches(self):
        ids = ["KU%05d" % i for i in range(APPROVER_FILTER_BATCH * 2 + 1)]
        client = RecordingClient(lambda m, p, k: {"data": []})

        list(client.iter_pending_approvals(ids))

        self.assertEqual(len(client.calls), 3)
        asked = []
        for _, _, kwargs in client.calls:
            asked.extend(json.loads(kwargs["params"]["pendingApprovers"]))
        self.assertEqual(sorted(asked), sorted(ids))

    def test_an_approval_seen_in_two_batches_is_yielded_once(self):
        ids = ["KU%05d" % i for i in range(APPROVER_FILTER_BATCH + 1)]
        client = RecordingClient(
            lambda m, p, k: {"data": [{"id": "IEASHARED", "taskId": "IEATASK"}]}
        )

        approvals = list(client.iter_pending_approvals(ids))

        self.assertEqual(len(client.calls), 2)
        self.assertEqual([a["id"] for a in approvals], ["IEASHARED"])

    def test_empty_approver_list_makes_no_request(self):
        client = RecordingClient(lambda m, p, k: {"data": []})

        self.assertEqual(list(client.iter_pending_approvals([])), [])
        self.assertEqual(client.calls, [])

    def test_none_asks_for_every_pending_approval_unfiltered(self):
        client = RecordingClient(lambda m, p, k: {"data": []})

        list(client.iter_pending_approvals(None))

        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("pendingApprovers", client.calls[0][2]["params"])

    def test_pagination_follows_the_next_page_token(self):
        pages = [
            {"data": [{"id": "IEAA"}], "nextPageToken": "token-1"},
            {"data": [{"id": "IEAB"}]},
        ]
        client = RecordingClient(lambda m, p, k: pages.pop(0))

        seen = [a["id"] for a in client.iter_pending_approvals(None)]

        self.assertEqual(seen, ["IEAA", "IEAB"])
        self.assertEqual(client.calls[1][2]["params"]["nextPageToken"], "token-1")


class ContactDegradation(unittest.TestCase):
    @staticmethod
    def _responder(method, path, kwargs):
        ids = path.rsplit("/", 1)[1].split(",")
        if "KUBAD" in ids:
            # Wrike rejects the whole path when one id is unreadable.
            raise WrikeError("GET /contacts failed with 400")
        return {
            "data": [{"id": cid, "firstName": "First", "lastName": cid} for cid in ids]
        }

    def test_one_unreadable_id_does_not_cost_the_rest_of_the_batch(self):
        client = RecordingClient(self._responder)

        names = client.get_contact_names(["KUA", "KUBAD", "KUB"])

        self.assertEqual(names["KUA"], "First KUA")
        self.assertEqual(names["KUB"], "First KUB")
        # An unreadable contact still needs a printable label.
        self.assertEqual(names["KUBAD"], "KUBAD")

    def test_the_batch_is_retried_one_id_at_a_time(self):
        client = RecordingClient(self._responder)

        client.get_contact_names(["KUA", "KUBAD", "KUB"])

        # One failed batch, then one call per id.
        self.assertEqual(len(client.calls), 4)

    def test_names_are_cached_for_the_life_of_the_run(self):
        client = RecordingClient(self._responder)

        client.get_contact_names(["KUA"])
        client.get_contact_names(["KUA"])

        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
