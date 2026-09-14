# =============================================================================
# config_helpers.py
# Reads segredo.ini and validates the values both integrations depend on.
#
# One segredo.ini serves both. That is deliberate: they share a database, a
# table, a Wrike account and a log collector, and keeping two copies of those
# meant a credential rotation had to be done twice -- with the second copy
# discovered only when something failed.
#
# The validators live here rather than in each integration because a bad value
# should fail the same way in both: a ConfigError at startup, not a traceback
# part-way through a run.
# =============================================================================

import configparser
import os
from typing import Optional

# segredo.ini sits next to this file, in shared/. Each integration finds it by
# adding ../shared to sys.path and importing this module, so no integration
# needs to know the path.
SHARED_DIR = os.path.dirname(os.path.abspath(__file__))
SEGREDO_PATH = os.path.join(SHARED_DIR, "segredo.ini")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or unusable."""


def load_segredo(path: Optional[str] = None) -> configparser.ConfigParser:
    """
    Read segredo.ini and return the parsed config.

    :param path: Override the default location. Used by tests.
    :returns:    Parsed ConfigParser.
    :raises ConfigError: if the file is absent -- there is no usable default
                         for a Wrike token, so failing here beats failing with
                         a confusing 401 later.
    """
    config_path = path or SEGREDO_PATH
    if not os.path.isfile(config_path):
        raise ConfigError(
            f"segredo.ini not found at {config_path}. Copy "
            f"shared/segredo.ini.example to shared/segredo.ini and fill it in."
        )

    config = configparser.ConfigParser()
    # Read explicitly rather than via read(), which silently ignores a file it
    # cannot parse and leaves you with an empty config.
    with open(config_path, "r", encoding="utf-8") as handle:
        config.read_file(handle)
    return config


# ---------------------------------------------------------------------------
# Typed getters -- each one names the setting in its error, so a bad value
# says which line of segredo.ini to go and look at.
# ---------------------------------------------------------------------------


def get_str(
    config: configparser.ConfigParser,
    section: str,
    option: str,
    default: Optional[str] = None,
    required: bool = False,
) -> str:
    """Read a string. `required=True` refuses an empty value."""
    value = (config.get(section, option, fallback=default) or "").strip()
    if required and not value:
        raise ConfigError(
            f"[{section}] {option} is required in segredo.ini and is empty"
        )
    return value


def get_secret(
    config: configparser.ConfigParser,
    section: str,
    option: str,
    default: str = "",
    required: bool = False,
) -> str:
    """
    Read a secret, preserving surrounding whitespace.

    Not stripped, unlike get_str: a password may legitimately begin or end with
    a space, and silently trimming one turns a correct secret into an
    authentication failure nobody can explain.
    """
    value = config.get(section, option, fallback=default) or ""
    if required and not value.strip():
        raise ConfigError(
            f"[{section}] {option} is required in segredo.ini and is empty"
        )
    return value


def get_int(
    config: configparser.ConfigParser, section: str, option: str, default: int
) -> int:
    """Read an integer."""
    raw = (config.get(section, option, fallback=str(default)) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"[{section}] {option} must be an integer, got {raw!r}"
        ) from exc


def get_bool(
    config: configparser.ConfigParser,
    section: str,
    option: str,
    default: bool = False,
) -> bool:
    """
    Read a boolean.

    An unrecognised spelling is an error, not a silent False: reading an
    intended `dry_run = true` as False is the difference between a rehearsal
    and real comments on real Wrike tasks.
    """
    raw = (config.get(section, option, fallback="") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(
        f"[{section}] {option} must be one of 1/true/yes/on or 0/false/no/off, "
        f"got {raw!r}"
    )


def get_hour(
    config: configparser.ConfigParser,
    section: str,
    option: str,
    default: int,
    upper: int = 23,
) -> int:
    """
    Read an hour-of-day setting, range checked before anything can use it.

    An out-of-range hour reaches datetime.replace(hour=...), where it raises
    deep inside the run rather than at startup.
    """
    value = get_int(config, section, option, default)
    if not 0 <= value <= upper:
        raise ConfigError(
            f"[{section}] {option} must be between 0 and {upper}, got {value}"
        )
    return value


def get_table_name(
    config: configparser.ConfigParser, section: str, option: str, default: str
) -> str:
    """
    Read a table name and check it is safe to interpolate into SQL.

    An identifier cannot be a bind parameter, so table names are interpolated.
    These come from segredo.ini, never from a request, but a typo must fail at
    startup instead of becoming a syntax error mid-run.
    """
    from database_helpers import is_safe_table_name

    value = get_str(config, section, option, default)
    if not is_safe_table_name(value):
        raise ConfigError(
            f"[{section}] {option} must be a plain table name (letters, digits "
            f"and underscores), got {value!r}"
        )
    return value
