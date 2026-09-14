"""Tests for reading weekday patterns from a file or the Wrike payload."""

import json
from pathlib import Path

import pytest

from sources import (
    JsonScheduleSource,
    ScheduleAssignment,
    WrikeScheduleSource,
    dedupe_assignments,
    parse_workweek,
)
from weekdays import FRIDAY, MONDAY, THURSDAY, TUESDAY, UnknownWeekdayError, WEDNESDAY

FOUR_DAY = {MONDAY, TUESDAY, WEDNESDAY, THURSDAY}
FIXTURE = Path(__file__).parent / "fixtures" / "workschedules_response.json"


# --- local file ------------------------------------------------------------ #
def test_json_source_flattens_schedules_to_assignments(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text(
        json.dumps(
            {
                "schedules": [
                    {
                        "workScheduleId": "S1",
                        "workScheduleTitle": "Four day week",
                        "workingDays": ["Mon", "Tue", "Wed", "Thu"],
                        "users": [
                            {"userId": "KUAAAAAA", "user": "Jan de Vries"},
                            {"userId": "KUABBBBB", "user": "Sanne Bakker"},
                        ],
                    }
                ]
            }
        )
    )

    assignments = JsonScheduleSource(path).assignments()

    assert [a.user_id for a in assignments] == ["KUAAAAAA", "KUABBBBB"]
    assert all(a.working_weekdays == FOUR_DAY for a in assignments)
    assert assignments[0].work_schedule_title == "Four day week"


def test_json_source_accepts_iso_numbers(tmp_path):
    path = tmp_path / "schedules.json"
    path.write_text(
        json.dumps(
            {
                "schedules": [
                    {
                        "workScheduleId": "S2",
                        "workingDays": [1, 2, 3, 4, 5],
                        "users": [{"userId": "KUACCCCC"}],
                    }
                ]
            }
        )
    )

    assignments = JsonScheduleSource(path).assignments()

    assert assignments[0].working_weekdays == FOUR_DAY | {FRIDAY}
    assert assignments[0].user is None


def test_json_source_reports_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        JsonScheduleSource(tmp_path / "nope.json").assignments()


# --- Wrike payload shapes -------------------------------------------------- #
def test_parses_the_real_wrike_workweek_shape():
    """The shape the account actually returns: blocks of workDays + capacity."""
    schedule = {
        "id": "IEAFXAOHMIACBDCL",
        "scheduleType": "Custom",
        "title": "Testing",
        "workweek": [{"workDays": ["Tue", "Wed", "Thu"], "capacityMinutes": 480}],
        "userIds": ["KUAW3EHG", "KUATAIRI"],
    }

    assert parse_workweek(schedule) == {TUESDAY, WEDNESDAY, THURSDAY}


def test_a_zero_capacity_block_is_not_worked():
    schedule = {
        "workweek": [
            {"workDays": ["Mon", "Tue", "Wed", "Thu"], "capacityMinutes": 480},
            {"workDays": ["Fri"], "capacityMinutes": 0},
        ]
    }

    assert parse_workweek(schedule) == FOUR_DAY


def test_multiple_blocks_are_unioned():
    schedule = {
        "workweek": [
            {"workDays": ["Mon", "Tue"], "capacityMinutes": 480},
            {"workDays": ["Wed", "Thu"], "capacityMinutes": 240},
        ]
    }

    assert parse_workweek(schedule) == FOUR_DAY


# --- the whole real payload ------------------------------------------------ #
class FakeWrikeClient:
    """Stands in for the shared WrikeClient: no real Wrike API calls."""

    def __init__(self, schedules=(), names=None):
        self._schedules = list(schedules)
        self._names = dict(names or {})

    def get_work_schedules(self):
        return list(self._schedules)

    def get_contact_names(self, contact_ids):
        return {cid: self._names.get(cid, cid) for cid in contact_ids}


def _source_from_fixture():
    payload = json.loads(FIXTURE.read_text())["data"]
    return WrikeScheduleSource(FakeWrikeClient(payload))


def test_real_payload_produces_one_assignment_per_member():
    assignments = _source_from_fixture().assignments()

    # 3 on Default + 1 + 2 on Testing + 1 on Studio; the two empty schedules
    # contribute nothing.
    assert len(assignments) == 7
    assert {a.user_id for a in assignments} == {
        "KUAQIONK",
        "KUAPBJP7",
        "KUAQPD3I",
        "KUAP56WO",
        "KUAW3EHG",
        "KUATAIRI",
        "KUAP5OS3",
    }


def test_real_payload_maps_the_testing_schedule_correctly():
    assignments = _source_from_fixture().assignments()

    abigail = next(a for a in assignments if a.user_id == "KUATAIRI")
    assert abigail.work_schedule_id == "IEAFXAOHMIACBDCL"
    assert abigail.work_schedule_title == "Testing"
    assert abigail.working_weekdays == {TUESDAY, WEDNESDAY, THURSDAY}
    assert abigail.schedule_type == "Custom"


def test_real_payload_reports_no_problems():
    source = _source_from_fixture()
    source.assignments()

    assert source.problems == []


def test_schedules_with_no_members_are_skipped():
    assignments = _source_from_fixture().assignments()

    titles = {a.work_schedule_title for a in assignments}
    assert "Brand Team" not in titles  # userIds: []
    assert "Half day schedule" not in titles  # userIds: []


def test_an_unreadable_schedule_is_reported_not_fatal():
    source = WrikeScheduleSource(
        FakeWrikeClient(
            [
                {"id": "S1", "title": "Broken", "userIds": ["KUAAAAAA"]},
                {
                    "id": "S2",
                    "title": "Fine",
                    "workweek": [{"workDays": ["Mon"], "capacityMinutes": 480}],
                    "userIds": ["KUABBBBB"],
                },
            ]
        )
    )

    assignments = source.assignments()

    assert [a.user_id for a in assignments] == ["KUABBBBB"]
    assert len(source.problems) == 1
    assert "Broken" in source.problems[0]
    assert source.unresolved_user_ids == ["KUAAAAAA"]


# --- one assignment per user ----------------------------------------------- #
def _assignment(user_id, title, schedule_type, weekdays):
    return ScheduleAssignment(
        user_id=user_id,
        user=None,
        work_schedule_id=title,
        work_schedule_title=title,
        working_weekdays=weekdays,
        schedule_type=schedule_type,
    )


def test_a_custom_schedule_beats_the_default_schedule():
    chosen, warnings = dedupe_assignments(
        [
            _assignment("KUATAIRI", "Default Schedule", "Default", FOUR_DAY | {FRIDAY}),
            _assignment("KUATAIRI", "Testing", "Custom", {TUESDAY}),
        ]
    )

    assert len(chosen) == 1
    assert chosen[0].work_schedule_title == "Testing"
    assert warnings == []


def test_order_does_not_change_which_schedule_wins():
    chosen, _ = dedupe_assignments(
        [
            _assignment("KUATAIRI", "Testing", "Custom", {TUESDAY}),
            _assignment("KUATAIRI", "Default Schedule", "Default", FOUR_DAY),
        ]
    )

    assert chosen[0].work_schedule_title == "Testing"


def test_two_custom_schedules_for_one_user_are_flagged():
    chosen, warnings = dedupe_assignments(
        [
            _assignment("KUATAIRI", "Testing", "Custom", {TUESDAY}),
            _assignment("KUATAIRI", "Studio", "Custom", FOUR_DAY),
        ]
    )

    assert len(chosen) == 1
    assert len(warnings) == 1
    assert "Resolve this in Wrike" in warnings[0]


def test_different_users_are_untouched():
    chosen, warnings = dedupe_assignments(
        [
            _assignment("KUAAAAAA", "Testing", "Custom", {TUESDAY}),
            _assignment("KUABBBBB", "Studio", "Custom", FOUR_DAY),
        ]
    )

    assert len(chosen) == 2
    assert warnings == []


def test_parses_a_plain_list_of_day_names():
    assert parse_workweek({"workweek": ["Monday", "Tuesday", "Wednesday", "Thursday"]}) == FOUR_DAY


def test_parses_per_day_objects_with_hours():
    payload = {
        "workweek": [
            {"day": "Monday", "hours": 8},
            {"day": "Tuesday", "hours": 8},
            {"day": "Wednesday", "hours": 8},
            {"day": "Thursday", "hours": 8},
            {"day": "Friday", "hours": 0},
            {"day": "Saturday", "hours": 0},
        ]
    }

    assert parse_workweek(payload) == FOUR_DAY


def test_parses_per_day_objects_with_a_working_flag():
    payload = {
        "workWeek": [
            {"dayOfWeek": "MON", "working": True},
            {"dayOfWeek": "FRI", "working": False},
        ]
    }

    assert parse_workweek(payload) == {MONDAY}


def test_parses_a_day_keyed_mapping():
    payload = {"workingDays": {"monday": True, "friday": False, "saturday": True}}

    # Saturday is accepted here; the expander is what refuses to emit weekends.
    assert parse_workweek(payload) == {MONDAY, 5}


def test_unknown_field_name_fails_loudly():
    with pytest.raises(UnknownWeekdayError, match="No working-days field"):
        parse_workweek({"id": "S1", "title": "Testing", "somethingElse": []})


def test_a_pattern_that_resolves_to_nothing_fails_loudly():
    # Better a hard error than silently marking someone off all week.
    with pytest.raises(UnknownWeekdayError, match="zero working days"):
        parse_workweek({"id": "S1", "workweek": [{"day": "Monday", "hours": 0}]})


def test_an_unrecognised_day_name_fails_loudly():
    with pytest.raises(UnknownWeekdayError):
        parse_workweek({"workweek": ["Someday"]})
