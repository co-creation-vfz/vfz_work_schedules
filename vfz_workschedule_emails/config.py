# =============================================================================
# config.py
# Runtime configuration for the Workschedule Emails job, read from segredo.ini.
#
# Every value is validated here, at startup, so a bad one stops the job with a
# clear ConfigError and exit code 2 rather than surfacing as a traceback
# part-way through a run — or as a notification window that silently never
# opens, which looks identical to a quiet day.
# =============================================================================

import os
import sys
from dataclasses import dataclass

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import config_helpers
import database_helpers
from config_helpers import ConfigError  # noqa: F401 — re-exported for callers


@dataclass(frozen=True)
class Config:
    """Everything the notifier needs, resolved from segredo.ini."""

    # Only the Wrike token has no sensible default: everything else falls back
    # to the value a real deployment uses, so a test can build a Config by
    # naming just the field it cares about.
    wrike_token: str

    # --- Wrike ---
    wrike_base_url: str = "https://app-eu.wrike.com/api/v4"
    request_timeout: int = 30
    space_id: str = "MQAAAAEEuHGf"
    recycle_bin_id: str = "IEAFXAOHI7777776"
    project_lead_field_id: str = ""
    fallback_contact_id: str = "KUARF54C"

    # --- MySQL ---
    mysql_host: str = "localhost"
    mysql_database: str = "vfz-wrike-workschedule-v1"
    mysql_user: str = "admin"
    mysql_password: str = ""
    mysql_port: int = 3306
    mysql_timeout_seconds: int = 30
    non_working_table: str = database_helpers.NON_WORKING_DAYS_TABLE
    notifications_table: str = database_helpers.APPROVAL_NOTIFICATIONS_TABLE
    baseline_table: str = database_helpers.BASELINED_APPROVALS_TABLE

    # --- SolarWinds ---
    papertrail_url: str = ""
    papertrail_token: str = ""
    log_keyword: str = "vfz_workschedule_emails"
    log_mode: str = ""

    # --- Service ---
    bind: str = "127.0.0.1"
    port: int = 5008

    # --- Timing ---
    timezone: str = "Africa/Johannesburg"
    notify_start_hour: int = 7   # inclusive
    notify_end_hour: int = 18    # exclusive
    upstream_grace_minutes: int = 10
    dry_run: bool = False

    @classmethod
    def from_segredo(cls, path: str = None) -> "Config":
        """
        Build the config from segredo.ini.

        :raises ConfigError: on a missing file, a missing Wrike token, an
                             unusable table name, a window that never opens, or
                             any value that cannot be read as its declared type.
        """
        cfg = config_helpers.load_segredo(path)

        config = cls(
            wrike_token=config_helpers.get_secret(
                cfg, "wrike", "api_token", required=True
            ),
            wrike_base_url=config_helpers.get_str(
                cfg, "wrike", "api_base_url", "https://app-eu.wrike.com/api/v4"
            ),
            request_timeout=config_helpers.get_int(
                cfg, "wrike", "request_timeout", 30
            ),
            space_id=config_helpers.get_str(
                cfg, "wrike", "space_id", "MQAAAAEEuHGf"
            ),
            recycle_bin_id=config_helpers.get_str(
                cfg, "wrike", "recycle_bin_id", "IEAFXAOHI7777776"
            ),
            project_lead_field_id=config_helpers.get_str(
                cfg, "wrike", "project_lead_field_id", ""
            ),
            fallback_contact_id=config_helpers.get_str(
                cfg, "wrike", "fallback_contact_id", "KUARF54C"
            ),
            mysql_host=config_helpers.get_str(
                cfg, "database", "host", required=True
            ),
            mysql_database=config_helpers.get_str(
                cfg, "database", "name", required=True
            ),
            mysql_user=config_helpers.get_str(
                cfg, "database", "user", required=True
            ),
            mysql_password=config_helpers.get_secret(cfg, "database", "password"),
            mysql_port=config_helpers.get_int(cfg, "database", "port", 3306),
            mysql_timeout_seconds=config_helpers.get_int(
                cfg, "database", "timeout_seconds", 30
            ),
            non_working_table=config_helpers.get_table_name(
                cfg,
                "database",
                "non_working_table",
                database_helpers.NON_WORKING_DAYS_TABLE,
            ),
            notifications_table=config_helpers.get_table_name(
                cfg,
                "database",
                "notifications_table",
                database_helpers.APPROVAL_NOTIFICATIONS_TABLE,
            ),
            baseline_table=config_helpers.get_table_name(
                cfg,
                "database",
                "baseline_table",
                database_helpers.BASELINED_APPROVALS_TABLE,
            ),
            papertrail_url=config_helpers.get_str(cfg, "papertrail", "url"),
            papertrail_token=config_helpers.get_secret(cfg, "papertrail", "token"),
            log_keyword=config_helpers.get_str(
                cfg, "emails", "log_keyword", "vfz_workschedule_emails"
            ),
            log_mode=config_helpers.get_str(cfg, "papertrail", "mode", ""),
            bind=config_helpers.get_str(cfg, "emails", "bind", "127.0.0.1"),
            port=config_helpers.get_int(cfg, "emails", "port", 5008),
            timezone=config_helpers.get_str(
                cfg, "emails", "timezone", "Africa/Johannesburg"
            ),
            notify_start_hour=config_helpers.get_hour(
                cfg, "emails", "notify_start_hour", 7
            ),
            # Exclusive, so 24 is meaningful: a window that never closes.
            notify_end_hour=config_helpers.get_hour(
                cfg, "emails", "notify_end_hour", 18, upper=24
            ),
            upstream_grace_minutes=config_helpers.get_int(
                cfg, "emails", "upstream_grace_minutes", 10
            ),
            dry_run=config_helpers.get_bool(cfg, "emails", "dry_run", False),
        )

        # A start at or after the end is not a narrow window, it is no window:
        # the job would run on schedule and skip every time, which looks
        # identical to a quiet day.
        if config.notify_start_hour >= config.notify_end_hour:
            raise ConfigError(
                "[emails] notify_start_hour must be before notify_end_hour, got "
                f"{config.notify_start_hour} and {config.notify_end_hour}; "
                "the notification window would never open"
            )

        # Checked here so a typo surfaces as a config error at startup rather
        # than as a ZoneInfoNotFoundError once the run is under way.
        _check_timezone(config.timezone)

        return config


def _check_timezone(name: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone {name!r} is not a known time zone: {exc}") from exc
