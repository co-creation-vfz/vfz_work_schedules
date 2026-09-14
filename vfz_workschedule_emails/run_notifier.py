# =============================================================================
# run_notifier.py
# CLI entry point for the Workschedule Emails job. What the hourly cron runs.
#
#   python run_notifier.py [--dry-run] [--force] [--watch]
#
# Exit codes: 0 success, 1 a comment failed to post or the DB Resync is
# late, 2 bad configuration.
# =============================================================================
"""Run one off-day approver notifier pass."""
import argparse
import os
import sys

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import general_helpers
from database_helpers import DatabaseError
from wrike_helpers import WrikeError

from config import ConfigError
from notifier import run_from_env


def _log_startup_failure(message: str, exc: BaseException) -> int:
    """Report a failure that happened before the run could log anything itself.

    run() owns the start/finish pair. A configuration error, an unreachable
    database or a dead Wrike token all abort before that, and those are exactly
    the failures a cron job must not swallow. If segredo.ini is what failed the
    logger cannot have been configured, in which case this still prints and
    only the collector misses out.
    """
    general_helpers.log_error(
        general_helpers.make_stamp(), message, exc, {"stage": "startup"}
    )
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_notifier",
        description="Notify working approvers when a non-working approver is on a pending approval.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the comments that would be posted without posting or recording them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even outside the configured notification window.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Narrate each stage as it happens, without timestamps or module names.",
    )
    parser.add_argument(
        "--only-task",
        action="append",
        metavar="TASK_ID",
        help="Restrict to these task ids. Repeatable. Use for contained live tests.",
    )
    parser.add_argument(
        "--title-contains",
        metavar="TEXT",
        help="Restrict to tasks whose title contains TEXT, e.g. dydxtest.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    if args.watch:
        # logging.basicConfig(level=logging.INFO, format="%(message)s")
        pass
    else:
        # logging.basicConfig(
        # level=logging.DEBUG if args.verbose else logging.INFO,
        # format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        # )
        pass

    try:
        summary = run_from_env(
            force_window=args.force,
            dry_run=True if args.dry_run else None,
            only_task_ids=set(args.only_task) if args.only_task else None,
            title_contains=args.title_contains,
        )
    except ConfigError as exc:
        # Logged with the module defaults rather than the run's config: the
        # config is exactly what could not be read.
        print(f"Configuration error: {exc}", file=sys.stderr)
        _log_startup_failure("Configuration error, run abandoned", exc)
        return 2
    except WrikeError as exc:
        print(f"Wrike API error: {exc}", file=sys.stderr)
        _log_startup_failure("Wrike API error, run abandoned", exc)
        return 1
    except DatabaseError as exc:
        # Unreachable host, bad credentials, auth failure: a run that cannot
        # read the non-working list has failed, and should say so rather than
        # produce a traceback in the cron log.
        print(f"MySQL error: {exc}", file=sys.stderr)
        _log_startup_failure("MySQL error, run abandoned", exc)
        return 1

    # if args.watch:
        # data = summary.as_dict()
        # print("\n" + "-" * 70)
        # print(f"date {data['date']}   dry run: {data['dryRun']}")
        # if data["skippedReason"]:
            # print(f"STOPPED EARLY: {data['skippedReason']}")
        # if data["upstreamLate"]:
            # print("UPSTREAM LATE: the work-schedule job has written nothing today")
        # print(
            # f"non-working {data['nonWorkingUsers']} -> "
            # f"pending {data['pendingApprovals']} -> "
            # f"in scope {data['eligibleApprovals']} -> "
            # f"planned {data['commentsPlanned']} -> "
            # f"posted {data['commentsPosted']}  (failures {data['failures']})"
        # )
        # print("-" * 70)
    # else:
        # print(json.dumps(summary.as_dict(), indent=2, default=str))
    return 1 if (summary.failures or summary.upstream_late) else 0


if __name__ == "__main__":
    sys.exit(main())
