"""app/reminders/store.py on a migrated database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.audit.store import AuditStore
from app.reminders.store import ReminderStore

T0 = datetime(2026, 10, 15, 6, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "bot.sqlite"
    audit = AuditStore(db)
    audit.migrate()
    audit.close()
    s = ReminderStore(db)
    yield s
    s.close()


def test_added_reminders_are_pending_soonest_first_per_chat(store):
    late = store.add(1, 7, "позже", T0 + timedelta(hours=2), now=T0)
    soon = store.add(1, 7, "скоро", T0 + timedelta(hours=1), repeat="every day", now=T0)
    store.add(2, 8, "другой чат", T0, now=T0)
    assert [r.id for r in store.pending(1)] == [soon, late]
    assert store.get(soon).repeat == "every day" and store.get(soon).start_at == \
        T0 + timedelta(hours=1)
    assert store.next_due() == T0


def test_due_sent_repeat_and_cancel(store):
    once = store.add(1, 7, "раз", T0, now=T0)
    daily = store.add(1, 7, "каждый день", T0, repeat="every day", now=T0)
    assert [r.id for r in store.due(T0)] == [once, daily]
    store.sent(once, T0)
    store.sent(daily, T0, next_due=T0 + timedelta(days=1))
    assert store.due(T0) == [] and [r.id for r in store.pending()] == [daily]
    assert store.cancel(daily) and not store.cancel(daily) and not store.cancel(once)
    assert store.pending() == []


def test_rule_firings_are_remembered(store):
    assert not store.fired("r1", "Клуб/встреча.md", T0)
    store.mark_fired("r1", "Клуб/встреча.md", T0, T0)
    store.mark_fired("r1", "Клуб/встреча.md", T0, T0)  # twice: still one row, no error
    assert store.fired("r1", "Клуб/встреча.md", T0)
    assert not store.fired("r1", "Клуб/встреча.md", T0 + timedelta(days=1))  # moved
