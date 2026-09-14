# =============================================================================
# vfz_workschedule_db_resync/vfz_workschedule_db_resync_main.py
# Integration 1: Workschedule DB Resync
#
# Expands each Wrike work schedule's weekday pattern into dates and writes one
# row per user per non-working date into MySQL. The Workschedule Emails
# integration reads that table, so this one feeds it.
#
# API:     POST /vfz_workschedule_db_resync/          start a resync
#          GET  /vfz_workschedule_db_resync/status    what the last run did
#          GET  /vfz_workschedule_db_resync/non-working-days   who is off
#          GET  /hello                                heartbeat
# Trigger: the VFZ dashboard's Work Schedules page, and a nightly cron
#          (run_resync.py, which runs the same code)
# Port:    5007, bound to 127.0.0.1 — see the note on binding below
#
# Process:
#   1. Read every work schedule and its members from Wrike
#   2. Resolve display names, so the Emails job needs no second API call
#   3. Expand each weekday pattern into dates across the window
#   4. Guard against a bulk change flipping most of a large schedule at once
#   5. Upsert the dates, and remove that user's in-window rows that should
#      no longer exist
#   6. Log the whole result to SolarWinds as a JSON object
# =============================================================================

import os
import sys
from contextlib import asynccontextmanager
from datetime import date as Date
from typing import Any, Dict, Optional

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query

# ---------------------------------------------------------------------------
# Add shared helpers to the module search path
# ---------------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import general_helpers
from database_helpers import DatabaseError

import resync
from config import Config, ConfigError
from models import ResyncRequest, ResyncStatusResponse, ResyncTriggerResponse

# ---------------------------------------------------------------------------
# Configuration — load from segredo.ini at startup
# ---------------------------------------------------------------------------

# Read once, at import, so a bad segredo.ini stops the service immediately
# rather than on the first request. Exit 2 with the reason rather than a
# traceback: this is the one failure an operator can actually fix, and it is
# what they will see in `journalctl -u vfz-workschedule-db-resync`.
try:
    CONFIG = Config.from_segredo()
except ConfigError as exc:
    print(f"Configuration error: {exc}", file=sys.stderr)
    raise SystemExit(2)

# SolarWinds logging
general_helpers.papertrail_url = CONFIG.papertrail_url
general_helpers.papertrail_token = CONFIG.papertrail_token
general_helpers.system_identifier = CONFIG.log_keyword
general_helpers.log_mode = CONFIG.log_mode

# The slug the dashboard and any reverse proxy use. Stable forever.
RESYNC_PATH = "/vfz_workschedule_db_resync"

_UNAVAILABLE = "Work-schedule data is temporarily unavailable. Retry the request."


def _stamp(task_id: str = "") -> Dict[str, Any]:
    return general_helpers.make_stamp(task_id=task_id, mode=CONFIG.log_mode)


# One pool for the read endpoints, opened on first use and kept for the life of
# the process. Building one per request meant a fresh TCP + TLS handshake every
# time -- and the dashboard polls /status every two seconds while a resync
# runs, so that was the dominant cost of the whole page. The resync itself
# still opens its own short-lived pool: it is a background job, and its
# connection has a very different lifetime from a request's.
_read_repository = None


def _reader():
    """The shared read repository. Raises DatabaseError if MySQL is unreachable."""
    global _read_repository
    if _read_repository is None:
        _read_repository = resync.build_repository(
            CONFIG, pool_name="db_resync_reads"
        )
    return _read_repository


def _close_reader() -> None:
    global _read_repository
    if _read_repository is not None:
        _read_repository.close()
        _read_repository = None


# ---------------------------------------------------------------------------
# MAIN — the FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    oStamp = _stamp()

    schema_ready = True
    schema_error = ""
    try:
        repository = resync.build_repository(CONFIG, pool_name="db_resync_startup")
        try:
            repository.ensure_schema()
        finally:
            repository.close()
    except DatabaseError as exc:
        # Startup must not fail on a transient database problem; /status
        # reports it, and the first resync retries the connection.
        schema_ready = False
        schema_error = str(exc)
        general_helpers.log_error(
            oStamp,
            "Startup could not reach MySQL; schema check skipped",
            exc,
            {"host": CONFIG.mysql_host, "database": CONFIG.mysql_database},
        )

    general_helpers.log_message(
        oStamp,
        "Workschedule DB Resync starting",
        {
            "database": CONFIG.mysql_database,
            "table": CONFIG.non_working_table,
            "host": CONFIG.mysql_host,
            "timezone": CONFIG.timezone,
            "horizonDays": CONFIG.horizon_days,
            "bind": f"{CONFIG.bind}:{CONFIG.port}",
            "schemaReady": schema_ready,
            "schemaError": schema_error,
        },
    )

    yield

    general_helpers.log_message(
        oStamp,
        "Workschedule DB Resync shutting down",
        {"database": CONFIG.mysql_database, "table": CONFIG.non_working_table},
    )
    _close_reader()


app = FastAPI(
    title="VFZ Workschedule DB Resync",
    description=(
        "Expands Wrike work schedules into non-working dates and writes them "
        "to MySQL. Read-only against Wrike: it never writes to Wrike and never "
        "changes an approval."
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.post(f"{RESYNC_PATH}/", response_model=ResyncTriggerResponse, tags=["resync"])
def trigger_resync(
    background_tasks: BackgroundTasks, payload: Optional[ResyncRequest] = None
) -> ResyncTriggerResponse:
    """
    Kick off a resync and return immediately.

    A full Wrike resync takes long enough — one /workschedules call plus a
    /contacts lookup per 100 users — that holding the HTTP connection open
    would time out behind a proxy. So the work is handed to a background task
    and the caller polls the status route, which is what the dashboard does.
    SolarWinds gets the outcome either way.
    """
    request = payload or ResyncRequest()
    oStamp = _stamp()

    if resync.is_running():
        # Refused rather than queued: by the time a queued run started, the one
        # in flight would already have written the same answer.
        general_helpers.log_message(
            oStamp,
            "Resync trigger ignored: a resync is already in progress",
            {"triggeredBy": request.triggered_by},
        )
        return ResyncTriggerResponse(
            status="ignored",
            message="A resync is already in progress. Poll the status endpoint.",
            accepted=False,
            running=True,
        )

    date_from, date_to = resync.resolve_window(
        CONFIG, request.date_from, request.date_to, request.today_only
    )
    if date_to < date_from:
        raise HTTPException(
            status_code=422, detail="'dateTo' must not be earlier than 'dateFrom'."
        )

    background_tasks.add_task(
        resync.run_resync_in_background,
        CONFIG,
        source=request.source,
        date_from=request.date_from,
        date_to=request.date_to,
        today_only=request.today_only,
        dry_run=request.dry_run,
        purge_orphans=request.purge_orphans,
        min_schedule_size_for_guard=request.min_schedule_size_for_guard,
        max_non_working_fraction=request.max_non_working_fraction,
        triggered_by=request.triggered_by,
    )

    general_helpers.log_message(
        oStamp,
        f"Resync accepted for {date_from} to {date_to} "
        f"(triggered by {request.triggered_by})",
        {
            "triggeredBy": request.triggered_by,
            "source": request.source,
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "dryRun": request.dry_run,
            "purgeOrphans": request.purge_orphans,
        },
    )

    return ResyncTriggerResponse(
        status="accepted",
        message=(
            f"Resync started for {date_from} to {date_to}"
            + (" (dry run, nothing will be written)" if request.dry_run else "")
            + ". Poll the status endpoint for the result."
        ),
        accepted=True,
        running=True,
        date_from=date_from,
        date_to=date_to,
        dry_run=request.dry_run,
    )


@app.get(
    f"{RESYNC_PATH}/status", response_model=ResyncStatusResponse, tags=["resync"]
)
def resync_status() -> ResyncStatusResponse:
    """
    What the last resync did, and whether one is running now.

    ``lastRun`` comes from the run log in MySQL, which every run writes to — so
    the nightly cron, a dashboard click and a CLI invocation all appear here,
    and a restart of this service does not erase the answer. ``lastSuccessfulRun``
    is the same record filtered to runs that actually wrote the table, which is
    what "last sync" means to a person looking at the dashboard.

    Null means no run has ever been logged. If MySQL is unreachable this falls
    back to the last run THIS process did, and ``databaseReachable`` is false.
    """
    n_rows = None
    database_reachable = True
    repository = None
    try:
        repository = _reader()
        n_rows = repository.count_rows()
    except Exception as exc:
        general_helpers.log_error(
            _stamp(), "Resync status could not read the table", exc
        )
        database_reachable = False
        repository = None
        # Drop the pool so the next poll rebuilds it rather than reusing a
        # connection that has gone bad.
        _close_reader()

    return ResyncStatusResponse(
        running=resync.is_running(),
        last_run=resync.last_run(repository),
        last_successful_run=resync.last_run(repository, status="success"),
        database=CONFIG.mysql_database,
        table=CONFIG.non_working_table,
        timezone=CONFIG.timezone,
        today=resync.today_in(CONFIG.timezone),
        horizon_days=CONFIG.horizon_days,
        row_count=n_rows,
        database_reachable=database_reachable,
    )


@app.get(f"{RESYNC_PATH}/non-working-days", tags=["resync"])
def non_working_days(
    on_date: Optional[Date] = Query(default=None, alias="date"),
) -> Dict[str, Any]:
    """
    Everyone recorded as not working on one date. Defaults to today.

    This is the same question the Emails job asks, so the two agree by
    construction — which is what makes it useful for support: if the dashboard
    shows somebody off and no comment was posted, the disagreement is not here.
    """
    evaluated = on_date or resync.today_in(CONFIG.timezone)
    oStamp = _stamp()
    try:
        repository = _reader()
        l_rows = repository.rows_for_date(evaluated)
        n_rows = repository.count_rows()
    except DatabaseError as exc:
        general_helpers.log_error(
            oStamp,
            "Non-working-day listing failed: MySQL unavailable",
            exc,
            {"date": evaluated.isoformat()},
        )
        _close_reader()
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc

    general_helpers.log_message(
        oStamp,
        f"Non-working-day listing complete: {len(l_rows)} user(s) off on {evaluated}",
        {"date": evaluated.isoformat(), "count": len(l_rows)},
    )
    return {
        "date": evaluated.isoformat(),
        "weekday": evaluated.strftime("%A"),
        "timezone": CONFIG.timezone,
        "table": CONFIG.non_working_table,
        "tableRowCount": n_rows,
        "count": len(l_rows),
        "users": l_rows,
    }


# ---------------------------------------------------------------------------
# HEARTBEAT
# ---------------------------------------------------------------------------


@app.get("/hello", tags=["ops"])
def hello() -> Dict[str, Any]:
    """Cheapest possible "is the process up" check. Touches no dependency."""
    return {
        "status": "alive",
        "service": CONFIG.log_keyword,
        "resyncPath": f"{RESYNC_PATH}/",
        "port": CONFIG.port,
    }


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------


def main() -> None:
    # Bound to 127.0.0.1 by default. The dashboard is the only caller and it
    # runs on the same host, so there is nothing to gain from listening on a
    # public interface — and nothing to authenticate, which removes a whole
    # class of "the token in segredo.ini does not match the one in .env"
    # failures. Widen [db_resync] bind only alongside auth in front of it.
    uvicorn.run(app, host=CONFIG.bind, port=CONFIG.port)


if __name__ == "__main__":
    main()

    # TO RUN (from the vfz_workschedule_db_resync/ directory):
    #   DEV:  python vfz_workschedule_db_resync_main.py
    #   PROD: managed by systemd — see deploy/ and ../DEPLOYMENT.md
    # The nightly cron calls run_resync.py, which runs the same code.
