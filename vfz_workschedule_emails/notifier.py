"""Core orchestration for the hourly non-working-approver notifier.

Flow
----
1. Read today's non-working users from MySQL (written by the upstream job).
2. Ask Wrike for every Pending approval where one of them still owes a decision.
3. Keep only approvals whose task is an active, non-deleted task in the
   Co-Creation Flow space.
4. Drop approvals recorded as pre-go-live in baselined_approvals.
5. Work out who still needs telling, using the approval_notifications history.
6. Post the comments, then record what was sent.

Nothing is ever queued between runs. Each run recomputes from that day's data,
so a notification deferred out of business hours can never go out stale.

Every run reports to SolarWinds three times over: once as it starts, once for
any error, and once when it finishes, carrying the whole run summary as a JSON
object. That is what makes an hourly cron job auditable without reading the
host's log file.
"""
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

# Shared helpers live one directory up.
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import general_helpers
from wrike_helpers import WrikeClient, WrikeError  # noqa: F401 — re-exported

import comments as comment_builder
from config import Config

if TYPE_CHECKING:  # keeps the planning logic importable without the DB driver
    from store import Store

# Only approvers who still owe a decision matter. Someone who has already
# approved or rejected has done their part, so their absence blocks nobody and
# they have no reason to be told about anyone else. This also excludes
# NotRequired, which Wrike uses for approvers whose input is not expected.
PENDING_DECISION = "Pending"


@dataclass
class PlannedComment:
    approval_id: str
    task_id: str
    recipient_ids: List[str]
    non_working_ids: List[str]          # who the message names
    covered_non_working_ids: List[str]  # what to record as now-known
    text: str
    reason: str
    # "approver" or "fallback". Recorded so the notification history does not
    # describe the support contact as an approver on the approval.
    recipient_role: str = "approver"


@dataclass
class RunSummary:
    ran_at: str = ""
    date: str = ""
    skipped_reason: Optional[str] = None
    non_working_users: int = 0
    upstream_late: bool = False
    pending_approvals: int = 0
    eligible_approvals: int = 0
    baselined: int = 0
    comments_planned: int = 0
    comments_posted: int = 0
    unnotifiable: int = 0
    failures: int = 0
    dry_run: bool = False
    details: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ranAt": self.ran_at,
            "date": self.date,
            "skippedReason": self.skipped_reason,
            "nonWorkingUsers": self.non_working_users,
            "upstreamLate": self.upstream_late,
            "pendingApprovals": self.pending_approvals,
            "eligibleApprovals": self.eligible_approvals,
            "baselined": self.baselined,
            "commentsPlanned": self.comments_planned,
            "commentsPosted": self.comments_posted,
            "unnotifiable": self.unnotifiable,
            "failures": self.failures,
            "dryRun": self.dry_run,
            "details": self.details,
        }


# -- filtering ------------------------------------------------------------


def within_notification_window(now: datetime, config: Config) -> bool:
    return config.notify_start_hour <= now.hour < config.notify_end_hour


def upstream_is_late(now: datetime, config: Config) -> bool:
    """True once the work-schedule job has had its grace period and written nothing."""
    deadline = now.replace(
        hour=config.notify_start_hour, minute=0, second=0, microsecond=0
    ) + timedelta(minutes=config.upstream_grace_minutes)
    return now >= deadline


def task_is_eligible(task: Dict[str, Any], config: Config) -> bool:
    """Active task, in the target space, not sitting in the recycle bin."""
    if task.get("status") != "Active":
        return False

    parents: Set[str] = set(task.get("parentIds") or [])
    supers: Set[str] = set(task.get("superParentIds") or [])

    if config.recycle_bin_id in parents | supers:
        return False

    # A task parented directly to the space root shows the space in parentIds
    # rather than superParentIds, so check both.
    return config.space_id in parents | supers


def approver_ids(approval: Dict[str, Any]) -> List[str]:
    """Approvers whose decision on this approval is still outstanding."""
    return [
        decision["approverId"]
        for decision in approval.get("decisions") or []
        if decision.get("approverId")
        and decision.get("status") == PENDING_DECISION
    ]


def _parse_contact_field(value: Any) -> List[str]:
    """Contact ids out of one Wrike custom field value.

    Wrike is inconsistent about how a Contacts field comes back: a JSON array
    encoded as a string, a bare id, or a comma-separated list. Parse all three
    rather than guess, and return [] for anything unrecognised so a surprising
    shape degrades to the fallback contact instead of raising mid-run.
    """
    if not value:
        return []
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                return []
            candidates = parsed if isinstance(parsed, list) else []
        else:
            candidates = text.split(",")
    else:
        return []

    ids = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            ids.append(candidate.strip())
    return ids


def project_lead_ids(task: Dict[str, Any], config: Config) -> List[str]:
    """The project lead(s) named on this task, in Wrike custom field order.

    Returns [] when the lookup is not configured, the field is absent from the
    task, or its value is empty — all of which mean "no lead", and are handled
    by falling through to the fallback contact.
    """
    field_id = config.project_lead_field_id
    if not field_id:
        return []
    for custom_field in task.get("customFields") or []:
        if custom_field.get("id") == field_id:
            return _parse_contact_field(custom_field.get("value"))
    return []


# -- planning -------------------------------------------------------------


def plan_for_approval(
    approval: Dict[str, Any],
    task: Dict[str, Any],
    non_working: Dict[str, str],
    existing: Dict[tuple, dict],
    names: Dict[str, str],
    config: Config,
    unnotifiable: Optional[List[Dict[str, str]]] = None,
) -> List[PlannedComment]:
    """Decide which comments this approval still needs."""
    approval_id = approval["id"]
    task_id = approval["taskId"]

    all_approvers = approver_ids(approval)
    non_working_here = [aid for aid in all_approvers if aid in non_working]
    if not non_working_here:
        return []

    working_here = [aid for aid in all_approvers if aid not in non_working]

    def give_up(reason: str) -> List[PlannedComment]:
        # Logging is silenced project-wide; the guard clauses below are not
        # logging and must stay live. Without them a stuck approval tags an
        # empty contact id, or tags the fallback contact on their day off.
        # logger.warning("Approval %s on task %s: %s", approval_id, task_id, reason)
        if unnotifiable is not None:
            unnotifiable.append(
                {"approvalId": approval_id, "taskId": task_id, "reason": reason}
            )
        return []

    if working_here:
        recipients = working_here
        build = comment_builder.approver_notice
        role = "approver"
    else:
        # Nobody on the approval is working, so it is stuck. Try the task's
        # project lead before the shared support account: the lead owns the
        # work and is the closer escalation. Leads who are themselves off are
        # dropped rather than tagged on their day off.
        working_leads = [
            lead for lead in project_lead_ids(task, config) if lead not in non_working
        ]
        if working_leads:
            recipients = working_leads
            build = comment_builder.project_lead_notice
            role = "lead"
        else:
            # No lead configured, none named on the task, or every named lead
            # is off. Fall back to the support account as before.
            fallback = config.fallback_contact_id
            if not fallback:
                return give_up(
                    "every approver is not working today and no fallback contact is set"
                )
            if fallback in non_working:
                return give_up(
                    "every approver is not working today and so is the fallback contact"
                )
            recipients = [fallback]
            build = comment_builder.no_working_approver_notice
            role = "fallback"

    approval_has_history = any(key[0] == approval_id for key in existing)
    current_nw = set(non_working_here)

    unnotified = sorted(r for r in recipients if (approval_id, r) not in existing)
    plans: List[PlannedComment] = []

    if not approval_has_history:
        # First ever notification for this approval: one comment, everyone tagged.
        plans.append(
            PlannedComment(
                approval_id=approval_id,
                task_id=task_id,
                recipient_ids=unnotified,
                non_working_ids=sorted(current_nw),
                covered_non_working_ids=sorted(current_nw),
                text=build(unnotified, sorted(current_nw), names),
                reason="initial",
                recipient_role=role,
            )
        )
        return plans

    # Someone joined the approval after we first commented: one comment, all
    # of them tagged. They are all being told the same thing, and three people
    # joining at once is three notifications on a task nobody asked to be
    # noisier -- the same reason the first notification is a single comment.
    if unnotified:
        plans.append(
            PlannedComment(
                approval_id=approval_id,
                task_id=task_id,
                recipient_ids=unnotified,
                non_working_ids=sorted(current_nw),
                covered_non_working_ids=sorted(current_nw),
                text=build(unnotified, sorted(current_nw), names),
                reason="new_recipient",
                recipient_role=role,
            )
        )

    # A non-working approver joined an approval we have already reported on.
    #
    # Grouped by what each recipient has not yet been told, not by recipient:
    # everyone who is owed the same news gets one comment between them. The
    # grouping key matters -- two recipients can be owed different news, when
    # one was notified on a later run than the other, and merging those would
    # tell somebody about a person they were already told about.
    d_by_newly_off: Dict[tuple, List[str]] = {}
    for recipient in sorted(recipients):
        doc = existing.get((approval_id, recipient))
        if not doc:
            continue
        newly_off = current_nw - set(doc.get("nonWorkingApproverIds") or [])
        if not newly_off:
            continue
        d_by_newly_off.setdefault(tuple(sorted(newly_off)), []).append(recipient)

    for t_newly_off, l_recipients in sorted(d_by_newly_off.items()):
        plans.append(
            PlannedComment(
                approval_id=approval_id,
                task_id=task_id,
                recipient_ids=l_recipients,
                non_working_ids=list(t_newly_off),
                covered_non_working_ids=sorted(current_nw),
                text=build(l_recipients, list(t_newly_off), names),
                reason="new_non_working_approver",
                recipient_role=role,
            )
        )

    return plans


# -- run ------------------------------------------------------------------


def run(
    config: Config,
    client: WrikeClient,
    store: "Store",
    force_window: bool = False,
    only_task_ids: Optional[Set[str]] = None,
    title_contains: Optional[str] = None,
) -> RunSummary:
    now = datetime.now(ZoneInfo(config.timezone))
    today = now.date().isoformat()
    summary = RunSummary(ran_at=now.isoformat(), date=today, dry_run=config.dry_run)
    stamp = general_helpers.make_stamp(mode=config.log_mode)

    if not config.project_lead_field_id:
        general_helpers.log_message(
            stamp,
            "Project lead lookup is not configured, so an approval with no "
            "working approver will go straight to the fallback contact rather "
            "than to its lead",
            {"fallbackContactId": config.fallback_contact_id or ""},
        )

    general_helpers.log_message(
        stamp,
        f"Off-day approver notifier starting for {today}",
        {
            "ranAt": summary.ran_at,
            "date": today,
            "dryRun": config.dry_run,
            "forceWindow": force_window,
            "window": f"{config.notify_start_hour:02d}:00-"
            f"{config.notify_end_hour:02d}:00 {config.timezone}",
            "spaceId": config.space_id,
            "database": config.mysql_database,
            "nonWorkingTable": config.non_working_table,
            "onlyTaskIds": sorted(only_task_ids) if only_task_ids else [],
            "titleContains": title_contains or "",
            # The escalation chain, spelled out. An unset lead field silently
            # sends every stuck approval straight to the fallback contact,
            # which reads as working behaviour -- so the run says which chain
            # it is actually about to use rather than leaving it to be
            # inferred from who got tagged.
            "projectLeadFieldId": config.project_lead_field_id or "",
            "fallbackContactId": config.fallback_contact_id or "",
        },
    )

    def finish(reason: str) -> RunSummary:
        """One exit point for the closing log line, whatever ended the run."""
        general_helpers.log_message(
            stamp, f"Off-day approver notifier finished: {reason}", summary.as_dict()
        )
        return summary

    if not force_window and not within_notification_window(now, config):
        summary.skipped_reason = (
            "outside notification window "
            f"{config.notify_start_hour:02d}:00-{config.notify_end_hour:02d}:00 "
            f"{config.timezone}"
        )
        # logger.info("Skipping run: %s", summary.skipped_reason)
        return finish("outside the notification window, nothing evaluated")

    non_working = store.non_working_users_for(today)
    summary.non_working_users = len(non_working)
    if not non_working:
        summary.skipped_reason = (
            f"no non_working_users rows for {today} - upstream job may not have run"
        )
        # An empty collection is ambiguous: nobody is off today, or the upstream
        # job failed. Past the grace period we treat it as a failure and say so,
        # because silently posting nothing looks identical to working correctly.
        if upstream_is_late(now, config):
            summary.upstream_late = True
            # An upstream job that has written nothing is the one failure this
            # run cannot work around: with no non-working users it posts
            # nothing, which is indistinguishable from a quiet day unless it is
            # said out loud.
            general_helpers.log_message(
                stamp,
                f"UPSTREAM LATE: no non-working rows for {today}, more than "
                f"{config.upstream_grace_minutes} minutes after "
                f"{config.notify_start_hour:02d}:00. No absence notifications "
                f"will go out today until the work-schedule job writes.",
                {
                    "date": today,
                    "graceMinutes": config.upstream_grace_minutes,
                    "notifyStartHour": config.notify_start_hour,
                    "nonWorkingTable": config.non_working_table,
                },
            )
            # Log-only by design: the non-zero exit is what cron and any log
            # monitoring pick up. No Wrike comment, because with no non-working
            # users there is no approval and no task to attach one to.
            # logger.error(
                # "UPSTREAM LATE: work-schedule job has written nothing for %s, more "
                # "than %d minutes after %02d:00. No absence notifications will go "
                # "out today until it does.",
                # today, config.upstream_grace_minutes, config.notify_start_hour,
            # )
        else:
            # logger.warning("%s", summary.skipped_reason)
            pass
        return finish(summary.skipped_reason or "no non-working users today")

    for user_id, user_name in sorted(non_working.items(), key=lambda kv: kv[1]):
        # logger.info("  off today: %s (%s)", user_name or "?", user_id)
        pass
    # logger.info(
        # "Step 1/6: %d non-working user(s) for %s", len(non_working), today
    # )

    # logger.info("Step 2/6: asking Wrike for their pending approvals")
    # Folder-level approvals carry folderId instead of taskId; out of scope.
    raw = list(client.iter_pending_approvals(list(non_working)))
    approvals = [approval for approval in raw if approval.get("taskId")]
    summary.pending_approvals = len(approvals)
    # logger.info(
        # "  %d pending approval(s) carry one of those users (%d folder approvals ignored)",
        # len(approvals), len(raw) - len(approvals),
    # )
    if not approvals:
        # logger.info("No pending task approvals for today's non-working users")
        return finish("no pending task approvals for today's non-working users")

    # logger.info("Step 3/6: checking each task is active and in the space")
    tasks = client.get_tasks([approval["taskId"] for approval in approvals])
    eligible_tasks = {
        task["id"]: task for task in tasks if task_is_eligible(task, config)
    }
    permalinks = {t["id"]: t.get("permalink", "") for t in tasks}

    # Test-only narrowing. Seeding a real person surfaces every approval they
    # sit on, so a live test without this would comment on production tasks.
    if only_task_ids is not None:
        eligible_tasks = {
            tid: task for tid, task in eligible_tasks.items() if tid in only_task_ids
        }
        # logger.warning("RESTRICTED to task ids: %s", ", ".join(sorted(only_task_ids)))
    if title_contains:
        needle = title_contains.lower()
        eligible_tasks = {
            tid: task
            for tid, task in eligible_tasks.items()
            if needle in (task.get("title") or "").lower()
        }
        # logger.warning("RESTRICTED to task titles containing %r", title_contains)
    dropped = len(approvals)
    approvals = [a for a in approvals if a["taskId"] in eligible_tasks]
    summary.eligible_approvals = len(approvals)
    # logger.info(
        # "  %d approval(s) in scope, %d dropped (deleted, archived or another space)",
        # len(approvals), dropped - len(approvals),
    # )
    if not approvals:
        return finish("no approvals in scope after task eligibility filtering")

    # Approvals that predate go-live never notify. Wrike gives approvals no
    # creation timestamp, so a recorded snapshot is the only way to distinguish
    # "already open when we switched on" from "raised since".
    # logger.info("Step 4/6: dropping approvals that predate go-live")
    baselined = store.baselined_ids([a["id"] for a in approvals])
    if baselined:
        approvals = [a for a in approvals if a["id"] not in baselined]
        summary.baselined = len(baselined)
        # logger.info(
            # "  %d approval(s) suppressed as pre-go-live; %d remain",
            # len(baselined), len(approvals),
        # )
        if not approvals:
            return finish("every approval in scope predates go-live")

    # logger.info("Step 5/6: working out who still needs telling")
    existing = store.notifications_for_approvals([a["id"] for a in approvals])
    # logger.info("  %d existing notification record(s) for these approvals", len(existing))

    contact_ids: Set[str] = set()
    if config.fallback_contact_id:
        contact_ids.add(config.fallback_contact_id)
    for approval in approvals:
        contact_ids.update(approver_ids(approval))
        # Leads are resolved here too, otherwise a lead mention renders with a
        # raw contact id instead of a name.
        contact_ids.update(
            project_lead_ids(eligible_tasks[approval["taskId"]], config)
        )
    names = client.get_contact_names(contact_ids)
    # The DB name is authoritative for non-workers; fill in anything Wrike could
    # not resolve (e.g. a contact the token cannot see).
    for user_id, db_name in non_working.items():
        if db_name and names.get(user_id, user_id) == user_id:
            names[user_id] = db_name

    plans: List[PlannedComment] = []
    unnotifiable: List[Dict[str, str]] = []
    for approval in approvals:
        plans.extend(
            plan_for_approval(
                approval,
                eligible_tasks[approval["taskId"]],
                non_working,
                existing,
                names,
                config,
                unnotifiable=unnotifiable,
            )
        )
    summary.unnotifiable = len(unnotifiable)
    if unnotifiable:
        # logger.info(
            # "  %d approval(s) have nobody who can be told; see unnotifiable in the summary",
            # len(unnotifiable),
        # )
        # Prefixed rather than replaced: the reason text is the only record of
        # why nobody could be told, and the summary is where it gets read.
        summary.details.extend(
            {**entry, "reason": f"unnotifiable: {entry['reason']}"}
            for entry in unnotifiable
        )
    summary.comments_planned = len(plans)
    # logger.info(
        # "  %d comment(s) to send across %d approval(s)",
        # len(plans), len({p.approval_id for p in plans}),
    # )

    # logger.info(
        # "Step 6/6: %s",
        # "dry run, nothing will be posted" if config.dry_run else "posting comments",
    # )

    for plan in plans:
        summary.details.append(
            {
                "approvalId": plan.approval_id,
                "taskId": plan.task_id,
                "permalink": permalinks.get(plan.task_id, ""),
                "reason": plan.reason,
                "recipients": [names.get(r, r) for r in plan.recipient_ids],
                "nonWorking": [names.get(n, n) for n in plan.non_working_ids],
                "text": comment_builder.to_plain_text(plan.text),
                "html": plan.text,
            }
        )

        if config.dry_run:
            # logger.info(
                # "[dry-run] task %s / approval %s (%s)\n           %s",
                # plan.task_id,
                # plan.approval_id,
                # plan.reason,
                # comment_builder.to_plain_text(plan.text),
            # )
            continue

        try:
            comment_id = client.create_comment(plan.task_id, plan.text)
        except WrikeError as exc:
            # Leave the history untouched so the next hourly run retries.
            summary.failures += 1
            general_helpers.log_error(
                general_helpers.make_stamp(
                    task_id=plan.task_id, mode=config.log_mode
                ),
                f"Failed to comment on task {plan.task_id} for approval "
                f"{plan.approval_id}; leaving the history untouched so the next "
                f"run retries",
                exc,
                {
                    "approvalId": plan.approval_id,
                    "taskId": plan.task_id,
                    "reason": plan.reason,
                    "recipientRole": plan.recipient_role,
                    "recipientIds": plan.recipient_ids,
                    "commentHtml": plan.text,
                },
            )
            # logger.exception(
                # "Failed to comment on task %s for approval %s",
                # plan.task_id, plan.approval_id,
            # )
            continue

        summary.comments_posted += 1
        # logger.info(
            # "  posted %s to %s on %s\n           %s",
            # comment_id or "(no id)",
            # ", ".join(names.get(r, r) for r in plan.recipient_ids),
            # permalinks.get(plan.task_id) or plan.task_id,
            # comment_builder.to_plain_text(plan.text),
        # )
        for recipient in plan.recipient_ids:
            store.record_notification(
                approval_id=plan.approval_id,
                task_id=plan.task_id,
                notified_approver_id=recipient,
                notified_approver_name=names.get(recipient, recipient),
                non_working_approver_ids=plan.covered_non_working_ids,
                comment_id=comment_id,
                recipient_role=plan.recipient_role,
            )

    # logger.info(
        # "Run complete: %d planned, %d posted, %d failed",
        # summary.comments_planned, summary.comments_posted, summary.failures,
    # )
    return finish(
        f"{summary.comments_planned} planned, {summary.comments_posted} posted, "
        f"{summary.failures} failed"
    )


def build_store(config: Config) -> "Store":
    """Open the store this run needs. The one place a connection is made."""
    from database_helpers import DatabaseConnection  # late: config errors first
    from store import Store

    database = DatabaseConnection(
        host=config.mysql_host,
        database=config.mysql_database,
        user=config.mysql_user,
        password=config.mysql_password,
        port=config.mysql_port,
        timeout_seconds=config.mysql_timeout_seconds,
        # Pool of one: a run is single-threaded, and one connection reused
        # across it beats reconnecting per query.
        pool_size=1,
        pool_name="workschedule_emails",
        zone=config.timezone,
    )
    return Store(
        database,
        non_working_table=config.non_working_table,
        notifications_table=config.notifications_table,
        baseline_table=config.baseline_table,
    )


def build_client(config: Config) -> WrikeClient:
    """Open the Wrike client this run needs."""
    return WrikeClient(
        token=config.wrike_token,
        base_url=config.wrike_base_url,
        timeout=config.request_timeout,
    )


def configure_logging(config: Config) -> None:
    """Point the shared logger at the collector. Call once, at startup."""
    general_helpers.papertrail_url = config.papertrail_url
    general_helpers.papertrail_token = config.papertrail_token
    general_helpers.system_identifier = config.log_keyword
    general_helpers.log_mode = config.log_mode


def run_from_env(
    force_window: bool = False,
    dry_run: Optional[bool] = None,
    only_task_ids: Optional[Set[str]] = None,
    title_contains: Optional[str] = None,
) -> RunSummary:
    """Build everything from segredo.ini and execute one run."""
    config = Config.from_segredo()
    if dry_run is not None:
        config = Config(**{**config.__dict__, "dry_run": dry_run})

    configure_logging(config)

    client = build_client(config)
    store = build_store(config)
    try:
        store.ensure_schema()
        return run(
            config,
            client,
            store,
            force_window=force_window,
            only_task_ids=only_task_ids,
            title_contains=title_contains,
        )
    finally:
        store.close()
