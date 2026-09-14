# =============================================================================
# vfz_workschedule_availability/vfz_workschedule_availability_main.py
# Integration 3: Workschedule Availability API
#
# Answers "who is not working today" for Workato, which calls it every time
# someone @tags a colleague in a Wrike comment -- so it is called far more often
# than the other two combined.
#
# API:     GET /vfz_workschedule_availability/          who is off today
#          GET /vfz_workschedule_availability/status    cache and DB health
#          GET /hello                                   heartbeat
# Trigger: Workato, on every @tag
# Port:    5009
#
# Two things make this different from the other two integrations:
#
#   It is called from OUTSIDE the host, so it binds publicly and REQUIRES a
#   bearer token. The other two bind to loopback and have no auth.
#
#   It caches. The table is rewritten once a day by the DB Resync, so nearly
#   every request is answered from memory without touching MySQL. That is the
#   whole reason this exists instead of pointing Workato at the database: a
#   direct connection cannot be cached, so every @tag would be a round trip.
# =============================================================================

import hmac
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# ---------------------------------------------------------------------------
# Add shared helpers to the module search path
# ---------------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import general_helpers
import non_working_days
from database_helpers import DatabaseConnection, DatabaseError

from cache import NonWorkingCache
from config import Config, ConfigError

# ---------------------------------------------------------------------------
# Configuration — load from segredo.ini at startup
# ---------------------------------------------------------------------------

try:
    CONFIG = Config.from_segredo()
except ConfigError as exc:
    print(f"Configuration error: {exc}", file=sys.stderr)
    raise SystemExit(2)

general_helpers.papertrail_url = CONFIG.papertrail_url
general_helpers.papertrail_token = CONFIG.papertrail_token
general_helpers.system_identifier = CONFIG.log_keyword
general_helpers.log_mode = CONFIG.log_mode

AVAILABILITY_PATH = "/vfz_workschedule_availability"

# A database failure must never be reported as "nobody is off": that would
# suppress a warning Workato was asking for.
_UNAVAILABLE = "Work-schedule data is temporarily unavailable. Retry the request."

_database = DatabaseConnection(
    host=CONFIG.mysql_host,
    database=CONFIG.mysql_database,
    user=CONFIG.mysql_user,
    password=CONFIG.mysql_password,
    port=CONFIG.mysql_port,
    timeout_seconds=CONFIG.mysql_timeout_seconds,
    pool_size=CONFIG.mysql_pool_size,
    pool_name="availability",
    zone=CONFIG.timezone,
)


def _load(on_date) -> List[Dict[str, Any]]:
    """Read one day's rows. The cache's only route to MySQL."""
    return non_working_days.rows_for_date(
        _database, CONFIG.non_working_table, on_date
    )


CACHE = NonWorkingCache(_load, CONFIG.cache_ttl_seconds)

# Requests served since the last rolled-up log line. Logging every request
# would mean a POST to SolarWinds per @tag, which would cost more than the
# request it describes.
_served = 0


def today() -> Any:
    """Today's date in the configured timezone.

    The date matters: at 01:00 in Johannesburg it is still the previous day in
    UTC, and Workato would be told about the wrong day.
    """
    return datetime.now(ZoneInfo(CONFIG.timezone)).date()


# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, ser_json_by_alias=True
    )


class NonWorkingUser(CamelModel):
    user_id: str
    user: Optional[str] = None
    work_schedule_title: Optional[str] = None
    work_schedule_id: Optional[str] = None


class AvailabilityResponse(CamelModel):
    """Who is not working today. The contract Workato depends on."""

    date: str = Field(description="The date these users are not working (YYYY-MM-DD).")
    timezone: str
    count: int
    user_ids: List[str] = Field(
        description="Just the Wrike user IDs, for a quick 'is this person in the list' check."
    )
    users: List[NonWorkingUser] = Field(
        description="The full rows, when the display name or schedule is wanted."
    )
    cached: bool = Field(
        description="True when answered from memory without reading MySQL."
    )
    stale: bool = Field(
        default=False,
        description=(
            "True when MySQL could not be read and this is the last known good "
            "answer for today. Treat the list as usable but slightly old."
        ),
    )


# ---------------------------------------------------------------------------
# SECURITY
# ---------------------------------------------------------------------------


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """
    Validate ``Authorization: Bearer <token>``.

    Unlike the other two integrations there is no unauthenticated mode here:
    this one is reachable from the internet, and the config refuses to start
    without a token, so there is no path where this check is skipped.
    """
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Constant-time, and compared as bytes: str-mode compare_digest raises on
    # non-ASCII, which would turn a malformed header into a 500 instead of a 401.
    if not hmac.compare_digest(
        presented.strip().encode("utf-8"), CONFIG.api_token.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    oStamp = general_helpers.make_stamp(mode=CONFIG.log_mode)

    # Warm the pool and the cache before accepting traffic.
    #
    # MySQLConnectionPool opens all pool_size connections when it is first
    # used, so without this the FIRST caller paid every handshake -- measured
    # at nine seconds against RDS. Workato's first @tag after a restart should
    # not be the request that absorbs that.
    #
    # Best effort: a database that is down must not stop the service starting,
    # because /status is how you find out that it is down.
    warm = {"pool": False, "rows": None, "error": ""}
    try:
        _database.ping()
        warm["pool"] = True
        warm["rows"] = len(CACHE.get(today())["rows"])
    except Exception as exc:
        warm["error"] = str(exc)
        general_helpers.log_error(
            oStamp,
            "Could not warm the connection pool at startup; the first request "
            "will pay for it and /status will report the problem",
            exc,
            {"host": CONFIG.mysql_host, "database": CONFIG.mysql_database},
        )

    general_helpers.log_message(
        oStamp,
        "Workschedule Availability API starting",
        {
            "database": CONFIG.mysql_database,
            "table": CONFIG.non_working_table,
            "host": CONFIG.mysql_host,
            "timezone": CONFIG.timezone,
            "bind": f"{CONFIG.bind}:{CONFIG.port}",
            "cacheTtlSeconds": CONFIG.cache_ttl_seconds,
            "logEveryRequests": CONFIG.log_every_requests,
            "poolWarmed": warm["pool"],
            "poolSize": CONFIG.mysql_pool_size,
            "preloadedRows": warm["rows"],
            "warmupError": warm["error"],
        },
    )
    yield
    general_helpers.log_message(
        oStamp,
        "Workschedule Availability API shutting down",
        {"served": _served, **CACHE.stats()},
    )
    _database.close()


app = FastAPI(
    title="VFZ Workschedule Availability API",
    description=(
        "Returns which Wrike users are not working today, for Workato. "
        "Read-only: it never writes to the database and never touches Wrike."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get(
    f"{AVAILABILITY_PATH}/",
    response_model=AvailabilityResponse,
    dependencies=[Depends(require_token)],
    tags=["availability"],
)
def non_working_today(response: Response) -> AvailabilityResponse:
    """
    Everyone not working today.

    Answered from memory unless the cached list has expired. A 503 means the
    database could not be read AND there was no earlier answer to fall back on
    — never an empty list, because "nobody is off" would suppress the very
    warning Workato is asking about.
    """
    global _served
    on_date = today()

    try:
        result = CACHE.get(on_date)
    except Exception as exc:
        general_helpers.log_error(
            general_helpers.make_stamp(mode=CONFIG.log_mode),
            "Availability lookup failed and no cached answer was available",
            exc,
            {"date": on_date.isoformat(), "table": CONFIG.non_working_table},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_UNAVAILABLE
        ) from exc

    rows = result["rows"]
    _served += 1

    # Rolled up rather than per request: one log line per N calls keeps the
    # collector useful without it costing more than the work being logged.
    if CONFIG.log_every_requests and _served % CONFIG.log_every_requests == 0:
        general_helpers.log_message(
            general_helpers.make_stamp(mode=CONFIG.log_mode),
            f"Availability API served {_served} request(s)",
            {"date": on_date.isoformat(), "served": _served, **CACHE.stats()},
        )

    # Let Workato and any proxy cache for the remainder of the TTL, so a burst
    # of @tags need not even reach this process.
    if CONFIG.cache_ttl_seconds:
        response.headers["Cache-Control"] = f"private, max-age={CONFIG.cache_ttl_seconds}"

    return AvailabilityResponse(
        date=on_date.isoformat(),
        timezone=CONFIG.timezone,
        count=len(rows),
        user_ids=[row["userId"] for row in rows if row.get("userId")],
        users=[
            NonWorkingUser(
                user_id=row.get("userId", ""),
                user=row.get("user"),
                work_schedule_title=row.get("workScheduleTitle"),
                work_schedule_id=row.get("workScheduleId"),
            )
            for row in rows
        ],
        cached=result["cached"],
        stale=result["stale"],
    )


@app.get(
    f"{AVAILABILITY_PATH}/status",
    dependencies=[Depends(require_token)],
    tags=["ops"],
)
def availability_status(response: Response) -> Dict[str, Any]:
    """
    Cache statistics and a real database round trip.

    Returns 503 when the database is unreachable, so a health check keyed on
    the status code notices. The hit rate is the number to watch: if it is low,
    the TTL is too short for how often Workato calls.
    """
    database_reachable = True
    try:
        _database.ping()
    except DatabaseError:
        database_reachable = False
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if database_reachable else "degraded",
        "databaseReachable": database_reachable,
        "database": CONFIG.mysql_database,
        "table": CONFIG.non_working_table,
        "timezone": CONFIG.timezone,
        "today": today().isoformat(),
        "served": _served,
        "cache": CACHE.stats(),
    }


@app.post(
    f"{AVAILABILITY_PATH}/invalidate",
    dependencies=[Depends(require_token)],
    tags=["ops"],
)
def invalidate() -> Dict[str, Any]:
    """
    Drop the cached list so the next request re-reads MySQL.

    For the case where a resync has just run and the answer is wanted
    immediately rather than after the TTL.
    """
    CACHE.invalidate()
    return {"status": "invalidated", "cache": CACHE.stats()}


# ---------------------------------------------------------------------------
# HEARTBEAT
# ---------------------------------------------------------------------------


@app.get("/hello", tags=["ops"])
def hello() -> Dict[str, Any]:
    """Cheapest possible "is the process up" check. Touches no dependency.

    Deliberately unauthenticated, like the other two: it exposes nothing beyond
    the fact that the process is running.
    """
    return {
        "status": "alive",
        "service": CONFIG.log_keyword,
        "path": f"{AVAILABILITY_PATH}/",
        "port": CONFIG.port,
    }


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------


def main() -> None:
    uvicorn.run(app, host=CONFIG.bind, port=CONFIG.port)


if __name__ == "__main__":
    main()

    # TO RUN (from the vfz_workschedule_availability/ directory):
    #   DEV:  python vfz_workschedule_availability_main.py
    #   PROD: managed by systemd -- see deploy/ and ../DEPLOYMENT.md
