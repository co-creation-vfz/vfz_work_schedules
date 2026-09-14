# Workschedule DB Resync

A FastAPI service on **port 5007** with one job: expand each Wrike work
schedule's weekday pattern into dates, and write one row per user per
non-working date into MySQL.

The other half of the system is
[**Workschedule Emails**](../vfz_workschedule_emails/) (port 5008), which reads
that same table and comments on pending Wrike approvals whose approvers are off.
**This one feeds it.** Both are triggerable from the **Work Schedules** page at
`/dashboard/workschedule` in `vfz_process_simplification_integrations`, which
also shows who is off today and what the last resync did. See
[`../DEPLOYMENT.md`](../DEPLOYMENT.md) for how the two fit together.

> **`POST /v1/availability` has been removed.** It answered *"of these Wrike
> users, who is not working on this date?"* for a Workato recipe that then
> commented on the task. That recipe has been retired — the Emails integration
> now does the commenting it was built for — so the endpoint went with it, along
> with the availability service, its schemas, the bearer-token auth and the
> request-size middleware. If you are coming from the old docs, that is what is
> missing and why.

## The data model

One **row** per user per **non-working** date, in
`work_schedule_non_working_days`. The presence of a row means that user is *not*
working; absence means they are working.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `INT AUTO_INCREMENT` | Surrogate key. Nothing depends on its value. |
| `user_id` | `VARCHAR(64)` | Wrike contact id, e.g. `KUATAIRI`. |
| `user_name` | `VARCHAR(255)` | Display name, resolved at resync time so the Emails job can build its comment without a second API call. |
| `work_schedule_title` | `VARCHAR(255)` | For debugging: which schedule produced this row. |
| `work_schedule_id` | `VARCHAR(64)` | |
| `date` | `DATE` | The non-working date. |
| `seeded_by` | `VARCHAR(32)` | `NULL` on a real row; `testkit` on one written by the Emails job's test tooling, so its `unseed` can never delete real data. |
| `created_at` | `DATETIME` | Column default, so it is stamped once by the database's own clock. Not used by any query — it is there for audit. |
| `updated_at` | `DATETIME` | `ON UPDATE CURRENT_TIMESTAMP`. |

Columns are snake_case; the JSON responses are camelCase. `repository.py`
aliases them on the way out:

```json
{
  "userId": "KUAXXXXX",
  "user": "Jane Doe",
  "workScheduleTitle": "Testing",
  "workScheduleId": "IEAFXAOHMIACBDCL",
  "date": "2026-08-25"
}
```

`date` is rendered as a `YYYY-MM-DD` string rather than handed back as a
`datetime.date`, because a caller comparing against `"2026-08-25"` must not
silently see a date object instead.

One thing to hold on to: a **missing row means "working"**. So if the resync
stops running, nothing fails loudly — the table quietly reports everyone as
available. Alert on the cron itself, and on the newest `date` in the table
falling behind today. (The Emails job does exactly this, and calls it
`upstreamLate`.)

## Where the SQL lives

Every SQL statement in this integration is in **`repository.py`**, and every one
is parameterised. The populator holds none of its own — it calls repository
methods — and the connection pool comes from `../shared/database_helpers.py`,
built lazily on first use so startup survives a briefly unreachable RDS.

That is not just tidiness. It is the seam the test suite substitutes: an
in-memory repository stands in for the real one, so everything above the
repository is tested with no database and no credentials. See [Tests](#tests).

**The `CREATE TABLE` itself is not here** — it lives in
`../shared/database_helpers.py`, alongside the Emails job's tables. That table is
the *contract* between the two integrations, and two copies of its definition
would eventually disagree; the way you would find out is the Emails job silently
reading a column this one had stopped writing.

The DDL builders take table names as arguments rather than baking the defaults
in, so a configured name carries its child tables with it. `schema.sql` at the
repo root is generated from those same builders, which means the file a DBA
reviews and the statement the service runs cannot drift apart.

## Shared helpers

`../shared/` holds what both integrations need, and each entry is there because
having two copies had already caused (or would cause) a real problem:

| Module | What it gives this integration |
| --- | --- |
| `general_helpers.py` | SolarWinds logging, the run stamp, retry-with-backoff. |
| `database_helpers.py` | The pooled `DatabaseConnection`, the table names, and every `CREATE TABLE`. |
| `wrike_helpers.py` | One Wrike client — work schedules, contacts, and (for the Emails job) approvals, tasks and comments. |
| `config_helpers.py` | Reads `segredo.ini` and validates every value, so a bad one fails the same way in both integrations. |

## From weekdays to dates

Wrike work schedules describe a *pattern* — "works Monday to Thursday" — while
the table needs *dates*. The resync bridges the two: for each user it walks the
window and writes a row for every **weekday** that is not one of their working
days.

A four-day week (Mon–Thu) over one week yields exactly one row: that Friday.

```
Mon 24  works      -> nothing
Tue 25  works      -> nothing
Wed 26  works      -> nothing
Thu 27  works      -> nothing
Fri 28  not worked -> { userId, date: "2026-08-28", ... }
Sat 29  weekend    -> nothing
Sun 30  weekend    -> nothing
```

**Deliberately excluded**, as agreed:

- **Weekends.** Saturday and Sunday are never written. They are non-working for
  practically everyone, so materialising them would inflate the table for no
  decision-making value.
- **Schedule exclusions** — leave, public holidays, one-off days off. Wrike keeps
  these at `/workschedules/{id}/workschedule_exclusions`; that endpoint is not
  called and exclusions are not read. Only the weekly pattern is used. The hook
  if that changes: `expand_non_working_dates` takes an `extra_dates` argument,
  and any weekday in it is included in the output.

### What a real run actually writes

Checked against the live Wrike account, so these numbers are not hypothetical.
**224 assignments resolve cleanly**, with no unreadable schedules:

| Schedule | Members | Weekdays off |
| --- | --- | --- |
| Default Schedule | 221 | none — works Mon–Fri |
| Default Schedule test | 2 | Wednesday, Friday |
| Testing | 1 | Monday, Friday |

**Expect a handful of rows, not hundreds.** Only three people are on a reduced
week; the other 221 work Mon–Fri and correctly generate nothing at all. A
Wednesday run writes 2 rows, a Friday run 3, and a Tuesday or Thursday run
**0**. An empty table on a Tuesday is the *right answer*, not a failed run —
which is exactly why the Emails job goes to the trouble of distinguishing
"nobody is off" from "the resync never ran".

Three other schedules exist (Brand Team, Studio, Half day schedule) but have no
members, so they are skipped.

### Running the resync

Designed to be run by cron over a single-day window, not to materialise dates in
advance. The run itself lives in **`resync.py`**; `run_resync.py` is a thin
argparse wrapper around it, and so is the HTTP trigger. One code path, so a
manual resync from the dashboard cannot behave differently from the cron —
which matters, because the cron's answer is what the Emails job acts on.

```bash
# preview — writes nothing
python run_resync.py --dry-run

# what the cron runs: a horizon_days window (default: today only)
python run_resync.py

# today only
python run_resync.py --today

# an explicit window
python run_resync.py --from 2026-09-01 --to 2026-09-30

# print the live Wrike payload and exit
python run_resync.py --dump-raw
```

`--source wrike` is the default, so none of the above need to say it. See
[Where the weekday patterns come from](#where-the-weekday-patterns-come-from)
for the explicit opt-in, `--source json`.

Exit codes make the cron alertable, and each one is a distinct thing worth
noticing rather than one catch-all failure:

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Nothing to populate — the source returned no assignments. |
| `2` | Bad arguments, or unreadable configuration. |
| `3` | At least one schedule could not be read. Dates were still written for the rest, but a partial answer must not pass silently. |
| `4` | A schedule's non-working count changed too abruptly to write unattended. See [The blast-radius guard](#the-blast-radius-guard). |
| `5` | The database could not be reached. **Nothing was written.** |
| `6` | The schedule source could not be read at all — a missing `schedules.json`, or Wrike refusing the token. |

`5` is deliberately separate from `1`, and that distinction is the whole reason
these codes exist: a day with no rows reads as *"everyone is working"*. "No
schedules today" and "the database was down and today's dates were never
written" look identical from the outside, and only the second one is wrong.

`horizon_days` counts today, so the default of **1** is a today-only window —
what a daily cron needs. Worth being explicit about the same consequence: a
missed run does not fail loudly, it silently reports everyone as working that
day. Alert on the cron, or widen the window to buy slack.

### Does a stale row get cleaned up?

Yes — that is the point of the window. Each run computes the dates that *should*
exist for each user, upserts those, and deletes that user's other rows inside
the window.

So: you work Tuesday, but a Tuesday row exists for you. Tuesday's run expands
your pattern to **no** dates for that day, so the existing row is not in the keep
set and is deleted. The same mechanism handles a schedule change: move from a
four-day to a five-day week and the stale Fridays disappear as each one comes
into the window. The upsert never touches `created_at`, so the audit trail
survives a re-run.

**The one blind spot: cleanup only touches users the source returned.** Someone
removed from every Wrike schedule is never expanded, so nothing deletes their old
rows and they go on being reported as not working. Those users are listed in the
run summary as rows "for users the source did not return", and `--purge-orphans`
deletes them:

```bash
python run_resync.py --source wrike --purge-orphans
```

It is opt-in on purpose — with a partial source such as a hand-written
`schedules.json`, purging would wipe every user the file does not list. Safe with
`--source wrike`, which returns every schedule.

**A user whose schedule could not be parsed is a different case, and is never
purged**, with or without `--purge-orphans`. Their real pattern is unknown for
this run, not confirmed absent, so deleting their rows would silently flip them
to "working" over what may be a transient read failure — exactly what exit code
`3` exists to make someone notice instead. Existing rows are left untouched;
re-run once the schedule reads correctly.

### The blast-radius guard

A schedule can also be read *successfully* and still produce a result worth
stopping for: its actual pattern changed in Wrike — deliberately or by mistake —
and most of a large, shared population would suddenly be marked not working in
one run. Nothing about that looks like a parsing failure, so without a separate
check it writes silently and reaches a live Wrike task.

Before writing, the populator groups assignments by `work_schedule_id` and, for
any schedule with at least `min_schedule_size_for_guard` members (default **10**
— a small team sharing a day off is normal, not a red flag) where more than
`max_non_working_fraction` (default **50%**) of them would newly be marked not
working on the same date, refuses to write that schedule at all this run.
Existing rows for those users are left exactly as they were — same protection as
an unparseable schedule, including from `--purge-orphans` — and the run exits `4`
so the cron notices instead of trusting it. Confirm the change is real in Wrike,
then re-run; a schedule that reads the same way twice no longer trips the guard.

```bash
python run_resync.py --min-schedule-size-for-guard 20 --max-non-working-fraction 0.6
```

Both flags default to the `[db_resync]` values in `segredo.ini`, and omitting
them on the HTTP trigger uses those too — the dashboard sends neither, so
defaulting them to literals would have silently overridden a tuned setting on
every click.

One more consequence of keeping exactly the agreed fields: the resync cannot tell
its own rows from any written by hand, so it treats every row in the window as
its own. Anything hand-added inside the window is removed on the next run. Add
such days outside the window, or tag them — `seeded_by` is already there for
exactly this — and exclude that tag in `_stale_clause` in `repository.py`.

### Where the weekday patterns come from

Two interchangeable sources, and **neither makes its own HTTP calls**. Both take
the shared `WrikeClient` from `../shared/wrike_helpers.py`, so contact-name
resolution gets the same retry policy, batching and per-contact degradation the
Emails job has. (`httpx` is gone; everything is `requests` through that one
client. Previously this integration did its own unretried contact lookup, so a
single unreadable contact wrote a row with a `NULL` name.)

**`--source json`** reads `schedules.json` — copy `schedules.example.json` and
edit. Day names, three-letter abbreviations and ISO numbers (1 = Monday) all
work. Useful for testing without a Wrike token; not the default:

```json
{
  "schedules": [
    {
      "workScheduleId": "IEAFXAOHMIACBDCL",
      "workScheduleTitle": "Testing",
      "workingDays": ["Tue", "Wed", "Thu"],
      "users": [{ "userId": "KUATAIRI", "user": "Abigail Hlalele" }]
    }
  ]
}
```

**`--source wrike`** (the default) calls `GET /workschedules?fields=["userIds"]`
with `[wrike] api_token` (scope `amReadOnlyWorkSchedule`), then **always** follows
up with `GET /contacts` to resolve display names — a row with a null `user_name`
because a flag was left off was a silent, easy-to-miss mistake, so it is no
longer optional. It is mapped against the payload this account actually returns:

```json
{
  "id": "IEAFXAOHMIACBDCL",
  "scheduleType": "Custom",
  "title": "Testing",
  "workweek": [{ "workDays": ["Tue", "Wed", "Thu"], "capacityMinutes": 480 }],
  "userIds": ["KUAW3EHG", "KUATAIRI"]
}
```

`workweek` is a *list* of blocks. Blocks are unioned, and a block with
`capacityMinutes: 0` counts as not worked. `parse_workweek` also accepts several
looser shapes (a plain list of day names, one object per day, a day-keyed
mapping) so a future API change is unlikely to break it, and `--dump-raw` prints
the live payload whenever you want to check.

The account is **EU-hosted**, and `[wrike] api_base_url` defaults to
`https://app-eu.wrike.com/api/v4` accordingly. The US endpoint answers `401` for
an EU token, which reads as a bad token rather than a wrong host — leave it
alone unless the token really is US-bound.

Three behaviours worth knowing:

- **Schedules with no members are skipped.** They have `userIds: []` and
  contribute nothing.
- **One assignment per user.** A user found on both the Default Schedule and a
  custom one gets the custom schedule; two custom schedules for the same person
  is flagged as a problem to resolve in Wrike. This matters because the resync
  keys its writes on user and date — two assignments for one user would undo
  each other, and the last to run would win silently.
- **A pattern that resolves to zero working days is a parsing failure**, not
  "off all week". That user is skipped and reported, and the run exits `3`. A
  misread schedule must never put comments on live Wrike tasks.

### Schema and keys

`WorkScheduleRepository.ensure_schema()` runs an idempotent
`CREATE TABLE IF NOT EXISTS` at startup, so a fresh environment needs no manual
migration step. `../schema.sql` is **generated from the same builders**, for a
DBA who would rather create it ahead of time or review it first:

```bash
mysql -h vfz-wrike-integration.cj6hvdyvczrr.eu-north-1.rds.amazonaws.com \
      -u admin -p vfz-wrike-workschedule-v1 < ../schema.sql
```

Two keys, and each earns its place:

| Key | Purpose |
| --- | --- |
| `UNIQUE KEY uniq_user_date (user_id, date)` | The real guarantee against a double-entered day. Every writer in both integrations upserts on exactly this key, so a normal write can never trip it — it only ever rejects a genuine duplicate. |
| `KEY idx_date_user (date, user_id)` | Serves the lookup: `WHERE date = ? AND user_id IN (...)`. |

The important difference from the MongoDB version this replaced: the unique
constraint is **created with the table**, not added to a collection that already
has data in it. So the old one-time migration — hunt for duplicates, delete them,
drop and rebuild the index — no longer applies, and neither does the failure mode
it existed for.

What *is* still here is the read-side collapse in `find_non_working`, which
de-duplicates by user before returning. That is belt and braces for rows written
before the constraint existed.

## Running it

Python 3.9 or newer (it uses `zoneinfo` from the standard library).

`zoneinfo` reads the *system* timezone database, which not every platform
ships — notably Windows, and some minimal Linux images. `requirements.txt` pins
the `tzdata` package specifically so the configured timezone resolves everywhere
regardless; if you ever see `ZoneInfoNotFoundError`, that dependency is missing
from the environment that raised it.

### 1. Create a virtual environment

**One venv at the repo root serves both integrations.** A venv keeps these
dependencies out of your system Python, and it is what makes the cron entry
reproducible — you point cron at the venv's own interpreter rather than whatever
`python3` happens to mean at 4am.

```bash
cd ..                              # the repo root
python3 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows PowerShell / cmd
python -m pip install --upgrade pip
pip install -r requirements.txt
```

That one file covers both services *and* `pytest`, because the suites need no
database and no credentials — there is no reason not to be able to run them
wherever the code is.

If the install fails on `uvicorn[standard]`, it is usually a missing C toolchain
for `uvloop`/`httptools`. Plain `pip install uvicorn` works fine — the standard
extras are only a performance nicety.

### 2. Configure

Everything is in **`../shared/segredo.ini`** — one file for both integrations,
because they share a database, a table, a Wrike account and a log collector.
Two copies meant a credential rotation had to be done twice, with the second copy
discovered only when something failed.

```bash
cp ../shared/segredo.ini.example ../shared/segredo.ini
$EDITOR ../shared/segredo.ini
```

Only three values need filling in — `[wrike] api_token`, `[database] password`
and `[papertrail] token`. Everything else already has a working default.

A bad value stops the service at import with `Configuration error: …` and
**exit 2**, rather than a traceback: this is the one failure an operator can
actually fix, and it is what shows up in
`journalctl -u vfz-workschedule-db-resync`.

### 3. Run the service

```bash
python vfz_workschedule_db_resync_main.py
```

Interactive docs at `/docs`, OpenAPI schema at `/openapi.json`. `/hello` is the
cheapest "is it up" check and touches no dependency at all;
`/vfz_workschedule_db_resync/status` is the one that does a real database round
trip and echoes back the database and table it is actually using — the quickest
way to confirm your `segredo.ini` took effect.

**Cron does not read your shell**, so an activated venv means nothing to it. Give
it the absolute path to the venv's interpreter:

```cron
# 04:00 Africa/Johannesburg
CRON_TZ=Africa/Johannesburg
0 4 * * 1-5 cd /srv/vfz_work_schedule/vfz_workschedule_db_resync && \
  /srv/vfz_work_schedule/.venv/bin/python run_resync.py \
  >> /var/log/work-schedules.log 2>&1
```

No `activate` needed — running `.venv/bin/python` directly uses that
environment's packages.

### 4. Fill the table

An empty table means everyone reads as working, so this step is not optional.

```bash
python run_resync.py --dry-run
python run_resync.py
```

Or without a Wrike token, from a local file:

```bash
cp schedules.example.json schedules.json    # edit it
python run_resync.py --source json --dry-run
python run_resync.py --source json
```

`seed_sample_data.py` inserts a couple of hand-written rows instead, if you just
want something in the table to query against.

Or trigger a resync over HTTP, which is what the dashboard does:

```bash
curl -X POST http://127.0.0.1:5007/vfz_workschedule_db_resync/ \
  -H "Content-Type: application/json" \
  -d '{"dryRun": true, "triggeredBy": "manual"}'

curl http://127.0.0.1:5007/vfz_workschedule_db_resync/status
```

### Configuration reference

Read from `../shared/segredo.ini`. Every value has a working default except the
three secrets.

| Section | Option | Default | Notes |
| --- | --- | --- | --- |
| `[wrike]` | `api_token` | *(required)* | The Co-creation integration bot token. |
| | `api_base_url` | `https://app-eu.wrike.com/api/v4` | EU-hosted account. |
| | `request_timeout` | `30` | Seconds before a Wrike call is abandoned and retried. |
| `[database]` | `host` | *(the RDS endpoint)* | The shared VFZ work-schedule instance. |
| | `name` | `vfz-wrike-workschedule-v1` | |
| | `user` | `admin` | |
| | `password` | *(required)* | Not whitespace-stripped — a password may legitimately begin or end with a space. |
| | `port` | `3306` | |
| | `timeout_seconds` | `30` | Connection timeout. |
| | `non_working_table` | `work_schedule_non_working_days` | **The contract with the Emails integration.** Both read this same file, so they cannot disagree — leave it alone. Validated as a plain identifier at startup, because a table name reaches SQL as an identifier and cannot be a bind parameter. |
| | `runs_table` | `work_schedule_resync_runs` | The run log behind `lastRun`. One row per finished run, written by cron and dashboard alike, so "last sync" survives a restart and is not just this process's memory. |
| `[papertrail]` | `url` | *(the SolarWinds collector)* | Same collector as every other VFZ integration. |
| | `token` | *(required)* | |
| | `mode` | *(empty)* | Empty sends to SolarWinds. `print_only` prints locally and sends nothing — use it when developing against live data. `do_nothing` silences the logger. |
| `[db_resync]` | `bind` | `127.0.0.1` | See [Binding and auth](#binding-and-auth). |
| | `port` | `5007` | Free alongside the process-simplification services, which hold 5001–5006. |
| | `log_keyword` | `vfz_workschedule_db_resync` | The search term that isolates this service's log lines. Stable forever. |
| | `timezone` | `Africa/Johannesburg` | Resolves "today". Rejected at startup if not a known zone. |
| | `skip_weekends` | `true` | Saturday and Sunday are not evaluated. |
| | `horizon_days` | `1` | Window length in days, counting today. `1` = today only. Must be at least 1. |
| | `schedules_file` | `schedules.json` | Weekday patterns for `--source json`. |
| | `min_schedule_size_for_guard` | `10` | Blast-radius guard minimum. |
| | `max_non_working_fraction` | `0.5` | Blast-radius guard fraction. Must be greater than 0 and at most 1. |

## Binding and auth

The service binds to **`127.0.0.1`** and has **no bearer token**. The dashboard
is the only caller and it runs on the same host, so there is nothing to gain from
listening on a public interface — and, more to the point, no token that has to be
kept identical across two config files. That mismatch was a real failure mode;
removing the token removes it entirely rather than trading one risk for another.

Nothing outside the host calls this service, so **no Nginx configuration is
needed at all**.

If you later need to trigger a resync from off the box, widen `[db_resync] bind`
**and put authentication in front of it**. Do not do one without the other.

## Endpoints

None of these require auth — see [Binding and auth](#binding-and-auth).

### `POST /vfz_workschedule_db_resync/`

Starts a resync and returns immediately. A full Wrike resync is one
`/workschedules` call plus a `/contacts` lookup per 100 users, which is long
enough that holding the connection open would time out behind a proxy — so the
work goes to a background task and the caller polls
[`/status`](#get-vfz_workschedule_db_resyncstatus). That is what the dashboard
does. SolarWinds gets the outcome either way.

Every field is optional; the defaults are the cron's.

```http
POST /vfz_workschedule_db_resync/
Content-Type: application/json

{
  "source": "wrike",
  "dateFrom": "2026-09-01",
  "dateTo": "2026-09-30",
  "todayOnly": false,
  "dryRun": true,
  "purgeOrphans": false,
  "triggeredBy": "dashboard"
}
```

`triggeredBy` is carried into every log line for the run, so a manual resync is
distinguishable in SolarWinds from the scheduled one — the first question worth
asking when a day looks wrong. `minScheduleSizeForGuard` and
`maxNonWorkingFraction` may also be sent; omit them to use the configured values.

```json
{
  "status": "accepted",
  "message": "Resync started for 2026-09-01 to 2026-09-30 (dry run, nothing will be written). Poll the status endpoint for the result.",
  "accepted": true,
  "running": true,
  "dateFrom": "2026-09-01",
  "dateTo": "2026-09-30",
  "dryRun": true
}
```

A second trigger while one is in flight returns `"status": "ignored"` with
`accepted: false`, **not** an error. It is refused rather than queued: by the
time a queued run started, the one in flight would already have written the same
answer. `dryRun` defaults to **false**, matching the cron — a default of `true`
would look like a successful run while writing nothing.

A backwards window is a `422`.

### `GET /vfz_workschedule_db_resync/status`

What the last resync did, and the state of the table it writes. This is also the
real database round trip, so it is the right thing for a health check to poll.

```json
{
  "running": false,
  "lastRun": { "status": "success", "exitCode": 0, "triggeredBy": "cron", "inserted": 2, "removed": 0, "runId": 41, "…": "…" },
  "lastSuccessfulRun": { "status": "success", "exitCode": 0, "triggeredBy": "cron", "finishedAt": "2026-08-31T03:00:07+02:00", "…": "…" },
  "database": "vfz-wrike-workschedule-v1",
  "table": "work_schedule_non_working_days",
  "timezone": "Africa/Johannesburg",
  "today": "2026-08-26",
  "horizonDays": 1,
  "rowCount": 2,
  "databaseReachable": true
}
```

`lastRun` comes from the **run log in MySQL** (`work_schedule_resync_runs`), which
every run appends to at the end of `_finish` — so the nightly cron, a dashboard
click and a CLI run all appear here, and restarting the service does not erase
the answer. `triggeredBy` says which of the three it was. `null` means no run has
ever been logged.

`lastSuccessfulRun` is the same record filtered to `status = "success"`: the last
run that actually wrote the table, which is what a person means by "last sync". A
failed 03:00 run therefore shows up in `lastRun` **without** moving
`lastSuccessfulRun`, which is the distinction that makes a stale table visible.

If MySQL is unreachable both fall back to the last run this process did, and
`databaseReachable` goes false rather than throwing, so the endpoint still answers
when RDS does not.

### `GET /vfz_workschedule_db_resync/non-working-days?date=`

Everyone recorded as not working on one date; defaults to today.

This is the same question the Emails job asks, so the two agree by construction —
which is what makes it useful for support. If the dashboard shows somebody off
and no comment was posted, the disagreement is not here.

```json
{
  "date": "2026-08-26",
  "weekday": "Wednesday",
  "timezone": "Africa/Johannesburg",
  "table": "work_schedule_non_working_days",
  "tableRowCount": 2,
  "count": 2,
  "users": [{ "userId": "KUATAIRI", "user": "Abigail Hlalele", "…": "…" }]
}
```

Returns `503` if MySQL is unreachable — never an empty list, which would read as
"nobody is off".

### `GET /hello`

The heartbeat. Touches no dependency at all, so it answers even when the database
and Wrike are both down — which is exactly what makes it useful for telling "the
process is dead" apart from "the process is up and its dependencies are not".

## Tests

```bash
python -m pytest -q
```

**99 pass, 12 skip**, and no database, credentials or network are required. The
suite runs against an in-memory repository (`tests/fakes.py`) implementing the
same interface as the real `WorkScheduleRepository`. Because all the SQL lives
behind that one seam, everything above it — the populator, the resync, the
routes, the CLI — is tested *exactly as it ships*.

What that deliberately does not cover is whether the SQL itself is right. That is
`tests/test_repository_mysql.py`, which runs the real statements against a real
MySQL and **skips itself** unless one is configured — which is where the 12 skips
come from:

```bash
TEST_MYSQL_HOST=127.0.0.1 TEST_MYSQL_DATABASE=work_schedules_test \
TEST_MYSQL_USER=root TEST_MYSQL_PASSWORD=secret \
  python -m pytest tests/test_repository_mysql.py
```

Point it at a **scratch database, never the live one**: each test truncates the
table it works on. `TEST_MYSQL_TABLE` (default `test_non_working_days`) keeps the
table name distinct from production's even if the database is shared. It has been
run green against the real RDS instance, so the statements are known to be valid
MySQL 8.4 and not merely plausible.

Coverage includes:

- **Weekday expansion:** four-day and full weeks, single-day and multi-week
  ranges, weekend exclusion (including a schedule that works weekends), the
  `extra_dates` hook, empty and invalid patterns, and every weekday spelling.
- **Populator:** row structure, no weekend writes, idempotent re-runs,
  stale-date removal after a schedule change, other users and out-of-window
  dates left untouched, dry runs, skipped unparsed patterns, orphan detection
  and purging, a user whose schedule failed to parse surviving
  `--purge-orphans` regardless, all the dates going in one upsert call, and the
  blast-radius guard — tripping on a large schedule's majority swing, staying
  quiet for a small schedule or a small fraction, surviving `--purge-orphans`,
  and its thresholds being configurable.
- **Sources:** the file format and each Wrike payload shape, the loud failures
  for an unknown field name or a pattern that resolves to nothing, and that an
  unreadable schedule reports its members via `unresolved_user_ids` instead of
  silently dropping them.
- **Resync** (`tests/test_resync.py`): a successful run's reported counts, a dry
  run writing nothing, each exit code (`1`, `2`, `3`, `5`, `6`) coming from the
  condition it names, the last-run result being recorded, a second run being
  refused while one is in flight, and `horizon_days` shaping the default window.
  Then the route around it: the trigger actually running a resync, defaulting to
  a *live* run rather than a silent rehearsal, rejecting a backwards window, an
  omitted guard override falling through to the configured value, and the status,
  listing and heartbeat endpoints.
- **SQL** (`tests/test_repository_mysql.py`, skipped by default): a round-tripped
  upsert, re-upserting updating rather than duplicating, `created_at` surviving
  an update, the unique key rejecting a genuine duplicate, inclusive ordered
  range queries, `delete_stale` keeping what it is told to and clearing the
  window when told to keep nothing, other users and windows left alone, and the
  window/user-scoped deletes.
- **CLI:** the blast-radius guard's exit code (`4`) and its thresholds wired end
  to end through `run_resync.py`, not just the populator's internal logic; that
  `--source wrike` resolves display names unconditionally; that the table is
  created before writing; and that an unreachable database exits `5` rather than
  looking like a quiet success.

## Deployment

**[`../DEPLOYMENT.md`](../DEPLOYMENT.md) is the full picture** — both
integrations, the shared database, cron and the dashboard, in the order to do
them. What is specific to this one:

- **systemd unit:** `deploy/vfz-workschedule-db-resync.service`. Config comes
  from `../shared/segredo.ini`, so nothing secret goes in the unit file.

  ```bash
  sudo cp deploy/vfz-workschedule-db-resync.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now vfz-workschedule-db-resync
  ```

- **The cron is still required.** The HTTP trigger is the *manual* one; deploying
  the service does not schedule anything. The nightly cron runs `run_resync.py`.
  An unpopulated table reads as "everyone is working", so a box with the service
  running and no cron installed is silently wrong rather than visibly broken.

- **The resync writes**, so a read-only MySQL user is not an option here.

## What is deliberately not here

- No weekend rows, and no public holidays, leave or other schedule exceptions.
  Both are excluded on purpose for this iteration; the hook for exceptions is
  `extra_dates` in `expansion.py`.
- No comment creation and no Slack alerting. This integration makes no Wrike
  *writes* at all — it only reads work schedules and contacts. Comments are
  posted by [Workschedule Emails](../vfz_workschedule_emails/), a separate
  service reading the same table.
- No caching. The queries are indexed point lookups; add caching only if a real
  load profile asks for it.

## Layout

```
vfz_workschedule_db_resync_main.py   FastAPI app and routes (port 5007)
run_resync.py       the resync CLI — what the cron runs; wraps resync.py
seed_sample_data.py a few hand-written rows, to have something to query
resync.py           one resync run; shared by the CLI and the HTTP trigger
repository.py       every SQL statement in this integration, and nothing else has any
populator.py        decides what to write and what to remove; holds no SQL
sources.py          where weekday patterns come from (file or Wrike)
expansion.py        weekday pattern -> dates
weekdays.py         weekday parsing and naming
models.py           request/response schemas
config.py           reads ../shared/segredo.ini and validates every value
deploy/
  vfz-workschedule-db-resync.service   systemd unit
tests/
  conftest.py              a Config with no I/O, so tests never read the real segredo.ini
  fakes.py                 in-memory repository, the suite's substitute for MySQL
  test_repository_mysql.py the SQL itself; skipped unless TEST_MYSQL_HOST is set
  test_resync.py           the resync run and its HTTP trigger
  test_populator.py        what gets written, removed and guarded
  test_sources.py          the Wrike and JSON payload shapes
  test_expansion.py        weekday pattern -> dates
  test_cli.py              run_resync.py end to end
schedules.example.json     copy to schedules.json for --source json
```

Shared helpers are in [`../shared/`](../shared/); the generated DDL is
[`../schema.sql`](../schema.sql).
