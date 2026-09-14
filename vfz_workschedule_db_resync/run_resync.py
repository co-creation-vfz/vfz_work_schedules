# =============================================================================
# run_resync.py
# CLI entry point for the Workschedule DB Resync. What the nightly cron runs.
#
#   # preview what would be written, from Wrike (the default source)
#   python run_resync.py --dry-run
#
#   # what the cron runs: today plus the configured horizon
#   python run_resync.py
#
#   # today only, or an explicit window
#   python run_resync.py --today
#   python run_resync.py --from 2026-09-01 --to 2026-09-30
#
#   # print the raw Wrike work schedule payload and exit
#   python run_resync.py --dump-raw
#
#   # from a local schedules.json, e.g. testing without a Wrike token
#   python run_resync.py --source json --dry-run
#
# A cron run REPLACES the table: every existing row is deleted and this run's
# dates inserted, in one transaction, so the table is exactly what Wrike says
# now with no stale rows and no orphans. Pass --no-full-refresh to reconcile
# instead. The swap is atomic, so a failed run leaves the previous answer in
# place rather than an empty table.
#
# This is a thin wrapper: the run itself lives in resync.py, which the
# service's trigger route calls too. One code path, so a manual resync from
# the dashboard cannot behave differently from the cron.
#
# Weekends are never written. Schedule exclusions (leave, public holidays) are
# out of scope: only the weekly pattern is used.
#
# Exit codes: 0 success, 1 nothing to do, 2 bad arguments, 3 one or more
# schedules could not be read, 4 a schedule's non-working count changed too
# abruptly to write unattended, 5 the database could not be reached, 6 the
# schedule source could not be read.
# =============================================================================

import argparse
import json
import os
import sys
from datetime import date as Date

# ---------------------------------------------------------------------------
# Add shared helpers to the module search path
# ---------------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import general_helpers

import resync
from config import Config, ConfigError
from sources import WrikeScheduleSource


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_resync",
        description=(
            "Expand Wrike work schedules into non-working dates and write them "
            "to MySQL."
        ),
    )
    parser.add_argument(
        "--source",
        choices=("json", "wrike"),
        default="wrike",
        help=(
            "Where to read the weekday patterns from (default: wrike). Use "
            "--source json for a local schedules.json, e.g. testing without "
            "a Wrike token."
        ),
    )
    parser.add_argument(
        "--file",
        default=None,
        help="Schedule file for --source json (default: the configured one).",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        type=Date.fromisoformat,
        default=None,
        help="First date of the window (default: today).",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=Date.fromisoformat,
        default=None,
        help="Last date of the window (default: a horizon_days-long window).",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="Force a one-day window: today only, in the configured timezone.",
    )
    parser.add_argument(
        "--no-full-refresh",
        dest="full_refresh",
        action="store_false",
        help=(
            "Reconcile the table instead of replacing it: upsert this run's "
            "dates and remove only the stale rows of users the source returned. "
            "Leaves rows for users dropped from every schedule behind unless "
            "--purge-orphans is also given. The default is a full replace."
        ),
    )
    parser.add_argument(
        "--purge-orphans",
        action="store_true",
        help=(
            "Also delete rows in the window for users the source did not "
            "return at all. Only safe when the source lists every user."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        help="Print the raw Wrike work schedule payload and exit.",
    )
    parser.add_argument(
        "--min-schedule-size-for-guard",
        type=int,
        default=None,
        help="Override the configured blast-radius guard minimum size.",
    )
    parser.add_argument(
        "--max-non-working-fraction",
        type=float,
        default=None,
        help="Override the configured blast-radius guard fraction.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        config = Config.from_segredo()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return resync.EXIT_BAD_ARGUMENTS

    general_helpers.papertrail_url = config.papertrail_url
    general_helpers.papertrail_token = config.papertrail_token
    general_helpers.system_identifier = config.log_keyword
    general_helpers.log_mode = config.log_mode

    if args.dump_raw:
        if args.source != "wrike":
            print("--dump-raw applies to --source wrike.", file=sys.stderr)
            return resync.EXIT_BAD_ARGUMENTS
        source = resync.build_source(config, "wrike")
        assert isinstance(source, WrikeScheduleSource)  # narrowed by the check
        print(json.dumps(source.fetch_raw(), indent=2))
        return resync.EXIT_OK

    d_result = resync.run_resync(
        config,
        source=args.source,
        file=args.file,
        date_from=args.date_from,
        date_to=args.date_to,
        today_only=args.today,
        dry_run=args.dry_run,
        purge_orphans=args.purge_orphans,
        full_refresh=args.full_refresh,
        min_schedule_size_for_guard=args.min_schedule_size_for_guard,
        max_non_working_fraction=args.max_non_working_fraction,
        triggered_by="cron",
    )

    for problem in d_result.get("problems") or []:
        print(f"  - {problem}", file=sys.stderr)

    if d_result.get("summaryText"):
        print(("DRY RUN\n" if args.dry_run else "") + d_result["summaryText"])
    else:
        print(d_result["message"], file=sys.stderr)

    return int(d_result["exitCode"])


if __name__ == "__main__":
    raise SystemExit(main())
