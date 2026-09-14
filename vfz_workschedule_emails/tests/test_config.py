"""Tests for segredo.ini loading and validation.

Every one of these is a value that, read wrongly, fails somewhere far from the
cause: a window that never opens looks like a quiet day, a mis-typed table name
becomes a SQL syntax error mid-run, and a `dry_run` typo is the difference
between a rehearsal and real comments on real Wrike tasks. So they are all
checked at startup, and this is what proves it.
"""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))

from config import Config, ConfigError  # noqa: E402

MINIMAL = """
[wrike]
api_token = abc

[database]
host = db.internal
name = testdb
user = admin
password = secret

[papertrail]
url =
token =
mode = do_nothing

[emails]
"""


class SegredoTestCase(unittest.TestCase):
    """Writes a throwaway segredo.ini per test, so none touch the real one."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def write(self, text: str) -> str:
        path = Path(self._tmpdir.name) / "segredo.ini"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return str(path)

    def load(self, text: str = MINIMAL) -> Config:
        return Config.from_segredo(self.write(text))


class Loading(SegredoTestCase):
    def test_a_minimal_file_loads_with_defaults(self):
        config = self.load()

        self.assertEqual(config.wrike_token, "abc")
        self.assertEqual(config.mysql_host, "db.internal")
        # Everything unstated falls back to a working default.
        self.assertEqual(config.wrike_base_url, "https://app-eu.wrike.com/api/v4")
        self.assertEqual(config.notify_start_hour, 7)
        self.assertEqual(config.notify_end_hour, 18)
        self.assertEqual(config.port, 5008)
        self.assertEqual(config.bind, "127.0.0.1")
        self.assertFalse(config.dry_run)

    def test_a_missing_file_is_a_config_error(self):
        """Better here than as a confusing 401 once the run is under way."""
        with self.assertRaises(ConfigError):
            Config.from_segredo(str(Path(self._tmpdir.name) / "nope.ini"))

    def test_a_missing_wrike_token_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL.replace("api_token = abc", "api_token ="))

    def test_a_password_keeps_its_surrounding_whitespace(self):
        """Trimming one turns a correct secret into an unexplainable failure."""
        config = self.load(MINIMAL.replace("password = secret", "password =  sec ret "))

        self.assertIn("sec ret", config.mysql_password)


class Window(SegredoTestCase):
    def test_a_start_after_the_end_is_refused(self):
        """Not a narrow window but no window: it would skip every single run."""
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "notify_start_hour = 18\nnotify_end_hour = 7\n")

    def test_equal_hours_are_refused(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "notify_start_hour = 9\nnotify_end_hour = 9\n")

    def test_an_hour_out_of_range_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "notify_start_hour = 25\n")

    def test_end_hour_24_is_allowed(self):
        """Exclusive, so 24 is meaningful: a window that never closes."""
        config = self.load(MINIMAL + "notify_end_hour = 24\n")

        self.assertEqual(config.notify_end_hour, 24)

    def test_a_non_numeric_hour_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "notify_start_hour = nine\n")


class Booleans(SegredoTestCase):
    def test_every_accepted_spelling(self):
        for spelling, expected in (
            ("1", True), ("true", True), ("YES", True), ("on", True),
            ("0", False), ("false", False), ("No", False), ("off", False),
        ):
            with self.subTest(spelling=spelling):
                config = self.load(MINIMAL + f"dry_run = {spelling}\n")
                self.assertIs(config.dry_run, expected)

    def test_an_unrecognised_spelling_is_an_error_not_a_silent_false(self):
        """A typo here is the difference between a dry run and real comments."""
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "dry_run = maybe\n")


class TableNames(SegredoTestCase):
    """Table names reach SQL as identifiers, which cannot be bind parameters."""

    def test_the_defaults_are_accepted(self):
        config = self.load()

        self.assertEqual(config.non_working_table, "work_schedule_non_working_days")
        self.assertEqual(config.notifications_table, "approval_notifications")
        self.assertEqual(config.baseline_table, "baselined_approvals")

    def test_a_plain_override_is_accepted(self):
        config = self.load(
            MINIMAL.replace(
                "password = secret",
                "password = secret\nnotifications_table = approval_notifications_staging",
            )
        )

        self.assertEqual(
            config.notifications_table, "approval_notifications_staging"
        )

    def test_a_name_with_sql_in_it_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load(
                MINIMAL.replace(
                    "password = secret",
                    "password = secret\nnon_working_table = days; DROP TABLE users",
                )
            )

    def test_an_empty_name_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load(
                MINIMAL.replace(
                    "password = secret", "password = secret\nbaseline_table =    "
                )
            )


class Timezone(SegredoTestCase):
    def test_an_unknown_timezone_is_refused_at_startup(self):
        """Otherwise it surfaces as a traceback once the run is under way."""
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "timezone = Mars/Olympus_Mons\n")

    def test_a_known_timezone_is_accepted(self):
        config = self.load(MINIMAL + "timezone = Europe/London\n")

        self.assertEqual(config.timezone, "Europe/London")


if __name__ == "__main__":
    unittest.main()
