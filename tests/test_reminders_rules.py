"""app/reminders/rules.py: rules over dated notes and tasks, fired once each."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import texts
from app.audit.store import AuditStore
from app.chats import ChatScope
from app.reminders.rules import ReminderRules, run_rules
from app.reminders.store import ReminderStore
from app.vault.index import VaultIndex
from app.vault.scope import ScopedIndex

TZ = ZoneInfo("Europe/Athens")
CREATED = datetime(2026, 10, 1, tzinfo=UTC)
GROUP = -100777


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Клуб").mkdir(parents=True)
    (root / "Клуб/Пикник.md").write_text(
        "---\ndate: 2026-11-08\ntags: [клуб]\n---\n", encoding="utf-8")
    (root / "Клуб/Моя встреча.md").write_text("---\ndate: 2026-11-08\n---\n", encoding="utf-8")
    (root / "Клуб/Прошлая.md").write_text("---\ndate: 2026-10-01\n---\n", encoding="utf-8")
    (root / "Задачи.md").write_text(
        "- [ ] платить аренду #дача 📅 2026-11-08\n"
        "- [ ] 17:30 смотреть квартиру #дача 📅 2026-11-08\n"
        "- [ ] без тега 📅 2026-11-08\n", encoding="utf-8")
    index = VaultIndex(root)
    index.refresh()
    return index


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
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


def rules(tmp_path, *raw) -> ReminderRules:
    r = ReminderRules(tmp_path / "rules.json", now=lambda: CREATED)
    r.save([{"created_at": CREATED.isoformat(), **x} for x in raw])
    return r


async def test_a_meeting_rule_fires_the_day_before_at_its_time_once(tmp_path, vault, store):
    r = rules(tmp_path, {"kind": "notes", "folder": "Клуб", "days_before": 1, "time": "09:00",
                         "chat_id": 1})
    send = Sender()
    before = datetime(2026, 11, 7, 8, 59, tzinfo=TZ)
    assert await run_rules(before, r.all(), store, lambda c: vault, ("date",), send,
                           "Europe/Athens") == 0
    at = datetime(2026, 11, 7, 9, 0, tzinfo=TZ)
    assert await run_rules(at, r.all(), store, lambda c: vault, ("date",), send,
                           "Europe/Athens") == 2  # both meetings on 8 November
    assert (1, texts.REMINDER.format(text=texts.REMINDER_RULE_TOMORROW.format(
        label="Пикник"))) in send.sent
    assert await run_rules(at + timedelta(minutes=10), r.all(), store, lambda c: vault,
                           ("date",), send, "Europe/Athens") == 0  # never twice


async def test_a_moved_meeting_fires_again_at_its_new_time(tmp_path, vault, store):
    r = rules(tmp_path, {"kind": "notes", "folder": "Клуб", "days_before": 1, "chat_id": 1})
    send = Sender()
    await run_rules(datetime(2026, 11, 7, 9, 5, tzinfo=TZ), r.all(), store, lambda c: vault,
                    ("date",), send, "Europe/Athens")
    (vault.root / "Клуб/Пикник.md").write_text("---\ndate: 2026-11-15\ntags: [клуб]\n---\n",
                                               encoding="utf-8")
    vault.refresh()
    assert await run_rules(datetime(2026, 11, 14, 9, 1, tzinfo=TZ), r.all(), store,
                           lambda c: vault, ("date",), send, "Europe/Athens") == 1


async def test_past_items_and_moments_before_the_rule_never_fire(tmp_path, vault, store):
    late_rule = ReminderRules(tmp_path / "late.json",
                              now=lambda: datetime(2026, 11, 7, 12, 0, tzinfo=TZ))
    late_rule.save([{"kind": "notes", "folder": "Клуб", "days_before": 1, "chat_id": 1}])
    send = Sender()
    # Created after 7 Nov 09:00: the 8 November meetings are not reminded of.
    assert await run_rules(datetime(2026, 11, 7, 13, 0, tzinfo=TZ), late_rule.all(), store,
                           lambda c: vault, ("date",), send, "Europe/Athens") == 0
    assert all("Прошлая" not in t for _, t in send.sent)


async def test_a_task_rule_uses_the_tasks_own_time_on_its_day(tmp_path, vault, store):
    r = rules(tmp_path, {"kind": "tasks", "tag": "дача", "days_before": 0, "time": "09:00",
                         "chat_id": 1})
    send = Sender()
    await run_rules(datetime(2026, 11, 8, 9, 0, tzinfo=TZ), r.all(), store, lambda c: vault,
                    ("date",), send, "Europe/Athens")
    assert [t for _, t in send.sent] == [texts.REMINDER.format(
        text=texts.REMINDER_RULE_TODAY.format(label="платить аренду"))]
    await run_rules(datetime(2026, 11, 8, 17, 30, tzinfo=TZ), r.all(), store,
                    lambda c: vault, ("date",), send, "Europe/Athens")
    assert texts.REMINDER_RULE_TODAY.format(label="17:30 смотреть квартиру") in send.sent[-1][1]
    assert all("без тега" not in t for _, t in send.sent)


async def test_a_group_rule_sees_only_the_groups_scope(tmp_path, vault, store):
    # The group's home is elsewhere: in «Клуб» only the notes with its tag are the group's.
    scope = ChatScope(chat_id=GROUP, rights=frozenset({"remind"}), tag="клуб",
                      folder="Группа")
    r = rules(tmp_path, {"kind": "notes", "folder": "Клуб", "days_before": 1,
                         "chat_id": GROUP})
    send = Sender()
    await run_rules(datetime(2026, 11, 7, 9, 0, tzinfo=TZ), r.all(), store,
                    lambda c: ScopedIndex(vault, scope), ("date",), send, "Europe/Athens")
    assert len(send.sent) == 1 and "Пикник" in send.sent[0][1]  # not the owner's meeting


def test_rules_are_validated_and_keep_their_creation_time(tmp_path):
    r = ReminderRules(tmp_path / "rules.json", now=lambda: CREATED)
    with pytest.raises(ValueError):
        r.save([{"kind": "tasks", "chat_id": 1}])  # a task rule needs a tag
    with pytest.raises(ValueError):
        r.save([{"kind": "notes", "folder": "Клуб", "chat_id": 1, "days_before": 99}])
    r.save([{"kind": "notes", "folder": "Клуб", "chat_id": 1}])
    [saved] = r.dump()
    later = ReminderRules(tmp_path / "rules.json", now=lambda: CREATED + timedelta(days=5))
    later.save([{**saved, "chat_id": 2}])  # only the chat: it keeps its start
    assert later.all()[0].created_at == CREATED and later.all()[0].chat_id == 2
    # Anything it watches changes: a new rule from now, so no burst of past reminders.
    later.save([{**later.dump()[0], "days_before": 3}])
    assert later.all()[0].created_at == CREATED + timedelta(days=5)


async def test_a_late_rule_says_how_far_the_item_really_is(tmp_path, vault, store):
    r = rules(tmp_path, {"kind": "notes", "folder": "Клуб", "days_before": 1, "chat_id": 1})
    send = Sender()
    # The bot was off on the 7th: on the morning of the 8th the meeting is today.
    await run_rules(datetime(2026, 11, 8, 8, 0, tzinfo=TZ), r.all(), store, lambda c: vault,
                    ("date",), send, "Europe/Athens")
    assert send.sent and all(texts.REMINDER_RULE_TODAY.split("{")[0] in t
                             for _, t in send.sent)
