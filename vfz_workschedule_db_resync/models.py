# =============================================================================
# models.py
# Request and response schemas for the resync endpoints.
#
# JSON is camelCase so the payloads line up with the Wrike conventions the
# dashboard already uses; Python attributes stay snake_case, and the MySQL
# columns are snake_case too -- repository.py aliases them on the way out.
# =============================================================================

from datetime import date as Date
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        ser_json_by_alias=True,
    )


class ResyncRequest(CamelModel):
    """What to resync. Every field is optional: the defaults are the cron's."""

    source: str = Field(
        default="wrike",
        pattern="^(wrike|json)$",
        description=(
            "Where to read the weekday patterns from. 'json' reads the local "
            "schedules file, for testing without a Wrike token."
        ),
    )
    date_from: Optional[Date] = Field(
        default=None, description="First date of the window. Omit for today."
    )
    date_to: Optional[Date] = Field(
        default=None,
        description="Last date of the window. Omit for a HORIZON_DAYS-long window.",
    )
    today_only: bool = Field(
        default=False, description="Force a one-day window: today only."
    )
    dry_run: bool = Field(
        default=False,
        description="Report what would change without writing anything.",
    )
    purge_orphans: bool = Field(
        default=False,
        description=(
            "Also delete rows in the window for users the source did not return "
            "at all. Only safe when the source lists every user."
        ),
    )
    # None means "use the value in segredo.ini". Defaulting these to literals
    # here would silently override a tuned [db_resync] setting on every call
    # from the dashboard, which sends neither.
    min_schedule_size_for_guard: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Schedules with fewer members than this are never blast-radius "
            "guarded. Omit to use the configured value."
        ),
    )
    max_non_working_fraction: Optional[float] = Field(
        default=None,
        gt=0,
        le=1,
        description=(
            "Refuse to write a schedule if more than this fraction of its "
            "members would newly be marked not working on the same date. "
            "Omit to use the configured value."
        ),
    )
    triggered_by: str = Field(
        default="api",
        max_length=64,
        description=(
            "Who asked for this run, e.g. 'dashboard'. Carried into every log "
            "line, so a manual resync is distinguishable from the cron in "
            "SolarWinds -- the first question asked when a day looks wrong."
        ),
    )


class ResyncTriggerResponse(CamelModel):
    """Acknowledgement that a resync was started. Not its result."""

    status: str = Field(description="'accepted' or 'ignored'.")
    message: str
    accepted: bool = Field(
        description="False when a resync was already running and this was ignored."
    )
    running: bool
    date_from: Optional[Date] = None
    date_to: Optional[Date] = None
    dry_run: bool = False


class ResyncStatusResponse(CamelModel):
    """What the last resync did, and the state of the table it writes."""

    running: bool
    last_run: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "The last resync's full result, from the run log in MySQL. Cron "
            "runs, dashboard clicks and CLI runs all appear here, and it "
            "survives a restart of this service. Null means nothing has ever "
            "been logged."
        ),
    )
    last_successful_run: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "The last run that actually wrote the table — what a person means "
            "by 'last sync'. Null when no successful run has been logged."
        ),
    )
    database: str
    table: str
    timezone: str
    today: Date
    horizon_days: int
    row_count: Optional[int] = None
    database_reachable: bool = True
