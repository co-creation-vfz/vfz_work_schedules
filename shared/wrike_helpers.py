# =============================================================================
# wrike_helpers.py
# Wrike API v4 access for the VFZ work-schedule integrations.
#
# Covers only the endpoints these two need:
#   DB Resync  -> work schedules, contacts
#   Emails     -> approvals, tasks, contacts, comments
#
# Contacts is the overlap, and it is why this is one client rather than two:
# the resync used to do its own unretried contact lookup, so a single
# unreadable contact wrote a row with a NULL name while the notifier's copy
# degraded gracefully. One client, one retry policy, one contact cache.
#
# Wrike caps requests at roughly 100/minute, so every collection read is
# batched and 429s are retried with the server-supplied Retry-After.
# =============================================================================

import json
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set

import requests

BATCH_SIZE = 100      # Wrike allows up to 100 ids per batched call
PAGE_SIZE = 1000
MAX_RETRIES = 4
MAX_RETRY_DELAY = 60  # seconds; an hourly job must not park itself for longer

# Ids per /approvals call. Well inside any query-string limit, so a day with
# a lot of absences cannot turn into a rejected request.
APPROVER_FILTER_BATCH = 50

# The VodafoneZiggo account is EU-hosted. The US endpoint
# (https://www.wrike.com/api/v4) answers 401 for an EU token, which reads as a
# bad token rather than a wrong host -- so the default is the one that works.
DEFAULT_BASE_URL = "https://app-eu.wrike.com/api/v4"


class WrikeError(RuntimeError):
    """A Wrike request failed after exhausting retries."""


# ---------------------------------------------------------------------------
# Parameter formatting -- Wrike is picky and inconsistent about array shapes
# ---------------------------------------------------------------------------


def id_list(values: Iterable[str]) -> str:
    """Wrike wants id arrays as a JSON array literal: ["IEAAAAAA","IEAAAAAB"]."""
    return json.dumps(sorted(set(values)), separators=(",", ":"))


def enum_list(values: Iterable[str]) -> str:
    """Wrike wants enum arrays unquoted: [Pending,Approved]."""
    return "[" + ",".join(values) + "]"


def chunks(items: Sequence[str], size: int = BATCH_SIZE) -> Iterator[Sequence[str]]:
    """Split a sequence into batches Wrike will accept in one call."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _retry_delay(response: requests.Response, attempt: int) -> int:
    """
    How long to wait before retrying, from Retry-After where it is usable.

    Retry-After is allowed to be an HTTP-date, which int() cannot read; the
    exponential backoff is a fine substitute for a job that runs hourly. The
    cap matters more than the parsing: an uncapped Retry-After of 3600 would
    hold a run open until well past the point the next one was due.
    """
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        delay = int(raw)
    except ValueError:
        delay = 2 ** attempt
    return max(1, min(delay, MAX_RETRY_DELAY))


# ---------------------------------------------------------------------------
# WrikeClient
# ---------------------------------------------------------------------------


class WrikeClient:
    """
    Thin Wrike API v4 client. Credentials are passed in by the caller, read
    from segredo.ini. Nothing is hardcoded here.
    """

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
    ) -> None:
        if not token:
            raise ValueError("A Wrike API token is required.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self._contact_cache: Dict[str, str] = {}

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        """
        One Wrike call, with retries on 429 and 5xx.

        A 4xx other than 429 will not fix itself, so it raises immediately
        rather than burning the retry budget.

        :returns:            The decoded JSON body.
        :raises WrikeError:  On a non-retryable status, or once retries run out.
        """
        url = f"{self.base_url}{path}"
        last_error: Optional[str] = None

        for attempt in range(MAX_RETRIES):
            # Nothing follows the last attempt, so sleeping after it only
            # delays the failure the caller is going to see anyway.
            final_attempt = attempt == MAX_RETRIES - 1

            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if final_attempt:
                    break
                time.sleep(min(2 ** attempt, MAX_RETRY_DELAY))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if final_attempt:
                    break
                time.sleep(_retry_delay(response, attempt))
                continue

            if not response.ok:
                raise WrikeError(
                    f"{method} {path} failed with {response.status_code}: "
                    f"{response.text[:500]}"
                )

            return response.json()

        raise WrikeError(
            f"{method} {path} failed after {MAX_RETRIES} attempts: {last_error}"
        )

    # -- work schedules (DB Resync) ---------------------------------------

    def get_work_schedules(self) -> List[Dict[str, Any]]:
        """
        Return every work schedule with its member ids.

        The shape Wrike actually returns:

            {
              "id": "IEAFXAOHMIACBDCL",
              "scheduleType": "Custom",
              "title": "Testing",
              "workweek": [{"workDays": ["Tue","Wed","Thu"], "capacityMinutes": 480}],
              "userIds": ["KUAW3EHG", "KUATAIRI"]
            }

        :returns: List of raw work schedule dicts.
        """
        payload = self._request(
            "GET", "/workschedules", params={"fields": '["userIds"]'}
        )
        return payload.get("data", [])

    # -- approvals (Emails) ------------------------------------------------

    def iter_pending_approvals(
        self, approver_ids: Optional[Sequence[str]] = None
    ) -> Iterator[Dict[str, Any]]:
        """
        Yield Pending approvals, optionally narrowed to a set of approvers.

        Uses `pendingApprovers`, not `approvers`: an approval only matters if
        the non-working person still owes a decision. Someone who approved
        before going off has already done their part and is not worth chasing.

        Passing None returns every Pending approval the token can see, which is
        what the diagnose command uses. Passing an empty list returns nothing,
        so an empty non-working list can never fan out to the whole account.
        """
        if approver_ids is None:
            yield from self._iter_approvals(None)
            return
        if not approver_ids:
            return

        # Asked in batches: a long pendingApprovers list makes the query string
        # long enough for Wrike to reject it, and a 4xx is fatal by design, so
        # one busy absence day would otherwise take the whole run down. An
        # approval with two non-working approvers can come back from more than
        # one batch, hence the dedup.
        seen: Set[str] = set()
        for batch in chunks(sorted(set(approver_ids)), APPROVER_FILTER_BATCH):
            for approval in self._iter_approvals(list(batch)):
                approval_id = approval.get("id")
                if approval_id and approval_id in seen:
                    continue
                if approval_id:
                    seen.add(approval_id)
                yield approval

    def _iter_approvals(
        self, approver_ids: Optional[Sequence[str]]
    ) -> Iterator[Dict[str, Any]]:
        """One paginated /approvals sweep, optionally filtered by approver."""
        params: Dict[str, Any] = {
            "statuses": enum_list(["Pending"]),
            "pageSize": PAGE_SIZE,
        }
        if approver_ids is not None:
            params["pendingApprovers"] = id_list(approver_ids)

        while True:
            payload = self._request("GET", "/approvals", params=params)
            for approval in payload.get("data", []):
                yield approval

            token = payload.get("nextPageToken")
            if not token:
                return
            params = {"nextPageToken": token, "pageSize": PAGE_SIZE}

    # -- tasks (Emails) ----------------------------------------------------

    def get_tasks(self, task_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """Fetch full task objects, batched 100 at a time."""
        tasks: List[Dict[str, Any]] = []
        for batch in chunks(sorted(set(task_ids))):
            payload = self._request("GET", f"/tasks/{','.join(batch)}")
            tasks.extend(payload.get("data", []))
        return tasks

    # -- contacts (both) ---------------------------------------------------

    def get_contact_names(self, contact_ids: Iterable[str]) -> Dict[str, str]:
        """
        Return {contactId: "First Last"}, cached for the lifetime of the run.

        Both integrations store or render display names, so this is the shared
        half of the client. A contact the token cannot see still gets a
        printable label rather than disappearing.
        """
        wanted = sorted({cid for cid in contact_ids if cid})
        missing = [cid for cid in wanted if cid not in self._contact_cache]

        for batch in chunks(missing):
            for contact in self._fetch_contacts(batch):
                name = " ".join(
                    part
                    for part in (
                        contact.get("firstName", ""),
                        contact.get("lastName", ""),
                    )
                    if part
                ).strip()
                self._contact_cache[contact["id"]] = name or contact.get(
                    "primaryEmail", contact["id"]
                )

        for cid in wanted:
            # A deactivated or invisible contact still needs a printable label.
            self._contact_cache.setdefault(cid, cid)

        return {cid: self._contact_cache[cid] for cid in wanted}

    def _fetch_contacts(self, batch: Sequence[str]) -> List[Dict[str, Any]]:
        """
        One batched contact read, degrading to single reads if it is refused.

        Wrike takes contact ids in the path, not as an `ids` query param, and
        it rejects the whole path when one id is unknown or invisible to the
        token. A 4xx is fatal by design, but names are cosmetic: one unreadable
        contact must not take the run down with it, and must not cost the other
        99 names in the batch either.
        """
        try:
            payload = self._request("GET", f"/contacts/{','.join(batch)}")
            return payload.get("data", [])
        except WrikeError:
            if len(batch) == 1:
                return []

        found: List[Dict[str, Any]] = []
        for contact_id in batch:
            found.extend(self._fetch_contacts([contact_id]))
        return found

    def get_current_user(self) -> Dict[str, Any]:
        """The contact behind the token. Used by diagnose to prove auth works."""
        payload = self._request("GET", "/contacts", params={"me": "true"})
        data = payload.get("data", [])
        return data[0] if data else {}

    # -- comments (Emails) -------------------------------------------------

    def create_comment(self, task_id: str, text: str) -> str:
        """Post an HTML comment (so @mentions render) and return its id."""
        payload = self._request(
            "POST",
            f"/tasks/{task_id}/comments",
            data={"text": text, "plainText": "false"},
        )
        data = payload.get("data", [])
        return data[0]["id"] if data else ""

    def delete_comment(self, comment_id: str) -> None:
        """Remove a comment this job posted. Used by the retract command."""
        self._request("DELETE", f"/comments/{comment_id}")
