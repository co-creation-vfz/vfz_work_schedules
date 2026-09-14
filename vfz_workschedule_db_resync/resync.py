# =============================================================================
# resync.py
# One resync run: read the weekday patterns from Wrike, write the dates to MySQL.
#
# This is the whole job the nightly cron does, lifted out of the CLI so the
# dashboard's manual trigger and the cron run the SAME code. Two copies of this
# would drift, and the way you would find out is a manual resync quietly
# behaving differently from the scheduled one.
#
# run_resync() never raises: it catches everything, logs it, and returns a
# result dict. The CLI turns that into an exit code; the HTTP route returns it
# as JSON.
# =============================================================================

import os
import sys
import threading
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import general_helpers
from database_helpers import DatabaseConnection, DatabaseError
from wrike_helpers import WrikeClient

from config import Config
from populator import NonWorkingDayPopulator
from repository import WorkScheduleRepository
from sources import JsonScheduleSource, WrikeScheduleSource

# Exit codes. Each one is a distinct thing a scheduled run should be noticed
# for, rather than one catch-all failure.
EXIT_OK = 0
EXIT_NOTHING_TO_DO = 1
EXIT_BAD_ARGUMENTS = 2
EXIT_UNREADABLE_SCHEDULE = 3
EXIT_BLAST_RADIUS = 4
EXIT_DATABASE = 5
EXIT_SOURCE_FAILED = 6

# In-flight guard. Two overlapping resyncs would both expand the same window
# and race on the same rows; the second is refused rather than queued, because
# by the time it ran the first would already have written the same answer.
_lock = threading.Lock()

# The last run THIS process did. Kept as a fallback for when the run log cannot
# be read (MySQL down), not as the answer: a cron run happens in its own
# process, so anything that only lives here is invisible to the dashboard.
_last_run: Optional[Dict[str, Any]] = None
_last_run_lock = threading.Lock()


def last_run(
    repository: Optional[WorkScheduleRepository] = None,
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The most recent resync result, from the run log when one is reachable.

    Pass ``repository`` and the answer comes from the ``work_schedule_resync_runs``
    table, which every run writes to -- so the nightly cron, a dashboard click
    and a CLI invocation all show up, in whichever process asks. Without one,
    or when the table cannot be read, this falls back to the run this process
    did, which is what it always used to return on its own.

    :param status: Restrict to runs with this status, e.g. "success" for "when
                   was the table last actually written".
    """
    if repository is not None:
        try:
            recorded = repository.latest_run(status)
            if recorded:
                return recorded
        except Exception as exc:
            # Not fatal: the dashboard would rather show this process's own
            # last run than nothing, and the status route reports the database
            # as unreachable separately.
            general_helpers.log_error(
                general_helpers.make_stamp(),
                "Could not read the resync run log; falling back to this "
                "process's own last run",
                exc,
            )

    with _last_run_lock:
        if not _last_run:
            return None
        if status and _last_run.get("status") != status:
            return None
        return dict(_last_run)


def is_running() -> bool:
    return _lock.locked()


# ---------------------------------------------------------------------------
# Wiring — the one place a connection or a Wrike client is opened
# ---------------------------------------------------------------------------


def build_repository(
    config: Config, pool_name: str = "db_resync"
) -> WorkScheduleRepository:
    """Open the pool a resync needs. Pool of one: a resync is single-threaded."""
    database = DatabaseConnection(
        host=config.mysql_host,
        database=config.mysql_database,
        user=config.mysql_user,
        password=config.mysql_password,
        port=config.mysql_port,
        timeout_seconds=config.mysql_timeout_seconds,
        pool_size=1,
        pool_name=pool_name,
        zone=config.timezone,
    )
    return WorkScheduleRepository(
        database, config.non_working_table, config.runs_table
    )


def build_source(config: Config, source: str = "wrike", file: Optional[str] = None):
    """Build the schedule source. `json` reads a local file, for testing."""
    if source == "wrike":
        return WrikeScheduleSource(
            WrikeClient(
                token=config.wrike_token,
                base_url=config.wrike_base_url,
                timeout=config.wrike_request_timeout,
            )
        )
    return JsonScheduleSource(file or config.schedules_file)


def resolve_window(
    config: Config,
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    today_only: bool = False,
) -> tuple:
    """
    Work out the window to materialise. Returns ``(date_from, date_to)``.

    ``horizon_days`` counts today, so 1 gives a single-day window — which is
    what a daily cron needs.
    """
    start = date_from or today_in(config.timezone)
    if today_only:
        return start, start
    window = max(1, config.horizon_days)
    return start, (date_to or start + timedelta(days=window - 1))


def today_in(timezone: str) -> Date:
    """
    Today's date in the given timezone.

    The date matters: at 01:00 in Johannesburg it is still the previous day in
    UTC, and a person would otherwise be checked against the wrong day.
    """
    return datetime.now(ZoneInfo(timezone)).date()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_resync(
    config: Config,
    source: str = "wrike",
    file: Optional[str] = None,
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    today_only: bool = False,
    dry_run: bool = False,
    purge_orphans: bool = False,
    full_refresh: bool = False,
    min_schedule_size_for_guard: Optional[int] = None,
    max_non_working_fraction: Optional[float] = None,
    triggered_by: str = "cron",
) -> Dict[str, Any]:
    """
    Run one resync end to end. Never raises.

    :param triggered_by: Who asked for this run. Carried into every log line,
                         so a manual resync from the dashboard is
                         distinguishable in SolarWinds from the scheduled one —
                         the first question asked when a day looks wrong.
    :returns:            Result dict, also recorded as last_run().
    """
    oStamp = general_helpers.make_stamp(mode=config.log_mode)
    started_at = datetime.now(ZoneInfo(config.timezone))
    window_from, window_to = resolve_window(config, date_from, date_to, today_only)

    guard_size = (
        config.min_schedule_size_for_guard
        if min_schedule_size_for_guard is None
        else min_schedule_size_for_guard
    )
    guard_fraction = (
        config.max_non_working_fraction
        if max_non_working_fraction is None
        else max_non_working_fraction
    )

    d_context = {
        "triggeredBy": triggered_by,
        "source": source,
        "dateFrom": window_from.isoformat(),
        "dateTo": window_to.isoformat(),
        "dryRun": dry_run,
        "purgeOrphans": purge_orphans,
        "fullRefresh": full_refresh,
        "database": config.mysql_database,
        "table": config.non_working_table,
    }

    if window_to < window_from:
        return _finish(
            oStamp,
            started_at,
            config,
            EXIT_BAD_ARGUMENTS,
            "dateTo must not be earlier than dateFrom",
            d_context,
        )

    if not _lock.acquire(blocking=False):
        # Not an error: the answer this run would write is already being
        # written. Reported so a dashboard click says so rather than looking
        # like it did nothing.
        return _finish(
            oStamp,
            started_at,
            config,
            EXIT_OK,
            "a resync is already in progress; this trigger was ignored",
            d_context,
            status="ignored",
        )

    try:
        general_helpers.log_message(
            oStamp,
            f"Work-schedule resync starting for {window_from} to {window_to} "
            f"(triggered by {triggered_by})",
            {
                **d_context,
                "minScheduleSizeForGuard": guard_size,
                "maxNonWorkingFraction": guard_fraction,
            },
        )

        try:
            schedule_source = build_source(config, source, file)
            l_assignments = schedule_source.assignments()
        except Exception as exc:
            general_helpers.log_error(
                oStamp, "Resync could not read the schedule source", exc, d_context
            )
            return _finish(
                oStamp,
                started_at,
                config,
                EXIT_SOURCE_FAILED,
                f"could not read the {source} schedule source: {exc}",
                d_context,
            )

        l_problems = list(getattr(schedule_source, "problems", []))
        l_unresolved = list(getattr(schedule_source, "unresolved_user_ids", []))
        if l_problems:
            general_helpers.log_message(
                oStamp,
                f"Resync found {len(l_problems)} unreadable schedule(s)",
                {
                    **d_context,
                    "problems": l_problems,
                    "unresolvedUserIds": l_unresolved,
                },
            )

        if not l_assignments:
            return _finish(
                oStamp,
                started_at,
                config,
                EXIT_NOTHING_TO_DO,
                "no schedule assignments found, nothing to do",
                {**d_context, "problems": l_problems},
            )

        # Always resolved for --source wrike: a row with user_name NULL because
        # one flag was left off was a silent, easy-to-miss mistake.
        if isinstance(schedule_source, WrikeScheduleSource):
            try:
                d_names = schedule_source.resolve_user_names(
                    [a.user_id for a in l_assignments]
                )
            except Exception as exc:
                general_helpers.log_error(
                    oStamp,
                    "Resync could not resolve display names; continuing with "
                    "the names already on the assignments",
                    exc,
                    d_context,
                )
                d_names = {}
            for assignment in l_assignments:
                assignment.user = d_names.get(assignment.user_id, assignment.user)

        repository = build_repository(config)
        try:
            repository.ensure_schema()
            result = NonWorkingDayPopulator(repository).populate(
                l_assignments,
                window_from,
                window_to,
                dry_run=dry_run,
                purge_orphans=purge_orphans,
                full_refresh=full_refresh,
                unresolved_user_ids=l_unresolved,
                min_schedule_size_for_guard=guard_size,
                max_non_working_fraction=guard_fraction,
            )
            n_rows = repository.count_rows()
        except DatabaseError as exc:
            general_helpers.log_error(
                oStamp,
                "Resync failed: MySQL unavailable, no dates were written",
                exc,
                {**d_context, "host": config.mysql_host},
            )
            return _finish(
                oStamp,
                started_at,
                config,
                EXIT_DATABASE,
                f"MySQL unavailable, no dates were written: {exc}",
                d_context,
            )
        except Exception as exc:
            general_helpers.log_error(
                oStamp, "Resync failed unexpectedly", exc, d_context
            )
            return _finish(
                oStamp,
                started_at,
                config,
                EXIT_DATABASE,
                f"unexpected failure: {exc}",
                d_context,
            )
        finally:
            repository.close()

        # A read failure is treated as more urgent than a suspicious-but-
        # successful read, so it wins when both happened.
        if l_problems:
            exit_code = EXIT_UNREADABLE_SCHEDULE
            note = f"{len(l_problems)} schedule(s) could not be read"
        elif result.blast_radius_flagged:
            exit_code = EXIT_BLAST_RADIUS
            note = (
                f"{len(result.blast_radius_flagged)} schedule(s) left unwritten "
                f"by the blast-radius guard; confirm in Wrike, then re-run"
            )
        else:
            exit_code = EXIT_OK
            note = (
                f"replaced the table: {result.inserted} inserted, "
                f"{result.removed} pre-existing row(s) removed"
                if result.full_refresh
                else f"{result.inserted} inserted, {result.matched} already "
                f"present, {result.removed} removed as stale"
            )

        return _finish(
            oStamp,
            started_at,
            config,
            exit_code,
            note,
            {
                **d_context,
                **result.as_details(),
                "problems": l_problems,
                "tableRowCount": n_rows,
            },
            summary_text=result.summary(),
        )
    finally:
        _lock.release()


def _finish(
    oStamp: Dict[str, Any],
    started_at: datetime,
    config: Config,
    exit_code: int,
    note: str,
    details: Dict[str, Any],
    status: Optional[str] = None,
    summary_text: str = "",
) -> Dict[str, Any]:
    """Log the closing line and record the result. The one exit point."""
    finished_at = datetime.now(ZoneInfo(config.timezone))
    if status is None:
        status = "success" if exit_code == EXIT_OK else "attention"

    d_result = {
        "status": status,
        "exitCode": exit_code,
        "message": note,
        "startedAt": started_at.isoformat(),
        "finishedAt": finished_at.isoformat(),
        "durationSeconds": round((finished_at - started_at).total_seconds(), 3),
        "summaryText": summary_text,
        **details,
    }

    general_helpers.log_message(
        oStamp,
        f"Work-schedule resync finished with exit code {exit_code}: {note}",
        d_result,
    )

    global _last_run
    with _last_run_lock:
        _last_run = d_result

    # "ignored" means a concurrent run was already doing the work and this one
    # did nothing; logging it as a run would make the dashboard's "last sync"
    # jump for something that never touched the table.
    if status != "ignored":
        _record_run(oStamp, config, d_result)

    return d_result


def _record_run(
    oStamp: Dict[str, Any], config: Config, d_result: Dict[str, Any]
) -> None:
    """Append the finished run to the run log. Best effort, never raises.

    Its own short-lived connection, because the run's repository is closed by
    the time a run finishes and a failure here must not be able to change the
    outcome of a run that otherwise worked. One connect per run, once a night.
    """
    repository = None
    try:
        repository = build_repository(config, pool_name="db_resync_runlog")
        repository.ensure_runs_schema()
        repository.record_run(d_result)
    except Exception as exc:
        # Worth saying out loud: from here on the dashboard's "last sync" is
        # stale, and that is exactly the kind of silence this change exists to
        # remove.
        general_helpers.log_error(
            oStamp,
            "Resync finished but its run could not be written to the run log; "
            "the dashboard's last sync will be out of date",
            exc,
            {"runsTable": getattr(config, "runs_table", "")},
        )
    finally:
        if repository is not None:
            try:
                repository.close()
            except Exception:
                pass


def run_resync_in_background(config: Config, **kwargs) -> None:
    """
    Wrapper for FastAPI's BackgroundTasks: swallow, never propagate.

    An exception escaping a background task is logged by Starlette and lost.
    run_resync already catches everything; this is the second line so a bug in
    the recording itself cannot take the worker thread down silently.
    """
    try:
        run_resync(config, **kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        general_helpers.log_error(
            general_helpers.make_stamp(mode=config.log_mode),
            "Background resync task crashed outside run_resync's own handling",
            exc,
        )
