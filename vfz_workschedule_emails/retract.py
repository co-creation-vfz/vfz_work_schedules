"""Undo notifications that should never have gone out.

    python retract.py --approval IEAxxxx
    python retract.py --task MAAAAAxxxx --yes

A retraction does two things, and both matter:

  1. Deletes the comments this job posted, so the false claim stops being
     visible on the task.
  2. Deletes the notification records, so the approval is treated as never
     notified. Without this the correction can never be sent - dedup would
     suppress it forever.

Previews by default. Nothing is deleted without --yes.
"""
import argparse
import os
import sys
from typing import List

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from wrike_helpers import WrikeError

from config import Config, ConfigError
from notifier import build_client, build_store, configure_logging

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="retract",
        description="Delete posted comments and clear their notification records.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--approval", action="append", metavar="ID", help="Retract by approval id. Repeatable."
    )
    target.add_argument(
        "--task", action="append", metavar="ID", help="Retract everything on a task. Repeatable."
    )
    parser.add_argument(
        "--yes", action="store_true", help="Actually delete. Without it this only previews."
    )
    parser.add_argument(
        "--keep-comments",
        action="store_true",
        help="Clear the records but leave the Wrike comments in place.",
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
    client = build_client(config)

    try:
        approval_ids = list(set(args.approval)) if args.approval else []
        task_ids = list(set(args.task)) if args.task else []
        records = store.notification_records(
            approval_ids=approval_ids, task_ids=task_ids
        )

        if not records:
            print("No notification records match. Nothing to retract.")
            return 0

        comment_ids: List[str] = []
        print(f"{len(records)} notification record(s) match:\n")
        for record in records:
            ids = record.get("commentIds") or []
            comment_ids.extend(ids)
            print(f"  approval {record['approvalId']}  task {record.get('taskId')}")
            print(
                f"    notified {record.get('notifiedApproverName')} "
                f"({record['notifiedApproverId']})"
            )
            print(f"    comments {ids or '(none recorded)'}")

        if args.keep_comments:
            comment_ids = []

        print(
            f"\nWould delete {len(comment_ids)} comment(s) and "
            f"{len(records)} record(s)."
        )

        if not args.yes:
            print("\nPreview only. Re-run with --yes to apply.")
            return 0

        deleted_comments = 0
        for comment_id in comment_ids:
            try:
                client.delete_comment(comment_id)
                deleted_comments += 1
                print(f"  deleted comment {comment_id}")
            except WrikeError as exc:
                # A comment removed by hand already is fine; keep going so the
                # records still get cleared.
                print(f"  could not delete comment {comment_id}: {exc}", file=sys.stderr)

        deleted_records = store.delete_notifications(
            approval_ids=approval_ids, task_ids=task_ids
        )
        print(
            f"\nDeleted {deleted_comments} comment(s) and "
            f"{deleted_records} record(s)."
        )
        print("These approvals will be treated as never notified on the next run.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
