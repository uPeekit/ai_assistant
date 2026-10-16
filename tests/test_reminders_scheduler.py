"""app/reminders/scheduler.py with a fake clock and a recording sender."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import texts
from app.audit.store import AuditStore
from app.reminders.scheduler import ChatGone, ReminderScheduler, late_by
from app.reminders.store import ReminderStore

TZ = ZoneInfo("Europe/Athens")
T0 = datetime(2026, 10, 15, 9, 0, tzinfo=TZ)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "bot.sqlite"
    audit = AuditStore(db)
    audit.migrate()
    audit.close()
    s = ReminderStore(db)
    yield s
    s.close()


class Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail: Exception | None = None

    async def __call__(self, chat_id: int, text: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, text))


def scheduler(store, sender, **kw) -> ReminderScheduler:
    return ReminderScheduler(store, sender, "Europe/Athens", **kw)


async def test_on_time_and_late_with_the_delay_named(store):
    sender = Sender()
    store.add(1, 7, "купить хлеб", T0)
    store.add(2, 7, "позвонить", T0 - timedelta(hours=3))  # missed while the bot was off
    assert await scheduler(store, sender).run_once(T0 + timedelta(seconds=5)) == 2
    assert (1, texts.REMINDER.format(text="купить хлеб")) in sender.sent
    assert (2, texts.REMINDER_LATE.format(delay=late_by(timedelta(hours=3)),
                                          text="позвонить")) in sender.sent
    assert store.pending() == []


async def test_a_repeating_reminder_missed_for_days_is_sent_once_and_moves_on(store):
    sender = Sender()
    rid = store.add(1, 7, "зубы", T0, repeat="every day")
    now = T0 + timedelta(days=3, hours=2)
    await scheduler(store, sender).run_once(now)
    assert len(sender.sent) == 1
    assert store.get(rid).due_at == T0 + timedelta(days=4)  # tomorrow 09:00, not a backlog
    assert await scheduler(store, sender).run_once(now) == 0


async def test_a_failed_send_is_retried_and_a_gone_chat_cancels(store):
    sender = Sender()
    rid = store.add(1, 7, "x", T0)
    sender.fail = OSError("network")
    await scheduler(store, sender).run_once(T0)
    assert store.get(rid) in store.pending()
    sender.fail = ChatGone()
    await scheduler(store, sender).run_once(T0)
    assert store.pending() == []


async def test_the_loop_sends_when_due_and_runs_the_rules_every_ten_minutes(store):
    clock = {"now": T0}
    sender = Sender()
    passes: list[datetime] = []

    async def rules(now):
        passes.append(now)

    async def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    store.add(1, 7, "через 90 секунд", T0 + timedelta(seconds=90))
    s = scheduler(store, sender, rules=rules, now=lambda: clock["now"], sleep=sleep)
    s.start()
    for _ in range(500):  # the store runs in worker threads: real time, bounded
        await asyncio.sleep(0.01)
        if clock["now"] >= T0 + timedelta(minutes=21):
            break
    await s.stop()
    assert sender.sent == [(1, texts.REMINDER.format(text="через 90 секунд"))]
    assert passes[0] == T0 and len(passes) >= 2
    assert all(b - a >= timedelta(minutes=10) for a, b in zip(passes, passes[1:], strict=False))


def test_late_by_reads_as_minutes_hours_or_days():
    assert late_by(timedelta(minutes=5)) == texts.REMINDER_MINUTES.format(n=5)
    assert late_by(timedelta(hours=3)) == texts.REMINDER_HOURS.format(n=3)
    assert late_by(timedelta(days=3)) == texts.REMINDER_DAYS.format(n=3)


# ---- from the branch review ------------------------------------------------------------------

class Counting(Sender):
    def __init__(self):
        super().__init__()
        self.tries = 0

    async def __call__(self, chat_id, text):
        self.tries += 1
        return await super().__call__(chat_id, text)


async def test_a_failing_send_waits_longer_each_time_instead_of_spinning(store):
    sender = Counting()
    sender.fail = OSError("offline")
    store.add(1, 7, "x", T0)
    s = scheduler(store, sender, now=lambda: T0 + timedelta(seconds=1))
    await s.run_once(T0)
    await s.run_once(T0 + timedelta(seconds=1))  # within the wait: not tried
    assert sender.tries == 1
    assert s._wait(store.next_due()) > 1  # sleeps until the retry, not 0.05 s
    await s.run_once(T0 + timedelta(seconds=31))
    assert sender.tries == 2
    await s.run_once(T0 + timedelta(seconds=61))  # the second wait is 60 s
    assert sender.tries == 2
    sender.fail = None
    await s.run_once(T0 + timedelta(seconds=92))
    assert sender.sent and store.pending() == []


async def test_a_group_that_became_a_supergroup_takes_its_reminders_along(store):
    from app.reminders.scheduler import ChatMoved

    sender = Sender()
    sender.fail = ChatMoved(-1009999)
    rid = store.add(-555, 7, "x", T0)
    await scheduler(store, sender).run_once(T0)
    assert store.get(rid).chat_id == -1009999


async def test_one_failed_database_read_does_not_end_the_loop(store, monkeypatch):
    clock = {"now": T0}
    sender = Sender()
    real = store.next_due
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real()

    async def sleep(seconds):
        clock["now"] += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(store, "next_due", flaky)
    store.add(1, 7, "позже", T0 + timedelta(minutes=2))
    s = scheduler(store, sender, now=lambda: clock["now"], sleep=sleep)
    s.start()
    for _ in range(500):
        await asyncio.sleep(0.01)
        if sender.sent:
            break
    await s.stop()
    assert sender.sent == [(1, texts.REMINDER.format(text="позже"))]
