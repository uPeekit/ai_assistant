"""When a reminder set by message first fires, and how the reply says when.

The reader returns what the message said — a date, a time, a repeat rule, any of them — and this
fills in the rest: no time means 09:00; a time alone means today, or tomorrow once it has
passed; a repeat rule with no date starts at its next occurrence. A one-off moment that has
already passed is not a reminder (None), and the reply says so."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app import texts
from app.reminders import repeat
from app.vault import summary

DEFAULT_AT = time(9, 0)


def _at(value: str) -> time:
    try:
        hour, minute = (int(p) for p in value.split(":", 1))
        return time(hour, minute)
    except (ValueError, TypeError):
        return DEFAULT_AT


def first_due(due: str, at: str, rule: str, now: datetime) -> datetime | None:
    """The first moment, aware in `now`'s zone; None for a one-off moment already past."""
    zone = now.tzinfo
    clock = _at(at) if at else DEFAULT_AT
    try:
        day = date.fromisoformat(due) if due else now.date()
    except ValueError:
        return None
    start = datetime.combine(day, clock, tzinfo=zone)
    if rule:
        # The first occurrence at or after the start, and after now: "every Monday at 8"
        # said on a Thursday starts next Monday.
        return repeat.next_after(rule, start, max(now, start - timedelta(seconds=1)))
    if start > now:
        return start
    if not due and at:
        return start + timedelta(days=1)  # "at 18:00" said at 19:00 means tomorrow
    return None


def asked_for(due: str, at: str, now: datetime) -> datetime:
    """The moment the message named, even when it has passed (for the reply that says so)."""
    try:
        day = date.fromisoformat(due) if due else now.date()
    except ValueError:
        day = now.date()
    return datetime.combine(day, _at(at) if at else DEFAULT_AT, tzinfo=now.tzinfo)


def when(moment: datetime, today: date) -> str:
    """"tomorrow at 09:00", "Fri 17.10 at 18:30" (in the reply's language)."""
    return texts.REMIND_AT.format(day=summary.when(moment.date().isoformat(), today),
                                  time=moment.strftime("%H:%M"))


def rule_words(rule: str) -> str:
    parsed = repeat.parse(rule)
    if parsed is None:
        return ""
    n, unit, days = parsed
    words = texts.REMIND_EVERY[unit] if n == 1 else texts.REMIND_EVERY_N[unit].format(n=n)
    if days:
        words += " " + texts.REMIND_ON_DAYS.format(
            days=", ".join(texts.REMIND_DAYS_SHORT[d] for d in days))
    return words


def label(moment: datetime, rule: str, today: date) -> str:
    """When, for a reply or a list: the next time, and the rule when it repeats."""
    at = when(moment, today)
    return texts.REMIND_REPEAT.format(rule=rule_words(rule), when=at) if rule else at
