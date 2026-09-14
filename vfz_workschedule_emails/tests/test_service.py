"""Tests for the HTTP service wrapper: the dashboard's manual trigger.

The run itself is covered by test_run.py. What matters here is that the wrapper
around it cannot post twice, cannot lose a failure, and reports the state the
dashboard renders.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

from fastapi.testclient import TestClient  # noqa: E402

import vfz_workschedule_emails_main as service  # noqa: E402
from config import Config, ConfigError  # noqa: E402
from notifier import RunSummary  # noqa: E402
from database_helpers import DatabaseError  # noqa: E402
from wrike_helpers import WrikeError  # noqa: E402

CONFIG = Config(wrike_token="t", log_mode="do_nothing")


class ServiceTestCase(unittest.TestCase):
    """Shared setup: a client, a stubbed config, and clean module state."""

    def setUp(self):
        # The config-error path reads LOG_MODE straight from the environment,
        # because Config is what failed to build. Set it so no log line leaves
        # the test process.
        env = mock.patch.dict(os.environ, {"LOG_MODE": "do_nothing"})
        env.start()
        self.addCleanup(env.stop)

        self.client = TestClient(service.app)
        # Config.from_segredo() would read the real segredo.ini and demand a Wrike
        # token; every route goes through _config, so stubbing that is enough.
        patcher = mock.patch.object(
            service, "_config", side_effect=lambda dry_run=None: CONFIG
        )
        self.config_mock = patcher.start()
        self.addCleanup(patcher.stop)

        service._last_run = None
        self.addCleanup(setattr, service, "_last_run", None)
        if service._lock.locked():  # a previous failure could have left it held
            service._lock.release()


class Heartbeat(ServiceTestCase):
    def test_hello_touches_no_dependency(self):
        # Deliberately not stubbing anything else: /hello must answer even when
        # the database and Wrike are both unreachable.
        body = self.client.get("/hello").json()

        self.assertEqual(body["status"], "alive")
        self.assertEqual(body["port"], 5008)
        self.assertEqual(body["triggerPath"], f"{service.EMAILS_PATH}/")


class Trigger(ServiceTestCase):
    def test_a_run_is_accepted_and_the_result_recorded(self):
        summary = RunSummary(date="2026-08-25", comments_posted=2)
        with mock.patch.object(
            service, "run_from_env", return_value=summary
        ) as run_from_env:
            response = self.client.post(
                f"{service.EMAILS_PATH}/",
                json={"dryRun": True, "force": True, "triggeredBy": "dashboard"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "accepted")
        self.assertTrue(body["accepted"])
        self.assertTrue(body["dryRun"])
        # BackgroundTasks run before TestClient returns, so the result is there.
        self.assertEqual(service.last_run()["commentsPosted"], 2)
        self.assertEqual(service.last_run()["triggeredBy"], "dashboard")
        self.assertEqual(service.last_run()["status"], "success")
        run_from_env.assert_called_once_with(
            force_window=True, dry_run=True, only_task_ids=None, title_contains=None
        )

    def test_defaults_match_the_cron(self):
        """No flags means a live run inside the window -- what the cron does.

        A default of dry_run would be worse than useless: it would look like a
        successful run while posting nothing.
        """
        with mock.patch.object(
            service, "run_from_env", return_value=RunSummary()
        ) as run_from_env:
            response = self.client.post(f"{service.EMAILS_PATH}/", json={})

        self.assertFalse(response.json()["dryRun"])
        run_from_env.assert_called_once_with(
            force_window=False, dry_run=None, only_task_ids=None, title_contains=None
        )

    def test_scope_narrowing_is_passed_through(self):
        with mock.patch.object(
            service, "run_from_env", return_value=RunSummary()
        ) as run_from_env:
            self.client.post(
                f"{service.EMAILS_PATH}/",
                json={
                    "onlyTaskIds": ["IEATASK1", "IEATASK2"],
                    "titleContains": "dydxtest",
                },
            )

        kwargs = run_from_env.call_args.kwargs
        self.assertEqual(kwargs["only_task_ids"], {"IEATASK1", "IEATASK2"})
        self.assertEqual(kwargs["title_contains"], "dydxtest")

    def test_a_second_run_is_refused_while_one_is_in_flight(self):
        """Two overlapping runs could both post the same comment.

        The notification history is what makes notify-once hold, and two runs
        reading it before either writes would both decide the comment is needed.
        """
        service._lock.acquire()
        try:
            response = self.client.post(f"{service.EMAILS_PATH}/", json={})
        finally:
            service._lock.release()

        body = response.json()
        self.assertEqual(body["status"], "ignored")
        self.assertFalse(body["accepted"])
        self.assertIn("already in progress", body["message"])

    def test_a_wrike_failure_is_recorded_not_lost(self):
        with mock.patch.object(
            service, "run_from_env", side_effect=WrikeError("401 unauthorized")
        ):
            response = self.client.post(f"{service.EMAILS_PATH}/", json={})

        # The trigger still succeeded -- the run is what failed, and the
        # dashboard learns that from the recorded result.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.last_run()["status"], "failed")
        self.assertIn("Wrike API error", service.last_run()["message"])

    def test_a_database_failure_is_recorded_not_lost(self):
        with mock.patch.object(
            service, "run_from_env", side_effect=DatabaseError("2003 cannot connect")
        ):
            self.client.post(f"{service.EMAILS_PATH}/", json={})

        self.assertEqual(service.last_run()["status"], "failed")
        self.assertIn("MySQL error", service.last_run()["message"])

    def test_an_unexpected_failure_is_recorded_not_lost(self):
        """An exception escaping a background task would otherwise vanish."""
        with mock.patch.object(
            service, "run_from_env", side_effect=RuntimeError("boom")
        ):
            self.client.post(f"{service.EMAILS_PATH}/", json={})

        self.assertEqual(service.last_run()["status"], "failed")
        self.assertIn("unexpected error", service.last_run()["message"])

    def test_the_lock_is_released_after_a_failure(self):
        """A crashed run must not wedge the service until restart."""
        with mock.patch.object(
            service, "run_from_env", side_effect=RuntimeError("boom")
        ):
            self.client.post(f"{service.EMAILS_PATH}/", json={})

        self.assertFalse(service.is_running())

    def test_a_run_with_failures_reports_attention_not_success(self):
        with mock.patch.object(
            service, "run_from_env", return_value=RunSummary(failures=1)
        ):
            self.client.post(f"{service.EMAILS_PATH}/", json={})

        self.assertEqual(service.last_run()["status"], "attention")

    def test_an_upstream_late_run_reports_attention(self):
        """Posting nothing because the resync never ran is not a success."""
        with mock.patch.object(
            service, "run_from_env", return_value=RunSummary(upstream_late=True)
        ):
            self.client.post(f"{service.EMAILS_PATH}/", json={})

        self.assertEqual(service.last_run()["status"], "attention")


class Status(ServiceTestCase):
    def test_the_window_and_scope_are_reported(self):
        body = self.client.get(f"{service.EMAILS_PATH}/status").json()

        self.assertFalse(body["running"])
        self.assertIsNone(body["lastRun"])
        self.assertEqual(body["window"], "07:00-18:00")
        self.assertEqual(body["timezone"], "Africa/Johannesburg")
        self.assertEqual(body["spaceId"], CONFIG.space_id)
        self.assertEqual(body["nonWorkingTable"], CONFIG.non_working_table)

    def test_a_config_error_is_a_503_not_a_500(self):
        """The one failure an operator can actually fix must read as such."""
        self.config_mock.side_effect = ConfigError("WRIKE_API_TOKEN is not set")

        response = self.client.get(f"{service.EMAILS_PATH}/status")

        self.assertEqual(response.status_code, 503)
        self.assertIn("Configuration error", response.json()["detail"])


class Data(ServiceTestCase):
    def _store(self, non_working=(), notifications=(), baseline=0):
        store = mock.Mock()
        store.non_working_rows_for.return_value = list(non_working)
        store.notification_records.return_value = list(notifications)
        store.baseline_count.return_value = baseline
        return store

    def test_the_two_halves_are_returned_together(self):
        store = self._store(
            non_working=[{"user_id": "KUATAIRI", "user_name": "Abigail Hlalele"}],
            notifications=[{"approvalId": "IEAAPPROVAL", "taskId": "IEATASK"}],
            baseline=3,
        )
        with mock.patch.object(service, "build_store", return_value=store):
            body = self.client.get(
                f"{service.EMAILS_PATH}/data?date=2026-08-25"
            ).json()

        self.assertEqual(body["date"], "2026-08-25")
        self.assertEqual(body["nonWorkingCount"], 1)
        self.assertEqual(body["notificationCount"], 1)
        self.assertEqual(body["baselinedApprovals"], 3)
        store.close.assert_called_once()

    def test_an_unreachable_database_is_a_503(self):
        store = mock.Mock()
        store.ensure_schema.side_effect = DatabaseError("2003 cannot connect")
        with mock.patch.object(service, "build_store", return_value=store):
            response = self.client.get(f"{service.EMAILS_PATH}/data")

        self.assertEqual(response.status_code, 503)
        # The connection is handed back even on the failure path.
        store.close.assert_called_once()

    def test_the_history_limit_is_bounded(self):
        """An unbounded limit would let one request pull the whole history."""
        response = self.client.get(f"{service.EMAILS_PATH}/data?historyLimit=5000")

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
