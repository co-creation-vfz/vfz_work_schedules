"""Read-only inspection of what the notifier can see. Touches no database.

    python diagnose.py

Lists every Pending approval the token can see, says which ones the notifier
would consider eligible and why the rest are filtered out, and prints the
contact ids you need to seed a test with.
"""
import argparse
import os
import sys
from typing import Any, Dict, List

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from wrike_helpers import WrikeError

from config import Config, ConfigError
from notifier import (
    approver_ids,
    build_client,
    configure_logging,
    task_is_eligible,
)

def rejection_reason(task: Dict[str, Any], config: Config) -> str:
    if task.get("status") != "Active":
        return f"task status is {task.get('status')}, not Active"
    parents = set(task.get("parentIds") or []) | set(task.get("superParentIds") or [])
    if config.recycle_bin_id in parents:
        return "task is in the recycle bin"
    if config.space_id not in parents:
        return "task is not in the target space"
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="diagnose",
        description="Show the pending approvals the notifier can see.",
    )
    parser.add_argument(
        "--limit", type=int, default=25, help="Max eligible approvals to print."
    )
    parser.add_argument(
        "--show-skipped",
        action="store_true",
        help="Also list approvals filtered out, with the reason.",
    )
    args = parser.parse_args(argv)

    try:
        config = Config.from_segredo()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # Point the shared logger at the collector before anything can log.
    configure_logging(config)

    client = build_client(config)

    print(f"Wrike host : {config.wrike_base_url}")
    print(f"Space      : {config.space_id}")

    try:
        me = client._request("GET", "/contacts", params={"me": "true"})["data"][0]
        print(f"Token user : {me.get('firstName','')} {me.get('lastName','')} ({me['id']})")
    except WrikeError as exc:
        print(f"Token check FAILED: {exc}", file=sys.stderr)
        return 1

    print("\nFetching pending approvals...")
    try:
        approvals = list(client.iter_pending_approvals())
    except WrikeError as exc:
        print(f"Failed to list approvals: {exc}", file=sys.stderr)
        return 1

    task_approvals = [a for a in approvals if a.get("taskId")]
    folder_approvals = len(approvals) - len(task_approvals)
    print(
        f"  {len(approvals)} pending approval(s) visible to this token "
        f"({folder_approvals} on folders, ignored by design)"
    )

    if not task_approvals:
        print("\nNothing to inspect. If you expected results, the token may not have")
        print("visibility of the space.")
        return 0

    tasks = {t["id"]: t for t in client.get_tasks([a["taskId"] for a in task_approvals])}

    eligible: List[Dict[str, Any]] = []
    skipped: List[tuple] = []
    for approval in task_approvals:
        task = tasks.get(approval["taskId"])
        if task is None:
            skipped.append((approval, None, "task could not be fetched"))
            continue
        if task_is_eligible(task, config):
            eligible.append(approval)
        else:
            skipped.append((approval, task, rejection_reason(task, config)))

    print(f"  {len(eligible)} in scope, {len(skipped)} filtered out")

    contact_ids = set()
    for approval in eligible[: args.limit]:
        contact_ids.update(approver_ids(approval))
    names = client.get_contact_names(contact_ids) if contact_ids else {}

    print("\n" + "=" * 78)
    print("IN SCOPE - the notifier would consider these")
    print("=" * 78)

    for approval in eligible[: args.limit]:
        task = tasks[approval["taskId"]]
        print(f"\n{task.get('title', '(untitled)')}")
        print(f"  task     {task['id']}   {task.get('permalink', '')}")
        print(f"  approval {approval['id']}   due {approval.get('dueDate', '-')}")

        print("  approvers:")
        for decision in approval.get("decisions") or []:
            aid = decision.get("approverId", "?")
            marker = "  " if decision.get("status") != "NotRequired" else " *"
            print(
                f"   {marker} {aid:<10} {names.get(aid, aid):<28} {decision.get('status')}"
            )

    if len(eligible) > args.limit:
        print(f"\n... and {len(eligible) - args.limit} more (raise --limit to see them)")

    print("\n  * NotRequired decisions are ignored by the notifier.")

    if args.show_skipped and skipped:
        print("\n" + "=" * 78)
        print("FILTERED OUT")
        print("=" * 78)
        for approval, task, reason in skipped:
            title = task.get("title", "(untitled)") if task else approval["taskId"]
            print(f"  {approval['id']}  {title[:45]:<45}  {reason}")

    if eligible:
        multi = [(a, approver_ids(a)) for a in eligible]
        multi = [(a, ids) for a, ids in multi if len(ids) >= 2]
        single = len(eligible) - len(multi)

        print("\n" + "=" * 78)
        print("TO BUILD A TEST CASE")
        print("=" * 78)
        print(
            f"{len(multi)} in-scope approval(s) have 2+ approvers; {single} have one.\n"
            "Only the first group can produce a comment: mark one approver off and\n"
            "the rest become the recipients. Single-approver approvals go quiet.\n"
        )

        if multi:
            approval, ids = multi[0]
            print(f"Approval {approval['id']} on task {approval['taskId']}:")
            print(f"  python testkit.py seed --user {ids[0]}")
            print("  python run_notifier.py --dry-run --force")
            print(f"  (recipients would be: {', '.join(ids[1:])})\n")


    return 0


if __name__ == "__main__":
    sys.exit(main())
