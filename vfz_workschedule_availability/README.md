# Workschedule Availability API

Answers **"who is not working today"** for Workato, which calls it every time
someone @tags a colleague in a Wrike comment — so it is called far more often
than the other two integrations combined.

Port **5009**. Read-only: it never writes to the database and never touches
Wrike.

## Why this exists instead of giving Workato the database

Workato's MySQL connector could read `work_schedule_non_working_days` directly.
This service is better for four reasons, and the first is the deciding one:

- **A direct connection cannot be cached.** The table is rewritten once a day by
  the DB Resync, but Workato calls on every @tag. Straight to MySQL, every tag
  is a round trip; through here, nearly all of them are answered from memory.
  Measured against the live database: **7968 ms** for the first request,
  **5.08 ms** for the next fifty.
- **The database stays private.** Giving Workato access means the RDS instance
  must be reachable from Workato's cloud. This service can be the only thing
  exposed, so RDS can go back inside the VPC.
- **A rotatable token beats MySQL credentials in a SaaS**, scoped to one
  read-only endpoint rather than a database login.
- **The schema can change.** These columns have been renamed once already. A
  recipe bound to the table breaks; a recipe bound to this contract does not.

## Endpoints

All except `/hello` require `Authorization: Bearer <token>`.

### `GET /vfz_workschedule_availability/`

```json
{
  "date": "2026-08-27",
  "timezone": "Africa/Johannesburg",
  "count": 2,
  "userIds": ["KUATAIRI", "KUAP5OS3"],
  "users": [
    {
      "userId": "KUATAIRI",
      "user": "Abigail Hlalele",
      "workScheduleTitle": "Testing",
      "workScheduleId": "IEAFXAOHMIACBDCL"
    }
  ],
  "cached": true,
  "stale": false
}
```

`userIds` is the quick "is this person in the list" check most recipes want.
`users` carries the display name and schedule when the message needs them.

| Field | Meaning |
|---|---|
| `date` | The date this list describes, in the configured timezone. Not the caller's clock. |
| `count` | `users.length`, so a recipe can branch without counting. |
| `cached` | True when answered from memory without reading MySQL. |
| `stale` | True when MySQL could not be read and this is the last known good answer for today. Usable, slightly old. |

A `Cache-Control: private, max-age=<ttl>` header is set, so Workato or any proxy
can avoid even reaching this process during a burst.

**An empty `users` array is a real answer.** Most weekdays nobody on a reduced
schedule is off. It is not an error, and it is not the same as a failure — see
below.

### `GET /vfz_workschedule_availability/status`

Cache statistics plus a real database round trip. Returns **503** when MySQL is
unreachable, so a health check keyed on the status code notices.

`cache.hitRate` is the number to watch. If it is low, `cache_ttl_seconds` is
too short for how often Workato is calling.

### `POST /vfz_workschedule_availability/invalidate`

Drops the cached list so the next request re-reads MySQL. For when a resync has
just run and you want the new answer immediately rather than after the TTL.

### `GET /hello`

Heartbeat. Touches no dependency, needs no token — it exposes nothing beyond
the fact that the process is running.

## Failure behaviour

This is the part worth being careful about, because the wrong answer here is
worse than no answer:

| Situation | Response |
|---|---|
| Nobody is off today | `200`, `count: 0`, empty `userIds` |
| MySQL unreachable, earlier answer cached for today | `200`, `stale: true`, the earlier list |
| MySQL unreachable, nothing cached | **`503`** |
| Missing or wrong bearer token | `401` |

**Never an empty list on failure.** An empty list tells Workato nobody is off,
which silently suppresses the very warning it was asking about. A 503 makes the
recipe retry or alert; a wrong `200` makes it carry on confidently.

Serving a stale list is the deliberate middle ground: a few minutes old is far
better than wrong, and `stale: true` says so. A stale answer is never served for
a *different* date — yesterday's list must not be passed off as today's.

## Configuration

Everything comes from `../shared/segredo.ini`. This integration reads
`[availability]`, plus `[database]` and `[papertrail]`, which it shares with the
other two.

| `[availability]` | Default | Notes |
|---|---|---|
| `api_token` | *required* | Bearer token Workato sends. **Empty refuses to start** — this is the one service reachable from the internet. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `bind` | `0.0.0.0` | Public, unlike the other two, which bind to loopback |
| `port` | `5009` | |
| `timezone` | `Africa/Johannesburg` | Defines "today". At 01:00 in Johannesburg it is still yesterday in UTC |
| `cache_ttl_seconds` | `60` | How long a loaded list stays fresh. `0` disables caching |
| `log_keyword` | `vfz_workschedule_availability` | The SolarWinds search term |
| `log_every_requests` | `200` | Requests between rolled-up log lines |

`[database] non_working_table` must match what the DB Resync writes. All three
read the same file, so they cannot disagree.

## The cache

`cache.py`. Three properties, each one a failure mode avoided:

- **Keyed by date**, not just time. A time-only cache would keep serving
  yesterday's list past midnight, and which day the list describes is the
  entire content of the answer.
- **Serves stale on failure**, as described above.
- **One refresh per burst.** A wave of @tags arriving on a cold cache would
  otherwise all miss and all query. The load happens inside the lock, so one
  request reads and the rest get what it stored.

Returned lists are copies, so a caller cannot mutate what everyone else gets.

## Logging

To the same SolarWinds collector as the other integrations, keyword
`vfz_workschedule_availability`. Startup, every error, and a rolled-up line
every `log_every_requests` calls carrying the cache statistics.

Deliberately **not** one line per request: at one POST per @tag, logging would
cost more than the work it describes. Errors are always logged individually.

## Running

```bash
# from this directory, with ../.venv active
python vfz_workschedule_availability_main.py
```

In production it is a systemd unit — see `deploy/` and `../DEPLOYMENT.md`.
Unlike the other two, this one **does** need an Nginx location block, because
Workato calls it from outside the host.

## Tests

```bash
python -m pytest -q        # 19 passed
```

No database, no credentials, no network. The cache tests cover the awkward
cases: date rollover, a database that has gone away with and without a cached
answer, a stale answer being refused for the wrong date, and twelve concurrent
requests on a cold cache collapsing to one read.

## Layout

```
vfz_workschedule_availability/
├── vfz_workschedule_availability_main.py   FastAPI app, auth, routes
├── config.py                               segredo.ini -> Config
├── cache.py                                today's list, held in memory
├── deploy/                                 systemd unit
└── tests/
```

The table read itself is in `../shared/non_working_days.py`, shared with the DB
Resync and the Emails job so the column list is defined once.
