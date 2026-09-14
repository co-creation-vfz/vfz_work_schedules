# Work-schedule integrations — deployment

Three integrations, one MySQL database, one dashboard page, one `segredo.ini`.

| Integration | Port | Slug | Role | Called by |
|---|---|---|---|---|
| `vfz_workschedule_db_resync` | 5007 | `/vfz_workschedule_db_resync/` | Wrike work schedules → MySQL | cron, dashboard |
| `vfz_workschedule_emails` | 5008 | `/vfz_workschedule_emails/` | Comments on approvals whose approvers are off | cron, dashboard |
| `vfz_workschedule_availability` | 5009 | `/vfz_workschedule_availability/` | Serves "who is off today" | **Workato**, on every @tag |

Ports 5007–5009 are free alongside the process-simplification services, which
hold 5001–5006 (db_importer, folder_structure, assignee, asset_metadata,
brief_amendment, dashboard).

**5007 and 5008 bind to loopback and have no authentication** — only the
dashboard, on the same host, calls them. **5009 binds publicly and requires a
bearer token**, because Workato calls it from outside. Keep that distinction in
mind: it is the reason only one of the three needs Nginx.

**The resync feeds the emails.** Deploy and verify it first: a day with no rows
reads as "everyone is working", so an Emails service pointed at an unpopulated
table is silently wrong rather than visibly broken.

## Secrets

One file: `shared/segredo.ini`. Copy the template and fill in three values —
everything else already has a working default.

```bash
cp shared/segredo.ini.example shared/segredo.ini
$EDITOR shared/segredo.ini
```

| Setting | Where it comes from |
|---|---|
| `[wrike] api_token` | The Co-creation integration bot token — the same one in the dashboard's `segredo.ini` under `[wrike] api_token`. Verified working against `app-eu`. |
| `[database] password` | The RDS password. |
| `[papertrail] token` | The SolarWinds token — the same one the process-simplification integrations use. |
| `[availability] api_token` | Generate one: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. This is what Workato sends. The service **refuses to start** with it empty, because starting without it would publish the staff absence list. |

The VodafoneZiggo Wrike account is **EU-hosted**. Both default to
`https://app-eu.wrike.com/api/v4`; the US endpoint answers 401 for an EU token,
which reads as a bad token rather than a wrong host. Leave `api_base_url` alone.

There is **no bearer token to manage**. Both services bind to `127.0.0.1` and
are reachable only from this host, so there is nothing to authenticate — and
nothing to keep in step between two config files.

## Database

Both share `vfz-wrike-workschedule-v1` on
`vfz-wrike-integration.cj6hvdyvczrr.eu-north-1.rds.amazonaws.com`. Each creates
its tables at startup (idempotent `CREATE TABLE IF NOT EXISTS`), so nothing has
to be run by hand. To create them ahead of time, or review them first:

```bash
mysql -h vfz-wrike-integration.cj6hvdyvczrr.eu-north-1.rds.amazonaws.com \
      -u admin -p vfz-wrike-workschedule-v1 < schema.sql
```

`schema.sql` is generated from `shared/database_helpers.py`, which is what the
services actually run — so there is one definition, not two.

| Table | Written by | Read by |
|---|---|---|
| `work_schedule_non_working_days` | DB Resync | both |
| `approval_notifications` (+ `_non_working`, `_comments`) | Emails | Emails |
| `baselined_approvals` | Emails (baseline command) | Emails |

`work_schedule_non_working_days` is the contract between the two integrations.
It is `[database] non_working_table`, read by both from the same file, so they
cannot disagree about it any more.

## Install

One virtualenv at the root serves both:

```bash
cd /srv/vfz_work_schedule
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo cp vfz_workschedule_db_resync/deploy/*.service /etc/systemd/system/
sudo cp vfz_workschedule_emails/deploy/*.service /etc/systemd/system/
sudo cp vfz_workschedule_availability/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vfz-workschedule-db-resync
sudo systemctl enable --now vfz-workschedule-emails
sudo systemctl enable --now vfz-workschedule-availability
```

Confirm all three are up before wiring anything to them:

```bash
curl -s localhost:5007/hello
curl -s localhost:5008/hello
curl -s localhost:5009/hello
curl -s localhost:5007/vfz_workschedule_db_resync/status   # real DB round trip
curl -s -H "Authorization: Bearer $TOKEN" \
     localhost:5009/vfz_workschedule_availability/         # what Workato will get
```

## Cron — still required

The services are manual triggers. The scheduled runs are cron's, and installing
a service does not replace one — running only the services would mean nobody is
ever notified unless a person clicks.

```cron
CRON_TZ=Africa/Johannesburg
MAILTO=""

# DB Resync — nightly, before the Emails job's first run
0 3 * * *  /srv/vfz_work_schedule/vfz_workschedule_db_resync/scripts/run-resync.sh

# Emails — every ten minutes on weekdays. Use the crontab file for the full version.
*/10 8-17 * * 1-5  /srv/vfz_work_schedule/vfz_workschedule_emails/scripts/run-notifier.sh
```

Use `vfz_workschedule_emails/scripts/vfz-workschedule-emails.crontab` as-is: it
pins `CRON_TZ`, sets a usable `PATH`, and points both jobs at the wrapper
scripts rather than at an interpreter path.

**Do not name the interpreter in the crontab.** The wrappers find it themselves
— an explicit `RESYNC_PYTHON` / `NOTIFIER_PYTHON` / `VFZ_PYTHON` override first,
then `$REPO_ROOT/.venv`, then any other virtualenv at the repo root (identified
by its `pyvenv.cfg`, so a venv named something else — `env_work_schedule`, say —
is still found), then a per-integration one. They then verify the chosen
interpreter can actually import the dependencies before running anything.

That sequence exists because of a real outage: a crontab line hard-coding
`$REPO_ROOT/.venv/bin/python` on a box whose venv was named `env_work_schedule`
exited 127 before Python started. With `>/dev/null 2>&1` on the entry, cron
logged the `CMD` and nothing else happened — the table simply stopped being
updated, and the dashboard, which then only knew about runs in its own process,
had nothing to say about it either.

Never end a cron entry with `>/dev/null 2>&1`. The wrappers write their own log
under `<integration>/logs/`; discarding stderr on top of that only hides the
failures that happen before the wrapper gets a chance to log.

Pin `CRON_TZ` for both. The services decide "today" and the notification window
from `[emails] timezone`; the cron schedule is clock time, so without pinning it
the two drift apart at every DST change.

## Nginx — required for 5009 only

Workato is the one caller from outside the box, so the Availability API is the
one service that needs proxying:

```nginx
location /vfz_workschedule_availability/ {
    proxy_pass         http://127.0.0.1:5009/vfz_workschedule_availability/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    # Answered from memory in the normal case; this only has to cover a cache
    # miss, which is one small query.
    proxy_read_timeout 30s;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Serve it over **HTTPS**. The bearer token is in the `Authorization` header on
every call, and Workato is reaching it across the public internet.

The DB Resync (5007) and Emails (5008) services need **no Nginx at all**: they
bind to loopback and the dashboard talks to them directly. If you later want to
trigger either from off the box, widen `bind` in `segredo.ini` and put
authentication in front of it — do not do one without the other.

An earlier version of this design exposed `POST /v1/availability` on 5007 for a
Workato recipe. That endpoint was removed when the recipe was retired; the
Availability API on 5009 is its replacement, with a different contract (it
returns everyone who is off, rather than classifying a list you send it).

## Dashboard

The page is `/dashboard/workschedule` in
`vfz_process_simplification_integrations`. Its URLs come from `[integrations]`
in `dashboard_integration/segredo.ini`:

```ini
workschedule_db_resync_url = http://127.0.0.1:5007/vfz_workschedule_db_resync
workschedule_emails_url    = http://127.0.0.1:5008/vfz_workschedule_emails
```

Loopback, unlike the other integrations there, which go out to
`http://15.240.16.2/` and back through Nginx. For a call that never leaves the
host that round trip buys nothing and adds a config dependency — which is
exactly what made the first attempt at this fail with a 404 from Nginx.

## What a cron resync does to the table

`run_resync.py` with no arguments **replaces the whole table**: every row is
deleted and today's non-working users inserted, in one transaction. The table is
not a history — it holds only who is off right now. No stale rows, no orphans
from a user dropped off every schedule, and no accumulating past dates.
`--no-full-refresh` reconciles instead, if you ever need that.

Two things follow from this, both deliberate:

- **A run for one date replaces every date.** For the cron, whose window is
  today, that is exactly the point. It is also why the dashboard's resync
  button has no date fields: picking a past date would wipe today's answer out
  from under the Emails job, which is about to read it.
- **The replace is refused when the source was not read cleanly.** If a
  schedule tripped the blast-radius guard, or a user's weekday pattern could
  not be resolved, a blanket delete would erase a correct answer and report
  that person as working. The run falls back to reconciling and says so
  (`fullRefreshDeclined`) rather than doing it quietly.

The swap is `DELETE` + `INSERT` in one transaction, not `TRUNCATE`. `TRUNCATE`
is DDL in MySQL and commits implicitly, so a failure part-way through the
inserts would leave an empty table — and an empty table reads as "everyone is
working" to the Emails job.

## What the first resync should produce

Read the live account rather than trusting a number in a doc — the schedules
get edited, and a stale table here is worse than none:

```bash
cd vfz_workschedule_db_resync && python run_resync.py --dry-run
```

As of the last check, 224 assignments resolve cleanly and only one custom
schedule has members, so **most weekdays legitimately produce zero rows**. An
empty table on a day nobody is off is the right answer, not a failed run —
which is exactly why the Emails job distinguishes "nobody is off" from "the
resync never ran".

No schedule trips the blast-radius guard: the only reduced ones have 2 and 1
members, well under the 10-member minimum.

## Workato

Point the recipe at the Availability API, not at the database:

```
GET https://<host>/vfz_workschedule_availability/
Authorization: Bearer <[availability] api_token>
```

```json
{ "date": "2026-08-27", "count": 2,
  "userIds": ["KUATAIRI", "KUAP5OS3"],
  "users": [{ "userId": "KUATAIRI", "user": "Abigail Hlalele" }],
  "cached": true, "stale": false }
```

Most recipes only need `userIds` — "is the person being @tagged in this list".

Three things the recipe should handle:

- **`503` means retry, not "nobody is off".** The service returns 503 when it
  cannot read the database and has no cached answer. It never returns an empty
  list on failure, precisely so an outage cannot be mistaken for a quiet day.
- **`count: 0` is a normal answer.** Most weekdays nobody on a reduced schedule
  is off.
- **`stale: true`** means the database was unreachable and this is the last good
  answer for today. Usable; worth surfacing if you care.

Responses are cached for `[availability] cache_ttl_seconds` (60 by default) and
carry a matching `Cache-Control` header, so a burst of @tags costs one query
rather than hundreds. If you need a resync reflected immediately, call
`POST /vfz_workschedule_availability/invalidate`.

**Why not give Workato the database directly?** It was considered and rejected:
a direct connection cannot be cached, so every @tag would be a MySQL round trip;
it would require keeping RDS reachable from Workato's cloud; and it would bind
the recipe to column names that have already changed once. See the Availability
API's README for the full reasoning.

## Logging

Both log to the same SolarWinds (formerly Papertrail) collector as the
process-simplification integrations. Filter by keyword:

| Keyword | Source |
|---|---|
| `vfz_workschedule_db_resync` | the resync service and its cron |
| `vfz_workschedule_emails` | the Emails service and its cron |
| `vfz_workschedule_availability` | the Availability API |

Every run logs three times: on start, on any error, and on finish with the whole
result as a JSON `details` object. `details.triggeredBy` separates a dashboard
click from the cron.

The Availability API is the exception, deliberately: at one call per @tag, a log
line per request would cost more than the request it describes. It logs startup,
every error, and a rolled-up line every `[availability] log_every_requests`
calls carrying the cache hit rate.

Set `[papertrail] mode` to `print_only` to develop against live data without
writing to the collector, or `do_nothing` to silence it.

## Order of operations for a first deployment

1. Fill in `shared/segredo.ini` (three values).
2. Create the tables, or let the services do it at startup.
3. Start the DB Resync; check `/hello` and `.../status`.
4. Dry run, then live, and confirm the row count matches the table above:
   `cd vfz_workschedule_db_resync && ../.venv/bin/python run_resync.py --dry-run`
5. Install the resync cron.
6. Start the Emails service; check `/hello`.
7. **Baseline before enabling the Emails cron** —
   `cd vfz_workschedule_emails && ../.venv/bin/python baseline.py --yes`.
   Without this, every approval that already exists gets commented on. It is a
   one-time step, and the point of no return for the whole deployment.
8. Install the Emails cron.
9. Point the dashboard at loopback and confirm the page loads.
10. Generate `[availability] api_token`, start the Availability service, and
    check `/hello` then the authenticated endpoint on 5009.
11. Add the Nginx block for 5009 over HTTPS, and give Workato the URL and token.
12. Watch `cache.hitRate` on
    `/vfz_workschedule_availability/status` once Workato is live. A low rate
    means `cache_ttl_seconds` is too short for how often it is calling.
