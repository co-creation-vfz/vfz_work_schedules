# Workschedule Emails — the off-day approver notifier

Runs hourly. When someone with a Wrike work schedule is off today and sits on a
pending approval in the Co-Creation Flow space, the working approvers on that
same approval get an @mention telling them so.

"Emails" is the user-facing name. This job posts a Wrike comment tagging the
people who need to know; what they receive is Wrike's notification email.
Nothing here sends mail directly.

- [How it works](#how-it-works)
- [Notification rules](#notification-rules)
- [Tables](#tables)
- [Configuration](#configuration)
- [Running it](#running-it)
- [The trigger service](#the-trigger-service)
- [Logging](#logging)
- [Testing](#testing)
- [Go-live: baseline first](#go-live-baseline-first)
- [Retracting a bad notification](#retracting-a-bad-notification)
- [Deployment](#deployment)
- [When the upstream job fails](#when-the-upstream-job-fails)
- [Behaviour outside the notification window](#behaviour-outside-the-notification-window)
- [Known limitations](#known-limitations)

Commands below are written as `python`. Use the project interpreter, which lives
one level up since both integrations share an environment:
`..\.venv\Scripts\python.exe` on Windows, `../.venv/bin/python` on Linux. Run
them from this directory.

## Layout

Flat, matching the other VFZ integrations. There is no package to import — each
entry point is a script you run directly.

```
vfz_workschedule_emails/
├── vfz_workschedule_emails_main.py   FastAPI service (port 5008)
├── run_notifier.py                   CLI — what the hourly cron runs
├── notifier.py                       who to tell, and once only
├── store.py                          all SQL for this job's tables
├── comments.py                       comment text and @mention markup
├── config.py                         reads ../shared/segredo.ini, validates it
├── baseline.py                       go-live snapshot — run once
├── retract.py                        undo notifications that should not have gone
├── diagnose.py                       read-only: what can the token see?
├── testkit.py                        seed and inspect test data
├── deploy/                           systemd unit
├── scripts/                          cron wrappers (Linux + Windows)
└── tests/
```

Four helpers are shared with the DB Resync integration and live in `../shared/`:

| Module | What this job uses it for |
|---|---|
| `general_helpers.py` | SolarWinds logging, the run stamp |
| `database_helpers.py` | The MySQL connection pool, and **all** the DDL |
| `wrike_helpers.py` | The Wrike client. This job's old `wrike.py` moved here; the resync now uses its contact lookup too, which is what stopped that service doing its own unretried version |
| `config_helpers.py` | Reading and validating `segredo.ini` |

## How it works

1. Reads today's non-working users from the `work_schedule_non_working_days`
   table in MySQL. That table is written by a **separate upstream job** —
   `vfz_workschedule_db_resync`, the Workschedule DB Resync integration — and
   this one only reads it. It is literally the same table, not a copy.
2. `GET /approvals?statuses=[Pending]&pendingApprovers=[...]`, paginated, in
   batches of 50 user ids. `pendingApprovers`, not `approvers`: an approval only
   matters if the absent person still owes a decision. Batched because a long id
   list makes the query string long enough for Wrike to reject it, and a 4xx is
   fatal by design.
3. Batch-fetches the tasks and keeps only those that are `status == "Active"`,
   have the space id `MQAAAAEEuHGf` in `parentIds`/`superParentIds`, and are not
   under the recycle bin `IEAFXAOHI7777776`.
4. Splits the approvers **whose decision is still Pending** into non-working and
   working. Anyone who already approved or rejected is ignored entirely.
5. Drops approvals recorded in `baselined_approvals` as pre-go-live.
6. Consults `approval_notifications` to decide who still needs telling.
7. Posts the comment, then records it.

## Notification rules

| Situation | Result |
|---|---|
| First time this approval is flagged | One comment tagging **all** working approvers, listing all non-workers |
| Working approver added to the approval later | Their own comment, tagging **only them** |
| Already-notified approver, a **new** non-worker joins | Their own follow-up naming **only the new** non-worker |
| Already-notified approver, nothing changed | Nothing |
| Every approver is off, task names a working project lead | Comment tagging the **project lead** |
| Every approver is off, no working lead | Comment tagging the fallback contact (Co-Creation Support) |

Dedup is keyed on `(approvalId, notifiedApproverId)` with no date component, so
each person hears about each approval once, not once a day.

The three tiers are tried in order — working approvers, then the project lead
off the task's configured custom field, then the fallback contact — and each
notification records which tier it used in `recipient_role` (`approver`, `lead`
or `fallback`). A lead who is themselves off is skipped rather than tagged on
their day off, and if the fallback contact is off too the approval is recorded
as unnotifiable rather than tagging nobody.

Non-working approvers are named in plain text, never @mentioned: mentioning them
would notify someone on their day off, which is the opposite of the point.

## Tables

Everything lives in the `vfz-wrike-workschedule-v1` database on
`vfz-wrike-integration.cj6hvdyvczrr.eu-north-1.rds.amazonaws.com` (user `admin`,
port 3306), reached through `mysql-connector-python`. Five tables, with
deliberately different lifecycles.

`Store.ensure_schema()` creates them all on every run with
`CREATE TABLE IF NOT EXISTS`, so a fresh environment needs no manual migration
step. `../schema.sql` holds the same DDL for a DBA who would rather create them
ahead of time, or review them first — it is generated from
`../shared/database_helpers.py`, so there is one definition rather than two.

The DDL builders there take the table names as **arguments**. That is not
decoration: an earlier version baked the defaults into a static registry, so
pointing `notifications_table` at a staging table created its two child tables
under the *default* names and every subsequent query hit a table that did not
exist. The live SQL suite caught it; the names now travel together.

All SQL this job runs lives in `store.py`, and every statement is
parameterised. Table names are the exception — an identifier cannot be a bind
parameter — so they are validated instead, both in `config.py` and again in the
`Store` constructor.

**`work_schedule_non_working_days`** — the upstream job owns this; this job only
reads it:

| Column | Notes |
|---|---|
| `id` | `INT AUTO_INCREMENT PRIMARY KEY` |
| `user_id` | Wrike contact id, e.g. `KUAAAAAA` |
| `user_name` | e.g. `Jane Smith`. Read from here rather than Wrike, so the message still reads correctly for a contact the token cannot see |
| `work_schedule_title` | e.g. `Mon/Wed off` |
| `work_schedule_id` | e.g. `IEAxxxxx` |
| `date` | `DATE` — this job filters on it |
| `seeded_by` | `"testkit"` on a row the test tooling wrote, `NULL` on a real one |
| `created_at` / `updated_at` | stamped by the column defaults |

`UNIQUE KEY (user_id, date)` stops a double-entered day at the source.

This job creates this table too, purely so a fresh environment works whichever
service starts first. The resync owns it.

If no rows match today's date the run posts nothing and says so, rather than
falling back to stale rows. See
[When the upstream job fails](#when-the-upstream-job-fails).

**`approval_notifications`** — this job owns this. It never expires: it is what
makes the "once per approval, per approver" rule hold across hourly runs and
across days.

| Column | Notes |
|---|---|
| `id` | `INT AUTO_INCREMENT PRIMARY KEY` |
| `approval_id` | e.g. `IEAxxxxxxxxxxxxx` |
| `notified_approver_id` | e.g. `KUBBBBBB` |
| `task_id` | e.g. `IEAxxxxx` |
| `notified_approver_name` | e.g. `John Doe` |
| `recipient_role` | `approver`, `lead` for the project lead, or `fallback` for Co-Creation Support |
| `first_notified_at` | written once, never moved |
| `last_notified_at` | refreshed on every write |

`UNIQUE KEY (approval_id, notified_approver_id)` is the real dedup guarantee, not
the planning logic. Even if two runs overlap, the second write for the same pair
updates the first row instead of silently duplicating it.

**`approval_notifications_non_working`** — which non-working approvers each
notification has already covered. `notification_id` + `approver_id`, unique
together, `ON DELETE CASCADE`.

This was an array on the Mongo document. It is a child table now because it is a
growing *set*: a later run compares today's non-workers against it to tell
"already covered" from "a new non-worker joined this approval". The unique key is
what gives it set semantics — re-covering the same approver is a no-op.

**`approval_notifications_comments`** — the Wrike comment ids a notification has
posted, so a retraction can delete the comments it actually made.
`notification_id` + `comment_id`, `ON DELETE CASCADE`.

Also an array before, and also a child table now — but deliberately **not**
unique, because a corrected re-notification adds a second comment to the same
record and a retraction has to be able to delete both.

**`baselined_approvals`** — approvals that already existed at go-live, recorded
once and suppressed permanently. `approval_id` (unique), `task_id`,
`baselined_at`. See [Go-live: baseline first](#go-live-baseline-first).

### The record shape did not change

`store.py` aliases every column back to the camelCase keys the planning logic and
the run summary already used — `approvalId`, `notifiedApproverId`,
`nonWorkingApproverIds`, `commentIds`, `lastNotifiedAt` — and reassembles the two
child tables into the lists they replaced. So the move from documents to rows
stopped at the storage boundary: `notifier.py` reads exactly what it always did.

That is also why the reads use one query per table rather than a join. A join
would repeat each parent row once per covered approver and once per comment, and
the planning logic wants those sets whole, keyed the way it looks them up.

## Configuration

One file for both integrations: **`../shared/segredo.ini`**. They share a
database, a table, a Wrike account and a log collector, so two copies meant a
credential rotation had to be done twice — with the second copy discovered only
when something failed.

```bash
cp ../shared/segredo.ini.example ../shared/segredo.ini
$EDITOR ../shared/segredo.ini
```

**Only three values need filling in:** `[wrike] api_token`,
`[database] password` and `[papertrail] token`. Everything else has a working
default in `config.py`, so a checkout runs with those three set.

**`[wrike]`**

| Option | Default | Notes |
|---|---|---|
| `api_token` | *required* | Co-Creation integration bot account |
| `api_base_url` | `https://app-eu.wrike.com/api/v4` | EU datacenter. Use `https://www.wrike.com/api/v4` only for a US-bound token |
| `request_timeout` | `30` | Seconds |
| `space_id` | `MQAAAAEEuHGf` | Co-Creation Flow |
| `recycle_bin_id` | `IEAFXAOHI7777776` | Tasks under it are deleted, and skipped |
| `project_lead_field_id` | `IEAFXAOHJUAEGYZF` | Wrike custom field id holding the project lead, read off the approval's task. Tried before the fallback contact when no approver on the approval is working. Leads who are themselves off are dropped rather than tagged. Empty disables the lookup — and disabling it is silent, so every stuck approval then goes straight to the fallback; the run logs a line when it is unset |
| `fallback_contact_id` | `KUARF54C` | Co-Creation Support; tagged when no approver is working and no lead is available |

**`[database]`**

| Option | Default | Notes |
|---|---|---|
| `host` | the VFZ work-schedule RDS endpoint | |
| `name` | `vfz-wrike-workschedule-v1` | |
| `user` | `admin` | |
| `password` | *required* | Not whitespace-trimmed: a password may legitimately begin or end with a space, and silently trimming one turns a correct secret into an authentication failure nobody can explain |
| `port` | `3306` | |
| `timeout_seconds` | `30` | Connection timeout |
| `non_working_table` | `work_schedule_non_working_days` | **The contract with the DB Resync.** Both integrations read this same file, so they cannot disagree about it any more — leave it alone |
| `notifications_table` | `approval_notifications` | The two child tables are named after it, so pointing this at a scratch table moves them with it |
| `baseline_table` | `baselined_approvals` | Approvals suppressed as pre-go-live |

**`[emails]`**

| Option | Default | Notes |
|---|---|---|
| `bind` | `127.0.0.1` | Loopback: the dashboard is the only caller and it runs on the same host |
| `port` | `5008` | |
| `log_keyword` | `vfz_workschedule_emails` | The search term that isolates this job's log lines. Stable forever |
| `timezone` | `Africa/Johannesburg` | Defines "today" and the window; match `CRON_TZ`. Rejected at startup if not a known zone |
| `notify_start_hour` | `7` | inclusive; 0-23, and must be before `notify_end_hour` |
| `notify_end_hour` | `18` | exclusive; 0-24, where 24 means the window never closes |
| `upstream_grace_minutes` | `10` | Wait after `notify_start_hour` before declaring the DB Resync late |
| `dry_run` | `false` | `1`/`true`/`yes`/`on` or `0`/`false`/`no`/`off`. Anything else is a config error rather than a silent `false`, because a typo here is the difference between a dry run and real comments |

**`[papertrail]`**

| Option | Default | Notes |
|---|---|---|
| `url` | the shared collector endpoint | See [Logging](#logging) |
| `token` | *required* | |
| `mode` | *(empty)* | Empty sends to SolarWinds; `print_only` prints locally and sends nothing; `do_nothing` silences the logger |

Every one of these is validated when the run starts. A bad value stops the job
with a `Configuration error:` line and exit code 2, rather than surfacing as a
traceback partway through or as a window that silently never opens. The three
table names get their own check — letters, digits and underscores only — because
they reach SQL as identifiers rather than as bind parameters, so a typo has to
fail at startup rather than become a syntax error mid-run.

There is **no bearer token** for the service. It binds to loopback and is
reachable only from this host, so there is nothing to authenticate and nothing
to keep in step between two config files.

## Running it

One entry point for the job itself:

```bash
python run_notifier.py
```

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would be posted. Posts nothing, records nothing |
| `--force` | Run even outside the notification window |
| `--watch` | Narrate each stage without timestamps or module names. What the wrappers use |
| `--only-task TASK_ID` | Restrict to these task ids. Repeatable. For contained live tests |
| `--title-contains TEXT` | Restrict to tasks whose title contains TEXT |
| `--verbose` / `-v` | Debug logging |

Exit codes: `0` success, `1` a comment failed to post, the upstream job is late,
or MySQL could not be reached, `2` bad configuration. A MySQL failure prints
`MySQL error: …` and exits 1 rather than producing a traceback in the cron log: a
run that cannot read the non-working list has failed, and should say so.

The three failures that happen *before* the run can log for itself — a config
error, a dead Wrike token, an unreachable database — are reported to SolarWinds
from the CLI, so a cron run that never got started is still visible in the
collector.

Supporting commands, each documented in its own section below:

| Command | Purpose |
|---|---|
| `python diagnose.py` | Read-only: what can the token see? |
| `python baseline.py` | Go-live snapshot |
| `python retract.py` | Undo notifications that should not have gone out |
| `python testkit.py` | Seed and inspect test data |

## The trigger service

This job is a cron job first. `vfz_workschedule_emails_main.py` adds a manual
trigger on top of it, on **port 5008**, bound to `127.0.0.1`, which is what the
VFZ dashboard's Work Schedules page calls. It does not replace the cron.

```bash
python vfz_workschedule_emails_main.py
# or, for development
uvicorn vfz_workschedule_emails_main:app --reload --port 5008
```

| Endpoint | Purpose |
|---|---|
| `POST /vfz_workschedule_emails/` | Start a run. Returns `accepted` or `ignored` immediately |
| `GET /vfz_workschedule_emails/status` | What the last run did, plus the window, timezone, space and tables it runs against |
| `GET /vfz_workschedule_emails/data?date=&historyLimit=` | Today's non-working users, the recent notification history, the baselined count, and an `upstreamLate` flag. Read-only. `historyLimit` is bounded 1-200 |
| `GET /hello` | Heartbeat. Touches no dependency, so it answers even when Wrike and MySQL are both down |

The trigger body is entirely optional, and its defaults match the cron — a live
run, inside the window:

| Field | Default | Effect |
|---|---|---|
| `dryRun` | `false` | Show what would be posted. Posts nothing, records nothing |
| `force` | `false` | Run outside the notification window |
| `onlyTaskIds` | `[]` | Restrict to these Wrike task ids |
| `titleContains` | *(none)* | Restrict to tasks whose title contains this text |
| `triggeredBy` | `api` | Recorded on the result and on every log line, so a dashboard click is distinguishable from the cron |

A run takes long enough — every pending approval for every non-working user —
that holding the HTTP connection open would time out. So the trigger hands the
work to a background task and the caller polls `status`. That is what the
dashboard does.

**A second trigger while one is in flight is refused, not queued.** This is the
same reason the cron wrapper takes a lock: two concurrent runs could both read an
empty notification history and both decide the same comment is needed. Queueing
would not help either, since by the time the queued run started the first would
already have written the same answer.

`lastRun` on the status endpoint is in-memory, so `null` means "not since this
process started", not "never" — and the hourly cron runs in its own process, so
its runs never appear there at all. SolarWinds holds the durable history for
both.

Whatever the outcome, the run's own failure is recorded rather than lost: an
exception escaping a background task would otherwise be logged by Starlette and
disappear, so a Wrike error, a MySQL error and an unexpected crash each land on
`lastRun` with a `failed` status.

The service calls the same `notifier.run_from_env` as the CLI. That is not just
tidiness: the notify-once rule lives entirely in the notification history, and
two code paths writing it differently would double-comment on a live Wrike task.

A bad `segredo.ini` stops the service at import with exit code 2 and the reason
on stderr, rather than a traceback — which is what shows up in
`journalctl -u vfz-workschedule-emails`.

### The dashboard

`vfz_process_simplification_integrations` has a **Work Schedules** page at
`/dashboard/workschedule`. It triggers this job and the upstream resync, and
shows who is off today alongside the notification history — the two together,
because an empty "off today" list is either a quiet day or a resync that never
ran, and only seeing both halves tells you which.

The dashboard runs on the same host and calls both services on loopback, so
there is no reverse proxy to configure. `../DEPLOYMENT.md` covers the wiring.

## Logging

Both work-schedule integrations report to the same SolarWinds (formerly
Papertrail) collector as the `vfz_process_simplification_integrations` suite,
with the same token, so everything VFZ lands in one place. The transport lives in
`../shared/general_helpers.py`.

One event is one line, the same shape every time:

```
{keyword}: {task_id or timestamp} {message}: {details}
```

```
vfz_workschedule_emails: IEATASKID Off-day approver notifier finished: 2 planned, 2 posted, 0 failed: {"caller": "run", "commentsPosted": 2, "...": "..."}
```

The second field is the Wrike task id when the run knows one, and the stamp's
integer microsecond epoch when it does not — so an event always traces back to
something, and a run with no task in hand still sorts and range-filters
numerically. Everything that is not the message goes in `details` at the end, as
JSON, never as a field of its own. Stdout still gets the human-readable
one-liner the sibling integrations print.

Three log points are guaranteed, which is what makes the collector worth
watching:

- **the run starting**, with the window, scope and tables it is about to use;
- **every error**, with the exception type and the line it came from;
- **the run finishing**, carrying the whole `RunSummary` as `details` — the
  funnel counts, the planned comments, the failures.

`details.triggeredBy` separates a dashboard click from the cron, which is the
first thing worth knowing when a day looks wrong.

The logger never raises. A failure to reach the collector prints to stderr and is
otherwise ignored: losing a log line must not take down the run.

Set `[papertrail] mode = print_only` to develop against live data without writing
to the collector, or `do_nothing` to silence it entirely (which the tests do).

## Testing

Five layers, cheapest first. Nothing below layer 4 posts a comment.

### Layer 1 — unit tests (offline, no credentials)

```bash
python -m unittest discover -s tests
```

131 tests, 20 skipped. They cover task eligibility, every notification rule, the
go-live baseline, dry-run behaviour, `segredo.ini` loading and validation, the
table-name checks, the Wrike client's retry and batching decisions, the testkit's
seed guard, and the trigger service. These use fake Wrike and store doubles, so
they prove the logic, not the integration.

The 20 skipped are `tests/test_store_mysql.py`, which covers the SQL itself and
skips unless a test database is configured. Run them against a **scratch**
database; each test empties the tables it uses, and they are named `test_*` so
they cannot collide with production even if the database is shared:

```bash
TEST_MYSQL_HOST=... TEST_MYSQL_DATABASE=... TEST_MYSQL_USER=... TEST_MYSQL_PASSWORD=... \
  python -m unittest tests.test_store_mysql
```

They are worth running after any change to `store.py` or the shared DDL: they are
what proves the cascades fire, that the upsert hands back the existing record's
id rather than inserting a second one, that a configured table name carries its
two child tables with it, and that `seeded_by` cannot land on an upstream row.

The store doubles implement the same interface as the MySQL `Store` and return
records in the same shape, so the planning logic under test is exactly what
ships. `tests/test_service.py` covers the parts of the HTTP wrapper that only
matter under failure: that the dedup lock refuses a second run, that every
failure path is recorded rather than lost, that a crashed run releases the lock
instead of wedging the service, and that the history limit is bounded so one
request cannot pull the whole table.

### Layer 2 — diagnose (read-only, needs the Wrike token only)

```bash
python diagnose.py
```

This is the answer to "can it see anything?". It verifies the token, counts the
pending approvals visible to it, applies the space/status/recycle-bin filters,
and prints the approvers for each in-scope approval. It writes nothing anywhere,
and touches no database.

Add `--show-skipped` to see what was filtered out and why. It ends by printing
ready-to-paste commands for building a test case.

### Layer 3 — dry run

A dry run only means something once the database has a non-working user who is
actually on a pending approval. Seed one using an id from layer 2:

```bash
python testkit.py seed --user KUARF3YR
python run_notifier.py --dry-run --force
```

Seeded rows carry `seeded_by = "testkit"`, and `seed` refuses to write over a row
the upstream job already owns.

#### Reading the output

The summary is a funnel. The first zero tells you where it stopped:

| Field | Zero means |
|---|---|
| `nonWorkingUsers` | Nothing seeded for today, or the wrong date/timezone. `skippedReason` says so. |
| `pendingApprovals` | Those users are not approvers on any Pending approval anywhere. |
| `eligibleApprovals` | They have approvals, but every task failed the space/active/recycle-bin filter. |
| `baselined` (non-zero) | Approvals were suppressed as pre-go-live. Expected after baselining. |
| `commentsPlanned` | Approvals are in scope but nobody needs telling — usually already notified. Check `testkit.py history`. |
| `commentsPosted` | Expected to be 0 in a dry run. |

A working dry run prints a line per planned comment with the text as a reader
would see it, plus a `details` array in the JSON carrying both the plain text and
the raw HTML with the mention anchors. The same summary reaches SolarWinds as the
`details` object on the closing log line, so a dry run left on a schedule is
reviewable from the collector.

`skippedReason` being non-null means the run stopped early and did nothing.

### Layer 4 — one real comment

The mention markup can only be verified by posting: a wrong `rel` attribute
still posts, still reports success, and still notifies nobody. Do it on a task
you control:

1. Create a throwaway task in the Co-Creation Flow space.
2. Add two approvers: yourself and one colleague.
3. Seed the colleague as non-working.
4. Run without `--dry-run`:

```bash
python run_notifier.py --force
```

Then check in Wrike that the comment appeared, that the mention is a real blue
mention rather than literal text, and that you received a notification.

Use `--only-task` or `--title-contains` to keep a live test contained: seeding a
real person surfaces every approval they sit on, so an unrestricted live test
can comment on production tasks. The trigger service takes the same two
narrowings as `onlyTaskIds` and `titleContains`, for the same reason.

### Layer 5 — the rules that only appear over time

Run these against the same throwaway task.

**Notify once.** Run the same command again. Expect `commentsPlanned: 0` and no
second comment.

**New working approver.** Add a third approver in Wrike, run again. Expect one
comment tagging only the new person.

**New non-working approver.** Seed a second approver as non-working, run again.
Expect one comment per already-notified approver naming only the newly-off
person. Run once more — expect silence.

**No working approver.** Seed *every* approver on an approval as non-working,
reset the history, run again. Expect one comment tagging the project lead if the
task names a working one, or Co-Creation Support if not.

Inspect state between steps:

```bash
python testkit.py history
```

Replay a scenario by clearing that approval's history:

```bash
python testkit.py reset --approval IEAFXAOHMECBTZYQ
```

Clearing a notification record cascades to its covered-approver and comment-id
rows, so there is no orphaned child state to clean up separately.

### Cleanup

```bash
python testkit.py unseed
```

`unseed` only deletes rows carrying `seeded_by = "testkit"`, so it cannot remove
anything the real upstream job wrote — the marker filter is never optional.
`seed` protects the other half of that guarantee: it refuses to write over a row
the upstream job owns, rather than restamping it as seeded and leaving `unseed`
free to delete real data. The column is written on insert only, so belt and
braces: the marker can only ever land on a row `seed` itself created.

Comments already posted to Wrike need
[retract](#retracting-a-bad-notification), not `unseed`.

### Before scheduling it

Install the schedule with `dry_run = true` under `[emails]` and leave it a day.
The wrapper logs every run to `logs/notifier.log`, and every run also reports its
summary to SolarWinds, so you can review a full day of planned comments against
real data before it posts anything.

Then set `dry_run = false` and confirm the first live run in the log.

## Go-live: baseline first

Run this **once, immediately before enabling the schedule**:

```bash
python baseline.py --yes
```

Without it the first run comments on every approval already open — of the order
of a hundred in the space, which for a dozen absent people would be a few
hundred comments in one hour. `diagnose.py` gives the current count. Baselining
records those ids and suppresses them permanently, so only approvals raised
after go-live ever notify.

Wrike approvals carry no creation timestamp. `updatedDate` exists but moves on
every change, so an approval opened weeks ago can show today's date. A recorded
snapshot is the only reliable way to separate old from new.

`--status` shows how much is suppressed; `--remove <approvalId>` un-suppresses
one approval. Do not run the baseline twice: the second run would also suppress
everything raised in between.

## Retracting a bad notification

If a wrong comment goes out, deleting it in Wrike is not enough: the
notification record still says that person has been told, so the corrected
message can never be sent. Retract clears both.

```bash
python retract.py --approval IEAxxxx
```

Previews by default; add `--yes` to apply. `--task` retracts everything on a
task, `--keep-comments` clears only the records. The comment ids it deletes come
from `approval_notifications_comments`, which is why that table is not unique on
`comment_id`: a record that commented twice must have both comments removed.

## Deployment

`../DEPLOYMENT.md` has the full picture for both integrations — the shared
database, the crons, and the dashboard wiring. What follows is what is specific
to this job.

Two things run here, and they are not alternatives:

- **the hourly cron**, `python run_notifier.py`, which is what actually notifies
  anyone;
- **the trigger service**, `python vfz_workschedule_emails_main.py`, which only
  runs when someone asks it to.

Installing the service does **not** replace the cron. Running only the service
would mean nobody is ever notified unless a person clicks a button.

### Installing the service

```bash
sudo cp deploy/vfz-workschedule-emails.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vfz-workschedule-emails
journalctl -u vfz-workschedule-emails -f
```

Adjust `User`, `WorkingDirectory` and the venv path for the box. Config comes
from `../shared/segredo.ini`, so nothing secret belongs in the unit file.

### Linux — the cron

```bash
sudo cp scripts/vfz-workschedule-emails.crontab /etc/cron.d/vfz-workschedule-emails
sudo chmod 644 /etc/cron.d/vfz-workschedule-emails
```

Or `crontab -e` for a service user, using the same content. The last run starts
at 17:05, Monday to Friday, hourly at 5 past. That sits inside the 07:00-18:00
window the code enforces, so the schedule is the binding constraint and the
window is the backstop.

`scripts/run-notifier.sh` handles what cron does not give you:

- **Environment.** Cron supplies almost no PATH, no profile, no working
  directory. The wrapper resolves the project root and the venv itself.
- **Single instance.** `flock` on a lock file. A run that overruns the hour must
  not be joined by the next one: two concurrent runs could both see an empty
  notification history and post the same comment twice.
- **Logging.** Appends to `logs/notifier.log`, since cron output otherwise
  vanishes into mail nobody reads.

Make it executable once: `chmod +x scripts/run-notifier.sh`.

### Windows

No cron, so use Task Scheduler with `scripts/run-notifier.ps1`. The registration
command is in the header of that file. It takes a named mutex instead of flock,
and writes the same log.

It launches Python with `Start-Process` and redirects the child's streams to
files, which looks roundabout next to the shell version. It is not optional:
Windows PowerShell 5.1 turns a native command's stderr into error records when
it is merged into the pipeline with `2>&1`, and under
`$ErrorActionPreference = "Stop"` that aborts the script. Python writes to
stderr, so the pipeline form died on the very first line — losing the log, the
exit status, and the `finished, exit N` line this document tells you to grep for.

### Timezone

Cron runs in the system timezone, usually UTC, while the notifier reads
`[emails] timezone` to decide both "today" and its own window. Set `CRON_TZ` to
match, or the two drift apart at every DST change. The supplied crontab already
pins it to `Africa/Johannesburg`.

### The window is enforced twice

The scheduler decides when the process starts; `notify_start_hour` /
`notify_end_hour` stop a mistimed or manual run from commenting at 03:00. Keep
the two in agreement. `--force` bypasses the in-code check for testing, and so
does the trigger service's `force` flag.

### Log rotation

`logs/notifier.log` grows without bound. On Linux:

```
/srv/vfz_work_schedule/vfz_workschedule_emails/logs/notifier.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
```

Windows has no equivalent installed; the log needs trimming by hand or by a
separate task.

### Monitoring

**SolarWinds is the first place to look.** Filter on `vfz_workschedule_emails`
and every run appears as a start line, any errors, and a finish line carrying the
whole summary — including runs that failed before they could do anything, and
runs triggered from the dashboard. See [Logging](#logging).

The local log is the backstop for cron-level problems the run itself cannot
report: a wrapper that never launched Python, a lock that was already held. The
process exits non-zero when any comment fails to post, the upstream job is late,
or MySQL is unreachable, and the wrapper logs the exit status on every run. A run
that posts nothing is normal and exits 0. Grep for `FATAL`, `ERROR`,
`UPSTREAM LATE`, or `finished, exit [^0]` to find genuine failures:

```bash
grep -E "UPSTREAM LATE|finished, exit [^0]" logs/notifier.log
```

## When the upstream job fails

An empty `work_schedule_non_working_days` for today is ambiguous: either nobody
is off, or the DB Resync did not run. Past `notify_start_hour` plus
`upstream_grace_minutes` the run stops treating it as a quiet day and treats it
as a failure:

- logs `UPSTREAM LATE: ...` to SolarWinds, with the date, the grace period and
  the table it read
- sets `upstreamLate` in the summary
- exits non-zero

No Wrike comment is posted. There is no approval to attach one to at that point,
and a standing monitoring task was not wanted.

The dashboard surfaces the same condition as an `upstreamLate` flag on the Work
Schedules page, next to a button that runs the resync — which is the fix.

## Behaviour outside the notification window

The run exits before touching Wrike or posting anything, and stores no state.
Nothing is queued, so the next in-window run rebuilds the whole picture from that
day's data. A working approver added at 20:00 is picked up at 07:00 the next
morning against *that* morning's non-working list — if the off person is back at
work by then, no comment is sent, which is correct.

## Known limitations

**A quiet day looks like a failed upstream job.** The check above cannot tell
"nobody is off today" from "the DB Resync did not run", so unless that job writes
something on a day with no absences, every quiet day and public holiday produces
an `UPSTREAM LATE` error on every run.

**Nothing caps the number of comments in one run.** The run posts every comment
it plans. That is fine in normal operation, but if the baseline or the
notification history were lost, the same code would post hundreds — `retract` is
the only remedy, after the fact.

**The single-instance lock is per entry point, not global.** The cron wrappers
hold `flock` or a mutex; the trigger service holds its own in-process lock. They
do not know about each other, and running `python run_notifier.py` by hand
bypasses both, so a manual run overlapping a scheduled one can post the same
comment twice. The unique key on `(approval_id, notified_approver_id)` still
prevents duplicate *records* — the losing run's write updates the existing row
rather than adding one — but the comment has already been posted by then.
