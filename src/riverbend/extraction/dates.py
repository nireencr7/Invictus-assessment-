"""Deterministic resolution of the customer's timing request into a date window.

The model classifies the phrase ("Thursday morning" -> kind=weekday, weekday=thursday,
time_of_day=morning). This module does the calendar arithmetic against the message's
received date in the shop's timezone. That keeps date maths out of the model, makes it
unit-testable, and keeps the prompt free of "today's date" (so it stays cacheable).

Conventions (documented because humans disagree about them):
- A bare weekday means the next occurrence strictly after the received date.
  ("Tuesday", said on a Tuesday, means a week later.)
- "next <weekday>" means that weekday in the following Monday-start calendar week.
  On Tue 22 Sep 2026: "next Monday" = 28 Sep, "next Tuesday" = 29 Sep.
- An explicit date without a year is the next occurrence on or after the received date.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from riverbend.extraction.schema import DateRequest

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class DateResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class DateWindow:
    earliest: date
    latest: date
    time_of_day: str
    time_24h: str
    phrase: str

    @property
    def label(self) -> str:
        if self.earliest == self.latest:
            text = self.earliest.strftime("%a %d %b %Y")
        elif self.earliest.year == self.latest.year:
            text = f"{self.earliest.strftime('%a %d %b')} – {self.latest.strftime('%a %d %b %Y')}"
        else:
            text = f"{self.earliest.isoformat()} – {self.latest.isoformat()}"
        if self.time_24h:
            text += f", {self.time_24h}"
        elif self.time_of_day != "any":
            text += f", {self.time_of_day}"
        return text


def _next_weekday_after(today: date, weekday: str) -> date:
    target = _WEEKDAYS.index(weekday)
    delta = (target - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _monday_of_next_week(today: date) -> date:
    return today + timedelta(days=7 - today.weekday())


def resolve(req: DateRequest, today: date) -> DateWindow | None:
    """Return the requested window, or None when the customer gave no timing."""

    def window(start: date, end: date) -> DateWindow:
        return DateWindow(start, end, req.time_of_day, req.time_24h, req.phrase)

    kind = req.kind
    if kind == "unspecified":
        return None
    if kind == "today":
        return window(today, today)
    if kind == "tomorrow":
        d = today + timedelta(days=1)
        return window(d, d)
    if kind == "asap":
        return window(today, today + timedelta(days=1))
    if kind == "weekday":
        d = _next_weekday_after(today, req.weekday)
        return window(d, d)
    if kind == "next_weekday":
        d = _monday_of_next_week(today) + timedelta(days=_WEEKDAYS.index(req.weekday))
        return window(d, d)
    if kind == "calendar_date":
        for year in (today.year, today.year + 1):
            try:
                d = date(year, req.month, req.day)
            except ValueError as exc:
                raise DateResolutionError(f"invalid calendar date {req.month}/{req.day}") from exc
            if d >= today:
                return window(d, d)
        raise DateResolutionError("unreachable")  # pragma: no cover
    if kind == "this_weekend":
        if today.weekday() == 6:
            return window(today, today)
        sat = today if today.weekday() == 5 else _next_weekday_after(today, "saturday")
        return window(sat, sat + timedelta(days=1))
    if kind == "next_week":
        mon = _monday_of_next_week(today)
        return window(mon, mon + timedelta(days=6))
    if kind == "month":
        year = today.year if req.month >= today.month else today.year + 1
        first = date(year, req.month, 1)
        last = date(year, req.month, calendar.monthrange(year, req.month)[1])
        return window(max(first, today), last)
    if kind == "any_weekday":
        return window(today + timedelta(days=1), today + timedelta(days=14))
    if kind == "flexible":
        return window(today + timedelta(days=1), today + timedelta(days=30))
    raise DateResolutionError(f"unknown date kind {kind}")  # pragma: no cover
