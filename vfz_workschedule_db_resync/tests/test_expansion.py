"""Tests for turning weekday patterns into dates."""

from datetime import date, timedelta

import pytest

from expansion import expand_non_working_dates, is_weekend, iter_dates
from weekdays import (
    FRIDAY,
    MONDAY,
    THURSDAY,
    TUESDAY,
    UnknownWeekdayError,
    WEDNESDAY,
    format_weekdays,
    parse_weekday,
    parse_weekdays,
)

# 2026-08-24 is a Monday; 2026-08-30 is the Sunday that closes that week.
MONDAY_DATE = date(2026, 8, 24)
SUNDAY_DATE = date(2026, 8, 30)

FOUR_DAY_WEEK = {MONDAY, TUESDAY, WEDNESDAY, THURSDAY}
FULL_WEEK = {MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY}


def test_four_day_week_yields_only_the_friday():
    dates = expand_non_working_dates(FOUR_DAY_WEEK, MONDAY_DATE, SUNDAY_DATE)

    assert dates == [date(2026, 8, 28)]
    assert dates[0].strftime("%A") == "Friday"


def test_full_week_yields_nothing():
    assert expand_non_working_dates(FULL_WEEK, MONDAY_DATE, SUNDAY_DATE) == []


def test_weekends_are_never_emitted():
    # A Monday-only schedule is off Tue-Fri, but Saturday and Sunday must not
    # appear even though they are non-working days.
    dates = expand_non_working_dates({MONDAY}, MONDAY_DATE, SUNDAY_DATE)

    assert dates == [
        date(2026, 8, 25),
        date(2026, 8, 26),
        date(2026, 8, 27),
        date(2026, 8, 28),
    ]
    assert not any(is_weekend(d) for d in dates)


def test_a_schedule_that_works_weekends_still_emits_no_weekend_dates():
    dates = expand_non_working_dates({5, 6}, MONDAY_DATE, SUNDAY_DATE)

    assert dates == [
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 26),
        date(2026, 8, 27),
        date(2026, 8, 28),
    ]


def test_multi_week_range_repeats_the_pattern():
    dates = expand_non_working_dates(
        FOUR_DAY_WEEK, MONDAY_DATE, MONDAY_DATE + timedelta(days=27)
    )

    assert len(dates) == 4
    assert all(d.strftime("%A") == "Friday" for d in dates)


def test_a_single_day_range_works():
    friday = date(2026, 8, 28)

    assert expand_non_working_dates(FOUR_DAY_WEEK, friday, friday) == [friday]
    assert expand_non_working_dates(FULL_WEEK, friday, friday) == []


def test_empty_pattern_is_refused():
    # Never silently mark someone as absent every weekday.
    with pytest.raises(ValueError, match="empty"):
        expand_non_working_dates(set(), MONDAY_DATE, SUNDAY_DATE)


def test_invalid_weekday_index_is_refused():
    with pytest.raises(ValueError, match="invalid"):
        expand_non_working_dates({0, 9}, MONDAY_DATE, SUNDAY_DATE)


def test_reversed_range_is_refused():
    with pytest.raises(ValueError):
        expand_non_working_dates(FOUR_DAY_WEEK, SUNDAY_DATE, MONDAY_DATE)


def test_extra_dates_hook_can_add_a_weekday():
    # The hook holidays and leave would use later; still weekend-safe.
    holiday = date(2026, 8, 26)  # a Wednesday this schedule works
    dates = expand_non_working_dates(
        FULL_WEEK, MONDAY_DATE, SUNDAY_DATE, extra_dates=[holiday, date(2026, 8, 29)]
    )

    assert dates == [holiday]


def test_iter_dates_is_inclusive():
    days = list(iter_dates(MONDAY_DATE, MONDAY_DATE))

    assert days == [MONDAY_DATE]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Monday", MONDAY),
        ("monday", MONDAY),
        ("MON", MONDAY),
        ("Mo", MONDAY),
        ("Tues", TUESDAY),
        ("Thurs", THURSDAY),
        ("Fri.", FRIDAY),
        (1, MONDAY),
        (5, FRIDAY),
        ("7", 6),
    ],
)
def test_parse_weekday_accepts_the_usual_spellings(value, expected):
    assert parse_weekday(value) == expected


@pytest.mark.parametrize("value", ["Someday", "", 0, 8, True, None])
def test_parse_weekday_rejects_nonsense(value):
    with pytest.raises(UnknownWeekdayError):
        parse_weekday(value)


def test_parse_weekdays_deduplicates():
    assert parse_weekdays(["Mon", "monday", 1]) == {MONDAY}


def test_format_weekdays_is_in_calendar_order():
    assert format_weekdays({FRIDAY, MONDAY}) == "Monday, Friday"
