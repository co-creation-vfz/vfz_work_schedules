"""Tests for the shared SolarWinds line in ``shared/general_helpers.py``.

The logger is shared by all three integrations, so it has no suite of its own;
it is tested from here because this is the integration whose cron silence is
the reason the format exists in its current shape.

One event is one line:

    {keyword}: {task_id or timestamp} {message}: {details}

The assertions below are deliberately about the line as a whole rather than its
parts. It is what a person reads in SolarWinds, and a change that quietly
reorders or drops a field is exactly what they would not notice.
"""

import json

import pytest

import general_helpers


class _ImmediateThread:
    """Runs the target inline, so a test never races the delivery thread."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def sent(monkeypatch):
    """Capture the line that would be POSTed, without any network."""
    lines = []

    monkeypatch.setattr(general_helpers, "papertrail_url", "https://collector/v1/logs")
    monkeypatch.setattr(general_helpers, "papertrail_token", "test-token")
    monkeypatch.setattr(general_helpers, "log_mode", "")
    monkeypatch.setattr(general_helpers, "system_identifier", "vfz_workschedule_test")
    monkeypatch.setattr(general_helpers.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(general_helpers, "_papertrail_thread", lines.append)

    return lines


def test_an_event_without_a_task_id_carries_the_timestamp(sent):
    stamp = general_helpers.make_stamp()
    stamp["time"] = 1756208400123456

    general_helpers.log_message(stamp, "Resync finished", {"inserted": 2})

    keyword, rest = sent[0].split(": ", 1)
    identifier, rest = rest.split(" ", 1)
    message, details = rest.split(": ", 1)

    assert keyword == "vfz_workschedule_test"
    # The timestamp stands in for the id, rather than "no task_id found".
    assert identifier == "1756208400123456"
    assert message == "Resync finished"
    assert json.loads(details)["inserted"] == 2


def test_a_known_task_id_replaces_the_timestamp(sent):
    general_helpers.log_message(
        general_helpers.make_stamp(task_id="IEAFXAOH"), "Comment posted"
    )

    assert sent[0].startswith("vfz_workschedule_test: IEAFXAOH Comment posted: ")


def test_the_calling_function_is_always_in_the_details(sent):
    """The one field the call site never has to remember to pass."""
    general_helpers.log_message(general_helpers.make_stamp(), "Something happened")

    details = json.loads(sent[0].split(": ", 2)[2])
    assert details["caller"] == "test_the_calling_function_is_always_in_the_details"


def test_an_error_line_carries_the_type_and_line_number(sent):
    """What makes a 2am line actionable, and it must survive the flattening."""
    try:
        raise ValueError("Wrike returned 422")
    except ValueError as exc:
        general_helpers.log_error(general_helpers.make_stamp(), "Update failed", exc)

    details = json.loads(sent[0].split(": ", 2)[2])
    assert details["error_type"] == "ValueError"
    assert details["error"] == "Wrike returned 422"
    assert details["line_number"] > 0
    # Attributed to the real caller, not to log_error itself.
    assert details["caller"] == "test_an_error_line_carries_the_type_and_line_number"


def test_details_that_will_not_serialise_do_not_break_the_line(sent):
    """A date, a Decimal, a model object: never a reason to lose the event."""
    from datetime import date

    general_helpers.log_message(
        general_helpers.make_stamp(), "Window resolved", {"from": date(2026, 8, 31)}
    )

    assert json.loads(sent[0].split(": ", 2)[2])["from"] == "2026-08-31"


def test_print_only_sends_nothing(sent):
    general_helpers.log_message(
        general_helpers.make_stamp(mode="print_only"), "Local run"
    )

    assert sent == []


def test_do_nothing_sends_nothing(sent):
    general_helpers.log_message(
        general_helpers.make_stamp(mode="do_nothing"), "Silenced"
    )

    assert sent == []
