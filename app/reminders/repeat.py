"""When a repeating reminder is due next, in the Tasks plugin's words the bot already writes.

    every day | every week | every week on Monday, Friday | every month | every year
    every 3 days | every 2 weeks | every 6 months

A rule keeps the wall-clock time of its first occurrence in the user's time zone, so a reminder
at 08:00 stays at 08:00 across a daylight-saving change. A monthly one set for the 31st falls
on the last day of a shorter month and returns to the 31st when there is one."""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_RULE = re.compile(r"^every\s+(?:(\d+)\s+)?(day|week|month|year)s?(?:\s+on\s+(.+))?$")
MAX_STEPS = 5000  # a loop guard: no valid rule needs more steps to pass any date


def parse(rule: str) -> tuple[int, str, tuple[int, ...]] | None:
    """(every N, unit, weekdays) or None for words that are not a rule."""
    m = _RULE.match(" ".join(rule.casefold().split()))
    if m is None:
        return None
    n = int(m.group(1) or 1)
    unit = m.group(2)
    days: tuple[int, ...] = ()
    if m.group(3):
        if unit != "week":
            return None
        names = [d.strip() for d in m.group(3).split(",") if d.strip()]
        if not names or any(d not in WEEKDAYS for d in names):
            return None
        days = tuple(sorted({WEEKDAYS.index(d) for d in names}))
    return (n, unit, days) if n > 0 else None


def valid(rule: str) -> bool:
    return parse(rule) is not None


def _add_months(start: datetime, months: int) -> datetime:
    month0 = start.month - 1 + months
    year, month = start.year + month0 // 12, month0 % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def _occurrence(start: datetime, n: int, unit: str, k: int) -> datetime:
    """The k-th occurrence after `start` (k = 0 is start itself), on the same wall clock."""
    if unit == "day":
        return start + timedelta(days=n * k)
    if unit == "week":
        return start + timedelta(weeks=n * k)
    if unit == "month":
        return _add_months(start, n * k)
    return _add_months(start, 12 * n * k)


def next_after(rule: str, start: datetime, after: datetime) -> datetime | None:
    """The first occurrence of `rule` strictly after `after`, counted from `start` (an aware
    local datetime: the first time it was due). None for a rule that is not one."""
    parsed = parse(rule)
    if parsed is None:
        return None
    n, unit, days = parsed
    zone = start.tzinfo
    local_after = after.astimezone(zone)

    def at(moment: datetime) -> datetime:
        # The same wall-clock time in that day's offset (DST-safe with zoneinfo).
        return moment.replace(tzinfo=None).replace(tzinfo=zone)

    if days:
        # Weekly on given days: walk the days from start's week, every n-th week.
        week0 = start - timedelta(days=start.weekday())
        for step in range(MAX_STEPS):
            day = week0 + timedelta(days=step)
            weeks = step // 7
            if weeks % n or day.weekday() not in days:
                continue
            candidate = at(day)
            if candidate >= start and candidate > local_after:
                return candidate
        return None
    for k in range(MAX_STEPS):
        candidate = at(_occurrence(start, n, unit, k))
        if candidate > local_after:
            return candidate
    return None
