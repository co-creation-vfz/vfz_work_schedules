"""Comment text construction.

Wrike renders comments as a restricted HTML subset. A real @mention (the kind
that produces a notification) is an anchor carrying the contact id:

    <a class="stream-user-id avatar" rel="KUAAAAAA">@Jane Smith</a>

Non-working approvers are written as plain text on purpose: mentioning them
would notify someone on their day off, which is the opposite of the point.
"""
import html
import re
from typing import Dict, Sequence


def mention(contact_id: str, name: str) -> str:
    safe_name = html.escape(name or contact_id)
    return f'<a class="stream-user-id avatar" rel="{contact_id}">@{safe_name}</a>'


def _sorted_names(ids: Sequence[str], names: Dict[str, str]) -> list:
    return sorted((names.get(cid) or cid) for cid in set(ids))


def _join_mentions(ids: Sequence[str], names: Dict[str, str]) -> str:
    ordered = sorted(set(ids), key=lambda cid: (names.get(cid) or cid).lower())
    return ", ".join(mention(cid, names.get(cid, cid)) for cid in ordered)


def _join_names(ids: Sequence[str], names: Dict[str, str]) -> str:
    return ", ".join(html.escape(name) for name in _sorted_names(ids, names))


def approver_notice(
    recipient_ids: Sequence[str],
    non_working_ids: Sequence[str],
    names: Dict[str, str],
) -> str:
    """The standard notice sent to working approvers."""
    return (
        f"Hi {_join_mentions(recipient_ids, names)}, the following approver(s) "
        f"are not working today: {_join_names(non_working_ids, names)}."
        f" Please decide whether this can wait until they return. If not, please reassign them or remove them, add a replacement approver, or tag their team."
    )


def project_lead_notice(
    recipient_ids: Sequence[str],
    non_working_ids: Sequence[str],
    names: Dict[str, str],
) -> str:
    """Sent to the task's project lead when nobody on the approval is working."""
    return (
        f"Hi {_join_mentions(recipient_ids, names)}, the following approver(s) "
        f"are not working today: {_join_names(non_working_ids, names)}. "
        "There are no working approvers on this approval, so it is coming to "
        "you as project lead."
    )


def no_working_approver_notice(
    recipient_ids: Sequence[str],
    non_working_ids: Sequence[str],
    names: Dict[str, str],
) -> str:
    """Sent to the fallback contact when nobody on the approval is working."""
    return (
        f"Hi {_join_mentions(recipient_ids, names)}, the following approver(s) "
        f"are not working today: {_join_names(non_working_ids, names)}. "
        "There are no working approvers on this approval."
    )


def to_plain_text(markup: str) -> str:
    """Strip the comment markup so a dry run is readable in a terminal.

    Mention anchors carry "@Name" as their inner text, so removing the tags
    leaves exactly what a reader sees in Wrike.
    """
    return html.unescape(re.sub(r"<[^>]+>", "", markup))
