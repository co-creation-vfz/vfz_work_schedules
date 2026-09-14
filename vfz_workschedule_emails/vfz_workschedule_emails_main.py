# =============================================================================
# vfz_workschedule_emails/vfz_workschedule_emails_main.py
# Integration 2: Workschedule Emails (off-day approver notifier)
# =============================================================================
"""HTTP service wrapper around the off-day approver notifier.

The notifier is a cron job first: ``python run_notifier.py``, hourly on
weekdays. This adds a manual trigger on top of it, which is what the VFZ
dashboard's "Workschedule Emails" page calls, plus read-only endpoints so that
page can show what the job can see and what it has already sent.

    POST /vfz_workschedule_emails/         start a run
    GET  /vfz_workschedule_emails/status   what the last run did
    GET  /vfz_workschedule_emails/data     today's non-workers + recent history
    GET  /hello                            heartbeat

Port 5008, bound to loopback: the dashboard is the only caller and it runs
on the same host.

"Emails" is the user-facing name: the job posts a Wrike comment tagging the
people who need to know, and what they actually receive is Wrike's notification
email. Nothing here sends mail directly.

The run itself is unchanged -- ``notifier.run_from_env`` -- so a manual trigger
and the hourly cron cannot diverge. That matters more than it sounds: the whole
notify-once rule lives in the notification history, and two code paths writing
it differently would double-comment on a live Wrike task.
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Add shared helpers to the module search path
# ---------------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import general_helpers
from database_helpers import DatabaseError
from wrike_helpers import WrikeError

from config import Config, ConfigError
from notifier import (
    build_client,
    build_store,
    configure_logging,
    run_from_env,
)

# The slug the dashboard and Nginx use. Stable forever.
EMAILS_PATH = "/vfz_workschedule_emails"

# Read once, at import, so a bad segredo.ini stops the service immediately
# rather than on the first request. Exit 2 with the reason rather than a
# traceback: this is the one failure an operator can actually fix, and it is
# what they will see in `journalctl -u vfz-workschedule-emails`.
try:
    CONFIG = Config.from_segredo()
except ConfigError as exc:
    print(f"Configuration error: {exc}", file=sys.stderr)
    raise SystemExit(2)

configure_logging(CONFIG)

SERVICE_BIND = CONFIG.bind
SERVICE_PORT = CONFIG.port

# In-flight guard. Two overlapping runs could both read an empty notification
# history and post the same comment twice -- the same reason the cron wrapper
# takes a lock. Refused rather than queued: a queued run would recompute the
# same answer the in-flight one is already writing.
_lock = threading.Lock()
_last_run: Optional[Dict[str, Any]] = None
_last_run_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# MODELS
# --------------------------------------------------------------------------- #


class TriggerRequest(BaseModel):
    """What to run. Every field is optional: the defaults are the cron's."""

    dry_run: bool = Field(
        default=False,
        alias="dryRun",
        description="Show what would be posted. Posts nothing, records nothing.",
    )
    force: bool = Field(
        default=False,
        description=(
            "Run even outside the notification window. The window exists to stop "
            "a mistimed run commenting at 03:00, so this is for a deliberate "
            "manual run only."
        ),
    )
    only_task_ids: List[str] = Field(
        default_factory=list,
        alias="onlyTaskIds",
        description="Restrict to these Wrike task ids. For contained live tests.",
    )
    title_contains: Optional[str] = Field(
        default=None,
        alias="titleContains",
        description="Restrict to tasks whose title contains this text.",
    )
    triggered_by: str = Field(
        default="api",
        alias="triggeredBy",
        max_length=64,
        description=(
            "Who asked for this run, e.g. 'dashboard'. Recorded on the result so "
            "a manual run is distinguishable from the cron in SolarWinds."
        ),
    )

    model_config = {"populate_by_name": True}


class TriggerResponse(BaseModel):
    """Acknowledgement that a run was started. Not its result."""

    status: str = Field(description="'accepted' or 'ignored'.")
    message: str
    accepted: bool
    running: bool
    dry_run: bool = Field(default=False, alias="dryRun")

    model_config = {"populate_by_name": True, "ser_json_by_alias": True}


# --------------------------------------------------------------------------- #
# APP
# --------------------------------------------------------------------------- #

app = FastAPI(
    title="VFZ Workschedule Emails (off-day approver notifier)",
    description=(
        "Flags non-working approvers on pending Wrike approvals by commenting on "
        "the task, tagging whoever can act. Normally hourly on cron; these "
        "endpoints are the VFZ dashboard's manual trigger and read-only views."
    ),
    version="1.1.0",
)


def _config(dry_run: Optional[bool] = None) -> Config:
    """Re-read config so an edited segredo.ini takes effect without a restart."""
    config = Config.from_segredo()
    if dry_run is not None:
        config = Config(**{**config.__dict__, "dry_run": dry_run})
    configure_logging(config)
    return config


def _config_or_503(dry_run: Optional[bool] = None) -> Config:
    try:
        return _config(dry_run)
    except ConfigError as exc:
        # A misconfigured service must say so plainly rather than 500 with a
        # traceback: this is the one failure an operator can actually fix.
        # LOG_MODE is read straight from the environment: Config is exactly what
        # could not be built, but a "do_nothing" set for a test must still hold.
        general_helpers.log_error(
            general_helpers.make_stamp(mode=os.environ.get("LOG_MODE", "").strip()),
            "Configuration error",
            exc,
            {"stage": "request"},
        )
        raise HTTPException(status_code=503, detail=f"Configuration error: {exc}") from exc


def last_run() -> Optional[Dict[str, Any]]:
    with _last_run_lock:
        return dict(_last_run) if _last_run else None


def is_running() -> bool:
    return _lock.locked()


# --------------------------------------------------------------------------- #
# WORKER -- what the background task actually does
# --------------------------------------------------------------------------- #


def worker(
    dry_run: bool,
    force: bool,
    only_task_ids: Optional[set],
    title_contains: Optional[str],
    triggered_by: str,
    log_mode: str = "",
) -> None:
    """Run one notifier pass and record the outcome. Never raises.

    An exception escaping a background task is logged by Starlette and lost, so
    everything is caught here and turned into a recorded result the dashboard
    can show.
    """
    if not _lock.acquire(blocking=False):
        return

    started_at = datetime.now(ZoneInfo(CONFIG.timezone)).isoformat()
    try:
        summary = run_from_env(
            force_window=force,
            dry_run=True if dry_run else None,
            only_task_ids=only_task_ids,
            title_contains=title_contains,
        )
        result = {
            "status": "attention"
            if (summary.failures or summary.upstream_late)
            else "success",
            "triggeredBy": triggered_by,
            "startedAt": started_at,
            **summary.as_dict(),
        }
    except ConfigError as exc:
        result = _failure("configuration error", exc, started_at, triggered_by, log_mode)
    except WrikeError as exc:
        result = _failure("Wrike API error", exc, started_at, triggered_by, log_mode)
    except DatabaseError as exc:
        result = _failure("MySQL error", exc, started_at, triggered_by, log_mode)
    except Exception as exc:  # must stay last
        result = _failure("unexpected error", exc, started_at, triggered_by, log_mode)
    finally:
        _lock.release()

    global _last_run
    with _last_run_lock:
        _last_run = result


def _failure(
    label: str,
    exc: BaseException,
    started_at: str,
    triggered_by: str,
    log_mode: str = "",
) -> Dict[str, Any]:
    general_helpers.log_error(
        general_helpers.make_stamp(mode=log_mode),
        f"Off-day notifier run failed: {label}",
        exc,
        {"triggeredBy": triggered_by, "startedAt": started_at},
    )
    return {
        "status": "failed",
        "triggeredBy": triggered_by,
        "startedAt": started_at,
        "message": f"{label}: {exc}",
        "failures": 1,
    }


# --------------------------------------------------------------------------- #
# ROUTES
# --------------------------------------------------------------------------- #


@app.post(f"{EMAILS_PATH}/", response_model=TriggerResponse, tags=["notifier"])
def trigger(
    background_tasks: BackgroundTasks, payload: Optional[TriggerRequest] = None
) -> TriggerResponse:
    """Kick off a notifier run and return immediately.

    A run walks every pending approval for today's non-working users and posts a
    comment per approval that needs one, so it takes long enough that holding
    the HTTP connection open would time out behind Nginx. The caller polls
    ``GET {EMAILS_PATH}/status``, which is what the dashboard does.
    """
    request = payload or TriggerRequest()
    config = _config_or_503(request.dry_run)
    stamp = general_helpers.make_stamp(mode=config.log_mode)

    if is_running():
        general_helpers.log_message(
            stamp,
            "Notifier trigger ignored: a run is already in progress",
            {"triggeredBy": request.triggered_by},
        )
        return TriggerResponse(
            status="ignored",
            message="A run is already in progress. Poll the status endpoint.",
            accepted=False,
            running=True,
        )

    background_tasks.add_task(
        worker,
        request.dry_run,
        request.force,
        set(request.only_task_ids) if request.only_task_ids else None,
        request.title_contains,
        request.triggered_by,
        config.log_mode,
    )

    general_helpers.log_message(
        stamp,
        f"Notifier run accepted (triggered by {request.triggered_by})",
        {
            "triggeredBy": request.triggered_by,
            "dryRun": request.dry_run,
            "force": request.force,
            "onlyTaskIds": sorted(request.only_task_ids),
            "titleContains": request.title_contains or "",
        },
    )

    return TriggerResponse(
        status="accepted",
        message=(
            "Run started"
            + (" (dry run, nothing will be posted)" if request.dry_run else "")
            + "."
            + (
                ""
                if request.force
                else f" Outside {config.notify_start_hour:02d}:00-"
                f"{config.notify_end_hour:02d}:00 {config.timezone} it will stop "
                f"early; use force to override."
            )
            + " Poll the status endpoint for the result."
        ),
        accepted=True,
        running=True,
        dry_run=request.dry_run,
    )


@app.get(f"{EMAILS_PATH}/status", tags=["notifier"])
def status() -> Dict[str, Any]:
    """What the last run did, and the window and scope it runs under.

    ``lastRun`` is in-memory, so null means "not since this process started",
    not "never" -- the hourly cron runs in its own process and does not appear
    here. SolarWinds holds the durable history for both.
    """
    config = _config_or_503()
    now = datetime.now(ZoneInfo(config.timezone))
    return {
        "running": is_running(),
        "lastRun": last_run(),
        "now": now.isoformat(),
        "today": now.date().isoformat(),
        "timezone": config.timezone,
        "window": f"{config.notify_start_hour:02d}:00-"
        f"{config.notify_end_hour:02d}:00",
        "withinWindow": config.notify_start_hour <= now.hour < config.notify_end_hour,
        "dryRunDefault": config.dry_run,
        "spaceId": config.space_id,
        "fallbackContactId": config.fallback_contact_id,
        "projectLeadFieldConfigured": bool(config.project_lead_field_id),
        "upstreamGraceMinutes": config.upstream_grace_minutes,
        "database": config.mysql_database,
        "nonWorkingTable": config.non_working_table,
        "notificationsTable": config.notifications_table,
    }


@app.get(f"{EMAILS_PATH}/data", tags=["notifier"])
def data(
    on_date: Optional[str] = Query(default=None, alias="date"),
    history_limit: int = Query(default=25, ge=1, le=200, alias="historyLimit"),
) -> Dict[str, Any]:
    """Today's non-working users and the most recent notifications.

    Read-only, and the two halves together are what makes the dashboard page
    diagnostic rather than decorative: an empty ``nonWorkingUsers`` with a late
    upstream job is the difference between "quiet day" and "nothing ran".
    """
    config = _config_or_503()
    stamp = general_helpers.make_stamp(mode=config.log_mode)
    now = datetime.now(ZoneInfo(config.timezone))
    date_iso = on_date or now.date().isoformat()

    store = build_store(config)
    try:
        store.ensure_schema()
        non_working = store.non_working_rows_for(date_iso)
        history = store.notification_records(limit=history_limit)
        baseline_count = store.baseline_count()
        _attach_permalinks(config, history, stamp)
    except DatabaseError as exc:
        general_helpers.log_error(
            stamp,
            "Notifier data lookup failed: MySQL unavailable",
            exc,
            {"date": date_iso},
        )
        raise HTTPException(
            status_code=503,
            detail="Work-schedule data is temporarily unavailable. Retry the request.",
        ) from exc
    finally:
        store.close()

    upstream_late = (
        not non_working
        and now.hour >= config.notify_start_hour
        and (
            now.hour > config.notify_start_hour
            or now.minute >= config.upstream_grace_minutes
        )
    )

    general_helpers.log_message(
        stamp,
        f"Notifier data lookup complete: {len(non_working)} user(s) off on "
        f"{date_iso}, {len(history)} notification record(s) returned",
        {
            "date": date_iso,
            "nonWorkingCount": len(non_working),
            "historyCount": len(history),
            "upstreamLate": upstream_late,
        },
    )

    return {
        "date": date_iso,
        "timezone": config.timezone,
        "nonWorkingUsers": non_working,
        "nonWorkingCount": len(non_working),
        # True when the upstream work-schedule job has had its grace period and
        # written nothing. Posting nothing then looks identical to a quiet day,
        # which is exactly why it is called out.
        "upstreamLate": upstream_late,
        "notifications": history,
        "notificationCount": len(history),
        "baselinedApprovals": baseline_count,
    }


def _attach_permalinks(config: Config, history, stamp) -> None:
    """Add a Wrike ``permalink`` to each notification record, in place.

    The permalink is not stored: it belongs to the task, not the notification,
    and duplicating it would mean a renamed or moved task carried a stale URL
    forever. So it is looked up on demand -- one batched /tasks call for the
    whole page, through the shared client's retry.

    Best effort by design. This endpoint's job is to report what is in the
    database; if Wrike is unreachable the ids still render, just not as links.
    Failing the page because a convenience link could not be resolved would be
    the wrong trade.
    """
    l_task_ids = sorted({row.get("taskId") for row in history if row.get("taskId")})
    if not l_task_ids:
        return

    try:
        client = build_client(config)
        d_permalinks = {
            task["id"]: task.get("permalink", "")
            for task in client.get_tasks(l_task_ids)
        }
    except Exception as exc:
        general_helpers.log_error(
            stamp,
            "Could not resolve Wrike permalinks for the notification history; "
            "task ids will render without links",
            exc,
            {"taskIds": l_task_ids},
        )
        return

    for row in history:
        row["permalink"] = d_permalinks.get(row.get("taskId"), "")


# --------------------------------------------------------------------------- #
# HEARTBEAT
# --------------------------------------------------------------------------- #


@app.get("/hello", tags=["ops"])
def hello() -> Dict[str, Any]:
    """Cheapest possible "is the process up" check. Touches no dependency."""
    return {
        "status": "alive",
        "service": CONFIG.log_keyword,
        "triggerPath": f"{EMAILS_PATH}/",
        "port": SERVICE_PORT,
    }


# --------------------------------------------------------------------------- #
# ENTRY POINT
# --------------------------------------------------------------------------- #


def main() -> None:
    # Bound to loopback by default: the dashboard is the only caller and it
    # runs on the same host, so there is nothing to gain from a public
    # interface and nothing to authenticate. Widen [emails] bind in
    # segredo.ini only alongside auth in front of it.
    uvicorn.run(app, host=SERVICE_BIND, port=SERVICE_PORT)


if __name__ == "__main__":
    main()

    # TO RUN (from the vfz_workschedule_emails/ directory):
    #   DEV:  python vfz_workschedule_emails_main.py
    #   PROD: managed by systemd -- see deploy/ and ../DEPLOYMENT.md
    # The hourly cron (scripts/run-notifier.sh -> run_notifier.py) stays as it
    # is: this service is the manual trigger, not a replacement for it.
