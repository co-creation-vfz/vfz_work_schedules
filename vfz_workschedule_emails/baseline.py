"""Go-live snapshot: suppress every approval that already exists.

    python baseline.py           # preview
    python baseline.py --yes     # apply

Run this ONCE, immediately before enabling the cron job.

Why a snapshot rather than a date filter: Wrike approvals carry no creation
timestamp. The object exposes `updatedDate`, but that moves whenever anything
on the approval changes, so an approval opened weeks ago can show today's date.
There is no way to ask "was this raised after Monday?". Recording the ids that
exist right now is the only reliable way to tell old from new.

Everything recorded here is suppressed permanently, so a pre-existing approval
never generates a comment even months later. Approvals raised after the
snapshot behave normally.
"""
import argparse
import os
import sys

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from wrike_helpers import WrikeError

from config import Config, ConfigError
from notifier import (
    build_client,
    build_store,
    configure_logging,
    task_is_eligible,
)

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="baseline",
        description="Record existing approvals so only newly raised ones notify.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Apply. Without it this only previews."
    )
    parser.add_argument(
        "--remove",
        metavar="APPROVAL_ID",
        help="Un-suppress one approval so it can notify again.",
    )
    parser.add_argument(
        "--status", action="store_true", help="Show how many approvals are suppressed."
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_segredo()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # Point the shared logger at the collector before anything can log.
    configure_logging(config)

    store = build_store(config)

    try:
        store.ensure_schema()

        if args.status:
            print(f"{store.baseline_count()} approval(s) currently suppressed.")
            return 0

        if args.remove:
            removed = store.remove_baseline(args.remove)
            if removed:
                print(f"{args.remove} un-suppressed; it can notify again.")
            else:
                print(f"{args.remove} was not in the baseline.")
            return 0

        existing = store.baseline_count()
        if existing and not args.yes:
            print(
                f"WARNING: {existing} approval(s) are already suppressed. This looks "
                "like a second run.\nBaselining again would also suppress everything "
                "raised since the first run.\n"
            )

        client = build_client(config)

        print("Reading every pending approval visible to the token...")
        try:
            approvals = [
                a for a in client.iter_pending_approvals() if a.get("taskId")
            ]
        except WrikeError as exc:
            print(f"Failed to list approvals: {exc}", file=sys.stderr)
            return 1

        tasks = {
            t["id"]: t
            for t in client.get_tasks([a["taskId"] for a in approvals])
        }
        in_scope = [
            a
            for a in approvals
            if a["taskId"] in tasks and task_is_eligible(tasks[a["taskId"]], config)
        ]

        print(f"  {len(approvals)} pending, {len(in_scope)} in the target space")

        if not args.yes:
            print(
                f"\nPreview only. Would suppress {len(in_scope)} approval(s), so the "
                "first live\nrun comments only on approvals raised from now on."
            )
            print("Re-run with --yes to apply.")
            return 0

        added = sum(
            store.add_baseline(a["id"], a["taskId"]) for a in in_scope
        )
        print(
            f"\nSuppressed {added} new approval(s); "
            f"{store.baseline_count()} in the baseline in total."
        )
        print("Approvals raised from now on will notify normally.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
