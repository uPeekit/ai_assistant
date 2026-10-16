"""app/reminders/repeat.py: the next time a repeating reminder is due."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.reminders.repeat import next_after, parse, valid

TZ = ZoneInfo("Europe/Athens")


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


def test_rule_words_are_parsed_and_nonsense_is_not():
    assert parse("every day") == (1, "day", ())
    assert parse("every 3 days") == (3, "day", ())
    assert parse("Every week on Monday, Friday") == (1, "week", (0, 4))
    assert parse("every 2 weeks") == (2, "week", ())
    assert not valid("every month on Monday") and not valid("каждый день")
    assert not valid("every week on Funday") and not valid("every 0 days")


def test_daily_weekly_monthly_yearly():
    start = local(2026, 10, 15, 8, 0)
    assert next_after("every day", start, local(2026, 10, 15, 8, 0)) == local(2026, 10, 16, 8, 0)
    assert next_after("every day", start, local(2026, 10, 15, 7, 59)) == start
    assert next_after("every week", start, start) == local(2026, 10, 22, 8, 0)
    assert next_after("every year", start, start) == local(2027, 10, 15, 8, 0)
    assert next_after("every 3 days", start, local(2026, 10, 20, 9, 0)) == \
        local(2026, 10, 21, 8, 0)


def test_weekdays_and_every_other_week():
    start = local(2026, 10, 15, 8, 0)  # a Thursday
    rule = "every week on Monday, Friday"
    assert next_after(rule, start, start) == local(2026, 10, 16, 8, 0)  # Friday
    assert next_after(rule, start, local(2026, 10, 16, 8, 0)) == local(2026, 10, 19, 8, 0)
    # Every other week on Monday: the week of the start counts as the first.
    assert next_after("every 2 weeks on Monday", start, start) == local(2026, 10, 26, 8, 0)


def test_month_ends_fall_back_and_return():
    start = local(2026, 1, 31, 9, 0)
    assert next_after("every month", start, start) == local(2026, 2, 28, 9, 0)
    assert next_after("every month", start, local(2026, 2, 28, 9, 0)) == local(2026, 3, 31, 9, 0)
    leap = local(2028, 2, 29, 9, 0)
    assert next_after("every year", leap, leap) == local(2029, 2, 28, 9, 0)


def test_the_wall_clock_holds_across_daylight_saving():
    start = local(2026, 10, 24, 8, 0)  # EEST; DST ends on 25 October
    after = next_after("every day", start, local(2026, 10, 25, 7, 0))
    assert after == local(2026, 10, 25, 8, 0) and after.utcoffset().total_seconds() == 7200


def test_a_missed_week_gives_the_next_one_not_a_backlog():
    start = local(2026, 10, 1, 8, 0)
    assert next_after("every day", start, local(2026, 10, 15, 12, 0)) == local(2026, 10, 16, 8, 0)
