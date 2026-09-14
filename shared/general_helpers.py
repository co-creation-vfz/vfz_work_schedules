# =============================================================================
# general_helpers.py
# Shared utility functions used across the VFZ work-schedule integrations.
# Covers: SolarWinds (Papertrail) logging, the run stamp, retry with backoff.
#
# IMPORTANT: no API calls and no database calls belong in this file, the
#            logging POST excepted. Wrike goes in wrike_helpers.py, MySQL in
#            database_helpers.py.
# =============================================================================

import json
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Module-level configuration (set by each integration's main file at startup,
# from segredo.ini -- the same pattern the process-simplification integrations
# use, so a reader moving between the two repos sees one convention)
# ---------------------------------------------------------------------------

# SolarWinds HTTP log endpoint, e.g.
# "https://logs.collector.eu-01.cloud.solarwinds.com/v1/logs"
papertrail_url: str = ""

# SolarWinds API token, sent as a bearer token
papertrail_token: str = ""

# Keyword identifying the integration in the logs. One per integration, stable
# forever: it is the search term that isolates this service's lines.
system_identifier: str = "vfz_workschedule"

# "" sends to SolarWinds, "print_only" prints locally and sends nothing (use it
# when developing against live data), "do_nothing" silences the logger (tests).
log_mode: str = ""

# Reuse one session so repeated log calls reuse the TCP connection.
_session = requests.Session()

# The body is one plain-text line, not a JSON document. That is the same shape
# the older dYdX integrations send to this collector, so it is known to ingest.
_HEADERS = {"content-type": "text/plain; charset=utf-8"}

# Display timezone for the stdout line only; the JSON timestamp is epoch-based.
_TZ = ZoneInfo("Africa/Johannesburg")

_TIMEOUT_SECONDS = 5

NO_TASK_ID = "no task_id found"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def make_stamp(task_id: str = "", mode: str = "") -> Dict[str, Any]:
    """
    Create a fresh stamp with the current timestamp, threaded through a run.

    Every function that might log takes this, which is why so many signatures
    start with it. `mode` falls back to the module-level `log_mode`, so an
    integration sets the mode once at startup rather than at every call site.

    :param task_id: Contextual Wrike task ID, when one is known.
    :param mode:    Override the module-level log mode for this stamp.
    :returns:       Stamp dict ready for log_message().
    """
    return {
        "time": int(time.time() * 1_000_000),
        "task_id": str(task_id) if task_id else NO_TASK_ID,
        "mode": mode or log_mode,
    }


def log_message(
    oStamp: Optional[Dict[str, Any]],
    message: str,
    details: Optional[Dict[str, Any]] = None,
    system_id: Optional[str] = None,
    _caller_depth: int = 2,
) -> None:
    """
    Emit one standard-shape log entry to SolarWinds. Never raises.

    One event is one line, in exactly this shape -- and nothing is ever added
    to it, because the rigidity is the point: every integration's lines read
    identically, so one saved search reads all of them.

        {keyword}: {task_id or timestamp} {message}: {details}

        vfz_workschedule_db_resync: 1756208400123456 Resync finished:
        {"caller": "run_resync", "inserted": 2}

    The second field is the task id when the run knows one, and the stamp's
    microsecond timestamp when it does not -- so an event is always traceable
    to something, and a Wrike task id is never buried behind a filler string.

    Everything that is not the message goes in `details`, at the end, as JSON.
    Never a top-level field of its own.

    Delivery happens on a background thread, so a slow or unreachable collector
    can never hold up a run. The thread is non-daemon: a short-lived cron run
    must not exit before its last line has been sent.

    :param oStamp:    Stamp from make_stamp().
    :param message:   Human-readable description, written for someone debugging
                      at 2am. "Wrike returned 422 on task update", not "error".
    :param details:   Raw metadata to attach -- counts, request/response bodies,
                      the row that caused the problem. Never a summary.
    :param system_id: Override the module-level system_identifier.
    """
    try:
        oStamp = oStamp or make_stamp()
        mode = oStamp.get("mode") or ""

        if mode == "do_nothing":
            return

        keyword = system_id or system_identifier
        task_id = oStamp.get("task_id") or NO_TASK_ID
        moment = datetime.now(_TZ).strftime("%H:%M:%S")

        # The same one-liner the process-simplification integrations print, so
        # a person tailing two services reads one shape from both.
        if not sys.is_finalizing():
            print(f"{keyword}: {moment}, id {task_id} : {message}")

        if mode == "print_only" or not is_configured():
            return

        # Microsecond epoch as an integer, so log tooling sorts and
        # range-filters it numerically rather than as text.
        timestamp = int(oStamp.get("time") or time.time() * 1_000_000)
        identifier = timestamp if task_id == NO_TASK_ID else task_id
        d_details = _with_caller(details, _caller_depth)

        s_line = (
            f"{keyword}: {identifier} {message}: "
            f"{json.dumps(d_details, default=str)}"
        )

        threading.Thread(
            target=_papertrail_thread,
            args=(s_line,),
            daemon=False,
            name="solarwinds-log",
        ).start()
    except Exception:
        # Logging is never allowed to be the thing that fails a run.
        pass


def log_error(
    oStamp: Optional[Dict[str, Any]],
    message: str,
    exc: Optional[BaseException] = None,
    details: Optional[Dict[str, Any]] = None,
    system_id: Optional[str] = None,
) -> None:
    """
    Log a failure, with the exception type and the line number it came from.

    The line number is what makes a 2am log line actionable, so it is captured
    here rather than left to every call site to remember.

    :param oStamp:  Stamp from make_stamp().
    :param message: What was being attempted when it failed.
    :param exc:     The caught exception.
    :param details: Any extra context -- the payload sent, the ids involved.
    """
    d_combined: Dict[str, Any] = dict(details or {})
    if exc is not None:
        d_combined["error_type"] = type(exc).__name__
        d_combined["error"] = str(exc)
        traceback = getattr(exc, "__traceback__", None)
        if traceback is not None:
            # The last frame is where it actually went wrong.
            while traceback.tb_next is not None:
                traceback = traceback.tb_next
            d_combined["line_number"] = traceback.tb_lineno
            d_combined["file"] = traceback.tb_frame.f_code.co_filename
    # One frame deeper: this sits between the real caller and log_message, so
    # without the bump every error would be attributed to log_error itself.
    log_message(oStamp, message, d_combined, system_id, _caller_depth=3)


def is_configured() -> bool:
    """True when a collector URL and token have both been set."""
    return bool(papertrail_url and papertrail_token)


def _with_caller(
    details: Optional[Dict[str, Any]], depth: int = 2
) -> Dict[str, Any]:
    """
    Add the calling function's name. Costs nothing at the call site.

    sys._getframe rather than inspect.stack(): the latter builds and resolves
    the whole stack, including reading source files, which is far too expensive
    for something on a request path. This needs one frame's name.

    `depth` counts out from here: 0 is this function, 1 is log_message, 2 is
    log_message's caller. log_error passes 3, because it sits in between.
    """
    d_extra = dict(details) if isinstance(details, dict) else {}
    try:
        d_extra.setdefault("caller", sys._getframe(depth).f_code.co_name)
    except Exception:
        pass
    return d_extra


def _papertrail_thread(s_line: str) -> None:
    """POST one log line. Runs on its own thread; never raises into the caller."""
    try:
        response = _session.post(
            papertrail_url,
            headers={**_HEADERS, "Authorization": f"Bearer {papertrail_token}"},
            data=s_line.encode("utf-8"),
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        if not sys.is_finalizing():
            print(f"SolarWinds log delivery failed: {exc}", file=sys.stderr)
    except Exception as exc:  # must stay the last handler
        if not sys.is_finalizing():
            print(f"SolarWinds logger error: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Retry / backoff helper
# ---------------------------------------------------------------------------


def retry_with_backoff(
    func: Callable,
    *args,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    **kwargs,
):
    """
    Call `func(*args, **kwargs)` up to `max_retries` times with exponential
    backoff. The function is expected to return a tuple whose last element is
    an HTTP status code; retries are triggered when it is not 200.

    :param func:          Callable to retry.
    :param max_retries:   Maximum number of attempts (default 3).
    :param initial_delay: Starting delay in seconds; doubles after each failure.
    :returns:             The last return value of `func`, regardless of success.
    """
    delay = initial_delay
    result = None

    for attempt in range(1, max_retries + 1):
        result = func(*args, **kwargs)
        status_code = result[-1] if isinstance(result, tuple) else None

        if status_code == 200:
            return result

        if attempt < max_retries:
            print(
                f"[retry_with_backoff] Attempt {attempt}/{max_retries} failed "
                f"(status={status_code}). Retrying in {delay}s..."
            )
            time.sleep(delay)
            delay *= 2

    return result
