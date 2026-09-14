# =============================================================================
# config.py
# Runtime configuration for the Workschedule Availability API, from segredo.ini.
# =============================================================================

import os
import sys
from dataclasses import dataclass

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import config_helpers
import database_helpers
from config_helpers import ConfigError  # noqa: F401 — re-exported for callers


@dataclass(frozen=True)
class Config:
    """Everything this API needs. Only api_token has no usable default."""

    api_token: str

    mysql_host: str = "localhost"
    mysql_database: str = "vfz-wrike-workschedule-v1"
    mysql_user: str = "admin"
    mysql_password: str = ""
    mysql_port: int = 3306
    mysql_timeout_seconds: int = 30
    non_working_table: str = database_helpers.NON_WORKING_DAYS_TABLE

    papertrail_url: str = ""
    papertrail_token: str = ""
    log_keyword: str = "vfz_workschedule_availability"
    log_mode: str = ""

    bind: str = "0.0.0.0"
    port: int = 5009
    timezone: str = "Africa/Johannesburg"
    cache_ttl_seconds: int = 60
    log_every_requests: int = 200
    # A handful of connections: Workato can fire several @tag lookups at once,
    # and nearly all of them are answered from cache anyway.
    mysql_pool_size: int = 4

    @classmethod
    def from_segredo(cls, path: str = None) -> "Config":
        cfg = config_helpers.load_segredo(path)

        config = cls(
            # Required, and refused when empty. This is the one service
            # reachable from the internet, so starting without a token would
            # publish the staff absence list.
            api_token=config_helpers.get_secret(
                cfg, "availability", "api_token", required=True
            ),
            mysql_host=config_helpers.get_str(cfg, "database", "host", required=True),
            mysql_database=config_helpers.get_str(
                cfg, "database", "name", required=True
            ),
            mysql_user=config_helpers.get_str(cfg, "database", "user", required=True),
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
            papertrail_url=config_helpers.get_str(cfg, "papertrail", "url"),
            papertrail_token=config_helpers.get_secret(cfg, "papertrail", "token"),
            log_keyword=config_helpers.get_str(
                cfg, "availability", "log_keyword", "vfz_workschedule_availability"
            ),
            log_mode=config_helpers.get_str(cfg, "papertrail", "mode", ""),
            bind=config_helpers.get_str(cfg, "availability", "bind", "0.0.0.0"),
            port=config_helpers.get_int(cfg, "availability", "port", 5009),
            timezone=config_helpers.get_str(
                cfg, "availability", "timezone", "Africa/Johannesburg"
            ),
            cache_ttl_seconds=config_helpers.get_int(
                cfg, "availability", "cache_ttl_seconds", 60
            ),
            log_every_requests=config_helpers.get_int(
                cfg, "availability", "log_every_requests", 200
            ),
        )

        if config.cache_ttl_seconds < 0:
            raise ConfigError(
                f"[availability] cache_ttl_seconds must not be negative, got "
                f"{config.cache_ttl_seconds}"
            )

        _check_timezone(config.timezone)
        return config


def _check_timezone(name: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone {name!r} is not a known time zone: {exc}") from exc
