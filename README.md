# VFZ Work Schedule Integrations

Three integrations, one shared MySQL database, one shared `segredo.ini`.

| Integration | Port | What it does | Called by |
|---|---|---|---|
| [`vfz_workschedule_db_resync`](vfz_workschedule_db_resync/) | 5007 | Expands each Wrike work schedule's weekday pattern into dates and writes one row per user per non-working date. | cron, dashboard |
| [`vfz_workschedule_emails`](vfz_workschedule_emails/) | 5008 | Reads those rows, finds pending Wrike approvals whose approvers are off, and comments on the task tagging whoever can act. | cron, dashboard |
| [`vfz_workschedule_availability`](vfz_workschedule_availability/) | 5009 | Serves "who is not working today" over HTTP, cached. | **Workato**, on every @tag |

5007 and 5008 bind to loopback with no auth — only the dashboard, on the same
host, calls them. 5009 binds publicly and requires a bearer token, because
Workato calls it from outside.

**The resync feeds the other two.** That ordering is the whole system: an empty
"who is off today" is either a quiet day or a resync that never ran, and both
consumers are built to tell those apart rather than quietly report nothing.

"Emails" is the user-facing name. The job posts a Wrike comment tagging the
people who need to know; what they receive is Wrike's notification email.
Nothing here sends mail directly.

## Layout

Modelled on `vfz_process_simplification_integrations`: shared helpers in
`shared/`, one folder per integration, each with a `<folder>_main.py`.

```
vfz_work_schedule/
├── segredo.ini                     -> shared/segredo.ini (the only secrets file)
├── requirements.txt                one environment for all three
├── schema.sql                      generated from shared/database_helpers.py
├── DEPLOYMENT.md
│
├── shared/
│   ├── segredo.ini                 ALL secrets and settings, all three
│   ├── segredo.ini.example         committed template
│   ├── config_helpers.py           reads segredo.ini, validates every value
│   ├── general_helpers.py          SolarWinds logging, the run stamp, retries
│   ├── database_helpers.py         MySQL pool, table names, idempotent DDL
│   ├── non_working_days.py         the canonical read of the shared table
│   └── wrike_helpers.py            one Wrike client: schedules, contacts,
│                                   approvals, tasks, comments
│
├── vfz_workschedule_db_resync/
│   ├── vfz_workschedule_db_resync_main.py   FastAPI service (port 5007)
│   ├── run_resync.py               CLI — what the nightly cron runs
│   ├── resync.py                   one run, shared by the CLI and the route
│   ├── repository.py               all SQL for the non-working-day table
│   ├── populator.py                what to write, and what to remove
│   ├── sources.py                  Wrike / local-JSON schedule sources
│   ├── expansion.py, weekdays.py   weekday pattern -> dates
│   ├── models.py                   request/response schemas
│   └── tests/
│
└── vfz_workschedule_emails/
    ├── vfz_workschedule_emails_main.py      FastAPI service (port 5008)
    ├── run_notifier.py             CLI — what the hourly cron runs
    ├── notifier.py                 who to tell, and once only
    ├── store.py                    all SQL for the notifier's tables
    ├── comments.py                 the comment text and @mention markup
    ├── baseline.py                 go-live snapshot — run once, before the cron
    ├── retract.py                  undo notifications that should not have gone
    ├── diagnose.py                 read-only: what can the token see?
    ├── testkit.py                  seed and inspect test data
    ├── scripts/                    cron wrappers (Linux + Windows)
    └── tests/

└── vfz_workschedule_availability/
    ├── vfz_workschedule_availability_main.py  FastAPI service (port 5009)
    ├── cache.py                    today's list, held in memory
    └── tests/
```

## What is shared, and why

Each of these was duplicated before, and duplication in these five places is
what would actually break the system:

| `shared/` | Why it is shared |
|---|---|
| `general_helpers.py` | The SolarWinds logger was byte-identical in both. One copy means one log shape, so a single saved search reads both services. |
| `database_helpers.py` | Both connect to the same database, and **the non-working-day table is the contract between them**. Its DDL lived in three places; two copies would eventually disagree, and the symptom would be the Emails job reading a column the resync had stopped writing. |
| `wrike_helpers.py` | Both resolve contact names. The resync used to do its own unretried lookup, so one unreadable contact wrote a row with a NULL name while the notifier's copy degraded gracefully. One client, one retry policy, one contact cache. |
| `config_helpers.py` | One `segredo.ini`, one set of validators. A bad value now fails the same way everywhere: a `ConfigError` at startup, not a traceback mid-run. |
| `non_working_days.py` | All three read the shared table. The `SELECT` and its column aliases live here, so a rename cannot leave one integration behind — which nearly happened once already. |

## Configuration

Everything lives in `shared/segredo.ini` — one file, because all three share a
database, a table, a Wrike account and a log collector. Separate copies meant a
credential rotation had to be done more than once, with the stragglers
discovered only when something failed.

```bash
cp shared/segredo.ini.example shared/segredo.ini
$EDITOR shared/segredo.ini        # fill in api_token, password, token
```

| Section | Read by | Holds |
|---|---|---|
| `[wrike]` | resync, emails | Token, EU base URL, space, recycle bin, project-lead field, fallback contact |
| `[database]` | all three | RDS host/name/user/password/port, and the table names |
| `[papertrail]` | all three | SolarWinds URL, token, and the log mode |
| `[db_resync]` | resync | Bind/port, timezone, horizon, blast-radius guard |
| `[emails]` | emails | Bind/port, timezone, notification window, grace period, dry run |
| `[availability]` | availability | Bind/port, **required** bearer token, cache TTL |

`non_working_table` is the one setting all three **must** agree on. They all
default to the same value, so the safe move is to leave it alone.

The resync and emails services bind to **127.0.0.1** with no authentication: the
dashboard is their only caller and it runs on the same host, so there is nothing
to gain from a public interface — and nothing to authenticate, which removes a
whole class of "the token in one config file does not match the other" failures.

The availability API is the exception. Workato calls it from outside, so it binds
publicly and **requires** `[availability] api_token`; it refuses to start without
one, because that would publish the staff absence list.

## Running

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# services
cd vfz_workschedule_db_resync    && python vfz_workschedule_db_resync_main.py
cd vfz_workschedule_emails       && python vfz_workschedule_emails_main.py
cd vfz_workschedule_availability && python vfz_workschedule_availability_main.py

# the jobs themselves, which is what cron runs
cd vfz_workschedule_db_resync && python run_resync.py --dry-run
cd vfz_workschedule_emails    && python run_notifier.py --dry-run --force
```

Each service exposes `/hello` as a dependency-free heartbeat, a `POST` slug to
trigger a run, and `GET .../status` to poll the result. The trigger returns
immediately and the work happens in a background task, because a full run takes
long enough that holding the connection open would time out behind a proxy.

## The dashboard

The **Work Schedules** page at `/dashboard/workschedule` in
`vfz_process_simplification_integrations` triggers the resync and emails jobs and
shows who is off today, the last run of each, and the notification history. It is
one page because you need both halves to tell a quiet day from a resync that
never ran. The availability API has no dashboard panel: Workato is its only
caller, and `/vfz_workschedule_availability/status` reports its cache.

## Tests

No database, no credentials, no network:

```bash
cd vfz_workschedule_db_resync    && python -m pytest -q                     # 105 passed
cd vfz_workschedule_emails       && python -m unittest discover -s tests   # 131 passed
cd vfz_workschedule_availability && python -m pytest -q                     #  19 passed
```

The DB-backed tests run against in-memory doubles that implement the same
interface as the real repository and store, so everything above them is
exercised exactly as it ships. The SQL itself is covered separately, against a
real MySQL, and those suites **skip themselves** unless one is configured:

```bash
TEST_MYSQL_HOST=... TEST_MYSQL_DATABASE=... TEST_MYSQL_USER=... TEST_MYSQL_PASSWORD=... \
  python -m pytest tests/test_repository_mysql.py      # in db_resync
TEST_MYSQL_HOST=... TEST_MYSQL_DATABASE=... TEST_MYSQL_USER=... TEST_MYSQL_PASSWORD=... \
  python -m unittest tests.test_store_mysql           # in emails
```

Point them at a **scratch** database: each empties the tables it uses. They are
worth running after any change to `repository.py`, `store.py` or the shared
DDL — they are what proves the cascades fire, that the upsert hands back the
existing record's id, and that a configured table name carries its child tables
with it.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) — install, systemd units, cron, Nginx, and
the order to do it in. Two things there are easy to get wrong and expensive:

- **Deploy and verify the resync first.** A day with no rows reads as "everyone
  is working", so an Emails job pointed at an unpopulated table is silently
  wrong rather than visibly broken.
- **Baseline before enabling the Emails cron** (`python baseline.py --yes`), or
  every approval that already exists gets commented on.

## Logging

Both log to the same SolarWinds (formerly Papertrail) collector as the
process-simplification integrations, with the same token. Filter by keyword:
`vfz_workschedule_db_resync`, `vfz_workschedule_emails`,
`vfz_workschedule_availability`.

One event is one line, the same shape from all three:

```
{keyword}: {task_id or timestamp} {message}: {details}
```

The second field is the Wrike task id when the run knows one, and the stamp's
integer microsecond epoch when it does not. Everything that is not the message
goes in `details` at the end, as JSON.

Every run logs three times — on start, on any error, and on finish with the
whole result as a JSON `details` object. `details.triggeredBy` separates a
dashboard click from the cron, which is the first thing worth knowing when a
day looks wrong. Set `[papertrail] mode` to `print_only` to develop against live
data without writing to the collector, or `do_nothing` to silence it.
