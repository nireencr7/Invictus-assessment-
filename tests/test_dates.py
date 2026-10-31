"""Risk: "Thursday" booked on the wrong Thursday. The model classifies; code resolves.
Every timing phrase in the 40 samples, against Tue 22 Sep 2026."""

from datetime import date

import pytest

from riverbend.extraction.dates import resolve
from riverbend.extraction.schema import DateRequest

TUE = date(2026, 9, 22)


def req(kind, weekday="none", month=0, day=0, tod="any", time=""):
    return DateRequest(
        kind=kind, weekday=weekday, month=month, day=day, time_of_day=tod, time_24h=time, phrase=""
    )


@pytest.mark.parametrize(
    "phrase,request_,expected",
    [
        ("Thursday morning", req("weekday", "thursday", tod="morning"), ("2026-09-24", "2026-09-24")),
        ("tomorrow ... around 4", req("tomorrow", time="16:00"), ("2026-09-23", "2026-09-23")),
        ("Free Saturday", req("weekday", "saturday"), ("2026-09-26", "2026-09-26")),
        ("Friday OK?", req("weekday", "friday"), ("2026-09-25", "2026-09-25")),
        ("Any day next week", req("next_week"), ("2026-09-28", "2026-10-04")),
        ("sometime in October", req("month", month=10), ("2026-10-01", "2026-10-31")),
        ("Sunday works", req("weekday", "sunday"), ("2026-09-27", "2026-09-27")),
        ("Wednesday", req("weekday", "wednesday"), ("2026-09-23", "2026-09-23")),
        ("ASAP please", req("asap"), ("2026-09-22", "2026-09-23")),
        ("Monday", req("weekday", "monday"), ("2026-09-28", "2026-09-28")),
        ("Weekend?", req("this_weekend"), ("2026-09-26", "2026-09-27")),
        ("Today if you can", req("today"), ("2026-09-22", "2026-09-22")),
        ("Next Tuesday", req("next_weekday", "tuesday"), ("2026-09-29", "2026-09-29")),
        ("Next Monday", req("next_weekday", "monday"), ("2026-09-28", "2026-09-28")),
        ("Any weekday", req("any_weekday"), ("2026-09-23", "2026-10-06")),
        ("October 4", req("calendar_date", month=10, day=4), ("2026-10-04", "2026-10-04")),
    ],
)
def test_sample_phrases(phrase, request_, expected):
    w = resolve(request_, TUE)
    assert (w.earliest.isoformat(), w.latest.isoformat()) == expected, phrase


def test_bare_weekday_said_on_that_weekday_means_next_week():
    assert resolve(req("weekday", "tuesday"), TUE).earliest == date(2026, 9, 29)


def test_calendar_date_in_the_past_rolls_to_next_year():
    assert resolve(req("calendar_date", month=1, day=15), TUE).earliest == date(2027, 1, 15)


def test_month_already_started_starts_today():
    assert resolve(req("month", month=9), TUE).earliest == TUE


def test_unspecified_is_none():
    assert resolve(req("unspecified"), TUE) is None


def test_model_cannot_send_inconsistent_date_shapes():
    with pytest.raises(ValueError):
        req("weekday")  # weekday kind without a weekday
    with pytest.raises(ValueError):
        req("calendar_date", month=10)  # no day
    with pytest.raises(ValueError):
        req("tomorrow", time="4pm")  # not HH:MM
