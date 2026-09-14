# =============================================================================
# config.py
# Runtime configuration for the Workschedule DB Resync, read from segredo.ini.
#
# Every value is validated here, at startup, so a bad one stops the service
# with a clear ConfigError rather than surfacing as a traceback part-way
# through a run — or, worse, as a window that silently never opens.
# =============================================================================

import os
import sys
from dataclasses import dataclass

# Shared helpers live one directory up. Appended before the imports below, the
# same way the process-simplification integrations do it.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import config_helpers
import database_helpers
from config_helpers import ConfigError  # noqa: F401 — re-exported for callers


@dataclass(frozen=True)
class Config:
    """Everything the resync needs, resolved from segredo.ini."""

    # --- Wrike ---
    wrike_token: str
    wrike_base_url: str
    wrike_request_timeout: int

    # --- MySQL ---
    mysql_host: str
    mysql_database: str
    mysql_user: str
    mysql_password: str
    mysql_port: int
    mysql_timeout_seconds: int
    non_working_table: str
    # One row per run, so "last sync" on the dashboard survives the process and
    # a cron run shows up there exactly like a clicked one.
    runs_table: str

    # --- SolarWinds ---
    papertrail_url: str
    papertrail_token: str
    log_keyword: str
    log_mode: str

    # --- Service ---
    bind: str
    port: int

    # --- Business rules ---
    timezone: str
    skip_weekends: bool
    horizon_days: int
    schedules_file: str
    min_schedule_size_for_guard: int
    max_non_working_fraction: float

    @classmethod
    def from_segredo(cls, path: str = None) -> "Config":
        """
        Build the config from segredo.ini.

        :raises ConfigError: on a missing file, a missing Wrike token, or any
                             value that cannot be read as its declared type.
        """
        cfg = config_helpers.load_segredo(path)

        config = cls(
            wrike_token=config_helpers.get_secret(
                cfg, "wrike", "api_token", required=True
            ),
            wrike_base_url=config_helpers.get_str(
                cfg, "wrike", "api_base_url", "https://app-eu.wrike.com/api/v4"
            ),
            wrike_request_timeout=config_helpers.get_int(
                cfg, "wrike", "request_timeout", 30
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
            runs_table=config_helpers.get_table_name(
                cfg,
                "database",
                "runs_table",
                database_helpers.RESYNC_RUNS_TABLE,
            ),
            papertrail_url=config_helpers.get_str(cfg, "papertrail", "url"),
            papertrail_token=config_helpers.get_secret(cfg, "papertrail", "token"),
            log_keyword=config_helpers.get_str(
                cfg, "db_resync", "log_keyword", "vfz_workschedule_db_resync"
            ),
            log_mode=config_helpers.get_str(cfg, "papertrail", "mode", ""),
            # 127.0.0.1 by default: the dashboard is the only caller and it runs
            # on the same host, so there is nothing to gain from listening on a
            # public interface and no bearer token to keep in step. Widen this
            # only alongside authentication in front of it.
            bind=config_helpers.get_str(cfg, "db_resync", "bind", "127.0.0.1"),
            port=config_helpers.get_int(cfg, "db_resync", "port", 5007),
            timezone=config_helpers.get_str(
                cfg, "db_resync", "timezone", "Africa/Johannesburg"
            ),
            skip_weekends=config_helpers.get_bool(
                cfg, "db_resync", "skip_weekends", True
            ),
            horizon_days=config_helpers.get_int(cfg, "db_resync", "horizon_days", 1),
            schedules_file=config_helpers.get_str(
                cfg, "db_resync", "schedules_file", "schedules.json"
            ),
            min_schedule_size_for_guard=config_helpers.get_int(
                cfg, "db_resync", "min_schedule_size_for_guard", 10
            ),
            max_non_working_fraction=_fraction(
                cfg, "db_resync", "max_non_working_fraction", 0.5
            ),
        )

        # Checked here so a typo surfaces as a config error at startup rather
        # than as a ZoneInfoNotFoundError once the run is under way.
        _check_timezone(config.timezone)

        if config.horizon_days < 1:
            raise ConfigError(
                f"[db_resync] horizon_days must be at least 1 (1 = today only), "
                f"got {config.horizon_days}"
            )

        return config


def _fraction(cfg, section: str, option: str, default: float) -> float:
    """A 0-1 fraction. Outside that range the blast-radius guard is meaningless."""
    raw = (cfg.get(section, option, fallback=str(default)) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"[{section}] {option} must be a number between 0 and 1, got {raw!r}"
        ) from exc
    if not 0 < value <= 1:
        raise ConfigError(
            f"[{section}] {option} must be greater than 0 and at most 1, got {value}"
        )
    return value


def _check_timezone(name: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone {name!r} is not a known time zone: {exc}") from exc
