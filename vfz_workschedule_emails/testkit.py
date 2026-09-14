"""Seed and inspect test data so the notifier can be exercised end to end.

    python testkit.py seed --user KUAAAAAA
    python testkit.py list
    python testkit.py history
    python testkit.py unseed
    python testkit.py reset --approval IEAxxxx

Rows written here carry `seededBy: "testkit"`. `unseed` only ever deletes rows
carrying that marker, so it cannot touch data written by the real upstream job.
"""
import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from wrike_helpers import WrikeError

from config import Config, ConfigError
from notifier import build_client, build_store, configure_logging

MARKER = "testkit"


def _today(config: Config) -> str:
    return datetime.now(ZoneInfo(config.timezone)).date().isoformat()


def _store(config: Config):
    store = build_store(config)
    # Every subcommand reads or writes one of these tables, and the tool is
    # normally the first thing pointed at a fresh environment.
    store.ensure_schema()
    return store


def cmd_seed(config: Config, store, args) -> int:
    date = args.date or _today(config)

    # Seeding on top of an upstream row would restamp it as seeded, and unseed
    # would then delete real data. The marker is only a safe guarantee if this
    # command can never apply it to somebody else's row.
    existing = store.non_working_row(args.user, date)
    if existing is not None and existing.get("seeded_by") != MARKER:
        print(
            f"{args.user} already has a {date} row written by the upstream job, "
            "not by this tool.\nSeeding would overwrite it and mark it as "
            "seeded, and unseed would then delete\nreal data. Refusing.\n\n"
            "That user is already recorded as non-working for that date, so "
            "there is nothing\nto seed: run the notifier as it is, or pick "
            "another --user or --date.",
            file=sys.stderr,
        )
        return 2

    name = args.name

    if not name:
        client = build_client(config)
        try:
            name = client.get_contact_names([args.user]).get(args.user, args.user)
        except WrikeError as exc:
            print(f"Could not look up the contact name: {exc}", file=sys.stderr)
            name = args.user

    # seeded_by is written on insert only: belt and braces with the check
    # above, so the marker can only ever land on a row this command created.
    store.seed_non_working(
        user_id=args.user,
        user_name=name,
        date_iso=date,
        work_schedule_title=args.schedule_title,
        work_schedule_id=args.schedule_id,
        marker=MARKER,
    )
    print(f"Seeded {name} ({args.user}) as non-working on {date}")
    print("\nNext:  python run_notifier.py --dry-run --force")
    return 0


def cmd_unseed(config: Config, store, args) -> int:
    deleted = store.delete_seeded_non_working(
        MARKER, user_id=args.user or "", date_iso=args.date or ""
    )
    print(f"Deleted {deleted} seeded row(s). Real rows are untouched.")
    return 0


def cmd_list(config: Config, store, args) -> int:
    date = args.date or _today(config)
    rows = store.non_working_rows_for(date)
    if not rows:
        print(f"No non-working users recorded for {date}.")
        print("The notifier would exit here without posting anything.")
        return 0

    print(f"Non-working users for {date}:")
    for row in rows:
        origin = "seeded" if row.get("seeded_by") == MARKER else "upstream"
        user_id = row.get("user_id") or ""
        print(f"  {user_id:<10} {row.get('user_name') or '':<28} [{origin}]")
    return 0


def cmd_history(config: Config, store, args) -> int:
    rows = store.notification_records(
        approval_ids=[args.approval] if args.approval else [], limit=args.limit
    )
    if not rows:
        print("No notifications recorded yet.")
        return 0

    print(f"{len(rows)} notification record(s), newest first:\n")
    for row in rows:
        print(f"  approval {row['approvalId']}  task {row.get('taskId')}")
        print(
            f"    notified {row.get('notifiedApproverName')} "
            f"({row['notifiedApproverId']}) as {row.get('recipientRole')}"
        )
        print(f"    covers non-workers {row.get('nonWorkingApproverIds')}")
        print(f"    first {row.get('firstNotifiedAt')}  last {row.get('lastNotifiedAt')}")
        print(f"    comments {row.get('commentIds')}\n")
    return 0


def cmd_reset(config: Config, store, args) -> int:
    if not args.approval and not args.all:
        print("Pass --approval <id>, or --all --yes to clear everything.", file=sys.stderr)
        return 2
    if args.all and not args.yes:
        print(
            "--all deletes the entire notification history, which means every "
            "approver gets re-notified on the next run. Re-run with --yes if "
            "that is what you want.",
            file=sys.stderr,
        )
        return 2

    deleted = store.delete_notifications(
        approval_ids=[] if args.all else [args.approval], all_records=bool(args.all)
    )
    print(f"Deleted {deleted} notification record(s).")
    print("The next run will treat these approvals as never notified.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="testkit")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="Mark a Wrike user non-working for a date.")
    p.add_argument("--user", required=True, help="Wrike contact id, e.g. KUAAAAAA")
    p.add_argument("--name", help="Override the name; looked up from Wrike otherwise.")
    p.add_argument("--date", help="YYYY-MM-DD. Defaults to today in the configured tz.")
    p.add_argument("--schedule-title", default="testkit seeded")
    p.add_argument("--schedule-id", default="TESTKIT")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("unseed", help="Remove rows this tool created.")
    p.add_argument("--user")
    p.add_argument("--date")
    p.set_defaults(func=cmd_unseed)

    p = sub.add_parser("list", help="Show non-working users for a date.")
    p.add_argument("--date")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("history", help="Show what has already been notified.")
    p.add_argument("--approval")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("reset", help="Delete notification history so it re-sends.")
    p.add_argument("--approval")
    p.add_argument("--all", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)

    args = parser.parse_args(argv)

    try:
        config = Config.from_segredo()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # Point the shared logger at the collector before anything can log.
    configure_logging(config)

    store = _store(config)
    try:
        return args.func(config, store, args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
