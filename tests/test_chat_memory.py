"""app/conversation/memory.py: a chat's recent exchanges, per chat, for two hours."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.audit.store import AuditStore
from app.conversation.memory import BOT, MAX_EXCHANGES, USER, ChatMemory

T0 = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)


@pytest.fixture
def memory(tmp_path):
    db = tmp_path / "bot.sqlite"
    audit = AuditStore(db)
    audit.migrate()
    audit.close()
    m = ChatMemory(db)
    yield m
    m.close()


def test_recent_is_per_chat_oldest_first_and_capped(memory):
    for i in range(MAX_EXCHANGES + 3):
        memory.add(1, 7, USER, f"вопрос {i}", T0 + timedelta(minutes=i))
        memory.add(1, 7, BOT, f"ответ {i}", T0 + timedelta(minutes=i, seconds=30))
    memory.add(2, 8, USER, "другой чат", T0)
    got = memory.recent(1, T0 + timedelta(minutes=20))
    assert len(got) == 2 * MAX_EXCHANGES and got[-1] == (BOT, f"ответ {MAX_EXCHANGES + 2}")
    assert all("другой чат" not in t for _, t in got)
    assert memory.last_answer(1, T0 + timedelta(minutes=20)) == f"ответ {MAX_EXCHANGES + 2}"


def test_older_than_two_hours_is_forgotten(memory):
    memory.add(1, 7, USER, "давно", T0)
    memory.add(1, 7, BOT, "давний ответ", T0)
    assert memory.recent(1, T0 + timedelta(hours=2, minutes=1)) == []
    assert memory.last_answer(1, T0 + timedelta(hours=3)) == ""


def test_empty_text_is_not_remembered(memory):
    memory.add(1, 7, BOT, "  ", T0)
    assert memory.recent(1, T0) == []
