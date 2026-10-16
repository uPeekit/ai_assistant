"""Reminders set, listed and cancelled by message: the remind stage, the pipeline, Undo."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import texts
from app.audit.store import AuditStore
from app.chats import ChatRef, ChatScope
from app.reminders.store import ReminderStore
from app.reminders.timing import first_due
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultWriter
from tests.test_vault_scoped_pipeline import Script

TZ = ZoneInfo("Europe/Athens")
NOW = datetime(2026, 10, 15, 21, 0)  # a Thursday evening, local wall clock
ME = ChatRef(chat_id=42, user_id=1, event_id=None, owner=True)


def local(*a) -> datetime:
    return datetime(*a, tzinfo=TZ)


def test_first_due_fills_in_what_the_message_left_out():
    now = local(2026, 10, 15, 21, 0)
    assert first_due("2026-10-16", "", "", now) == local(2026, 10, 16, 9, 0)  # 09:00
    assert first_due("", "22:30", "", now) == local(2026, 10, 15, 22, 30)  # later today
    assert first_due("", "18:00", "", now) == local(2026, 10, 16, 18, 0)  # passed: tomorrow
    assert first_due("2026-10-15", "18:00", "", now) is None  # a dated moment already past
    assert first_due("", "08:00", "every week on Monday", now) == local(2026, 10, 19, 8, 0)
    assert first_due("", "", "every day", now) == local(2026, 10, 16, 9, 0)


@pytest.fixture
def setup(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / f"{texts.VAULT_TASKS_NOTE}.md").write_text("## дом\n\n- [ ] счета #home\n",
                                                         encoding="utf-8")
    db = tmp_path / "bot.sqlite"
    audit = AuditStore(db)
    audit.migrate()
    audit.close()
    store = ReminderStore(db)
    index = VaultIndex(vault)
    index.refresh()
    yield index, store
    store.close()


def pipe(index, store, script) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         StagedFiler(script, "haiku"), now=lambda: NOW, reminders=store,
                         zone="Europe/Athens")


def add(text, due="", time="", repeat="") -> Script:
    return Script(INTENT_PROMPT={"intent": "remind"},
                  REMIND_PROMPT={"op": "add", "text": text, "due": due, "time": time,
                                 "repeat": repeat, "target": "none"})


async def test_a_reminder_is_stored_and_its_task_filed_with_the_same_date(setup):
    index, store = setup
    turn = await pipe(index, store, add("купить хлеб", due="2026-10-16")).handle(
        "напомни завтра купить хлеб", chat=ME)
    [reminder] = store.pending(42)
    assert reminder.text == "купить хлеб" and reminder.due_at == local(2026, 10, 16, 9, 0)
    assert turn.reminders == [reminder.id]
    assert "- [ ] купить хлеб 📅 2026-10-16" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    # The task's own line, then the reminder's.
    said = texts.REMIND_SET.format(when="завтра в 09:00", text="купить хлеб")
    assert said in turn.reply_line()


async def test_a_repeating_reminder_gets_its_first_date_and_a_repeating_task(setup):
    index, store = setup
    await pipe(index, store, add("зарядка", time="08:00", repeat="every week on Monday")).handle(
        "напоминай каждый понедельник в 8 про зарядку", chat=ME)
    [reminder] = store.pending(42)
    assert reminder.due_at == local(2026, 10, 19, 8, 0) and reminder.repeat == \
        "every week on Monday"
    assert "08:00 зарядка 🔁 every week on Monday 📅 2026-10-19" in index.read(
        f"{texts.VAULT_TASKS_NOTE}.md")


async def test_with_no_time_at_all_it_asks_when_and_writes_nothing(setup):
    index, store = setup
    before = index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    turn = await pipe(index, store, add("купить хлеб")).handle("напомни купить хлеб", chat=ME)
    assert turn.reply_line() == texts.REMIND_WHEN and store.pending() == []
    assert index.read(f"{texts.VAULT_TASKS_NOTE}.md") == before


async def test_a_moment_already_past_is_refused_with_its_task(setup):
    index, store = setup
    before = index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    turn = await pipe(index, store, add("позвонить", due="2026-10-15", time="18:00")).handle(
        "напомни сегодня в 18 позвонить", chat=ME)
    assert turn.reply_line().startswith(texts.REMIND_PAST.split("{")[0])
    assert store.pending() == [] and index.read(f"{texts.VAULT_TASKS_NOTE}.md") == before


async def test_in_two_hours_is_counted_from_now(setup):
    index, store = setup
    await pipe(index, store, add("выключить духовку", time="23:00")).handle(
        "напомни через 2 часа выключить духовку", chat=ME)
    assert store.pending(42)[0].due_at == local(2026, 10, 15, 23, 0)


async def test_list_and_cancel_by_key(setup):
    index, store = setup
    store.add(42, 1, "купить хлеб", local(2026, 10, 16, 9, 0))
    store.add(42, 1, "позвонить маме", local(2026, 10, 17, 9, 0))
    script = Script(INTENT_PROMPT={"intent": "remind"},
                    REMIND_PROMPT={"op": "list", "text": "", "due": "", "time": "",
                                   "repeat": "", "target": "none"})
    listed = (await pipe(index, store, script).handle("какие напоминания", chat=ME)).reply_line()
    assert listed.startswith(texts.REMIND_LIST) and "позвонить маме" in listed
    assert "купить хлеб" in script.shown[-1]  # the stage is shown the chat's reminders

    script = Script(INTENT_PROMPT={"intent": "remind"},
                    REMIND_PROMPT={"op": "cancel", "text": "", "due": "", "time": "",
                                   "repeat": "", "target": "r2"})
    turn = await pipe(index, store, script).handle("отмени про маму", chat=ME)
    assert turn.reply_line() == texts.REMIND_CANCELLED.format(text="позвонить маме")
    assert [r.text for r in store.pending(42)] == ["купить хлеб"]


async def test_in_a_group_a_member_cancels_only_their_own(setup):
    index, store = setup
    store.add(-100, 501, "печенье", local(2026, 10, 16, 9, 0))
    scope = ChatScope(chat_id=-100, rights=frozenset({"remind"}), tag="клуб", folder="Клуб")
    script = Script(INTENT_PROMPT={"intent": "remind"},
                    REMIND_PROMPT={"op": "cancel", "text": "", "due": "", "time": "",
                                   "repeat": "", "target": "r1"})
    bob = ChatRef(chat_id=-100, user_id=502, owner=False)
    turn = await pipe(index, store, script).scoped(scope).handle("отмени печенье", chat=bob)
    assert turn.reply_line() == texts.SCOPE_NOT_YOURS and len(store.pending(-100)) == 1


async def test_a_group_without_the_right_cannot_set_one(setup):
    index, store = setup
    scope = ChatScope(chat_id=-100, rights=frozenset({"ask", "tasks"}), tag="клуб",
                      folder="Клуб")
    member = ChatRef(chat_id=-100, user_id=501, owner=False)
    turn = await pipe(index, store, add("печенье", due="2026-10-16")).scoped(scope).handle(
        "напомни завтра про печенье", chat=member)
    assert store.pending() == [] and texts.SCOPE_KINDS["reminder"] in turn.reply_line()


# ---- through the orchestrator ------------------------------------------------------------------

from tests.test_orchestrator import CHAT, USER  # noqa: E402
from tests.test_vault_orchestrator import bot, staged_bot  # noqa: E402,F401 (fixtures)


async def test_undo_cancels_the_reminder_and_takes_the_task_back(staged_bot, tmp_path):  # noqa: F811
    store = ReminderStore(staged_bot.db)
    staged_bot.vault._reminders = store
    staged_bot.vault._zone = TZ
    staged_bot.script.answers.update(
        INTENT_PROMPT={"intent": "remind"},
        REMIND_PROMPT={"op": "add", "text": "купить хлеб", "due": "2026-09-23", "time": "",
                       "repeat": "", "target": "none"})
    reply = await staged_bot.orch.handle_text(CHAT, USER, "напомни завтра купить хлеб")
    assert len(store.pending(CHAT)) == 1 and reply.undo_id
    assert "купить хлеб" in staged_bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")

    undone = await staged_bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")
    assert undone.text == texts.UNDONE and store.pending() == []
    assert "купить хлеб" not in staged_bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    store.close()


async def test_a_question_about_reminders_is_the_remind_stages(setup):
    index, store = setup
    store.add(42, 1, "купить хлеб", local(2026, 10, 16, 9, 0))
    script = Script(INTENT_PROMPT={"intent": "ask"},
                    REMIND_PROMPT={"op": "list", "text": "", "due": "", "time": "",
                                   "repeat": "", "target": "none"})
    turn = await pipe(index, store, script).handle("какие у меня напоминания?", chat=ME)
    assert turn.reply_line().startswith(texts.REMIND_LIST)


async def test_about_is_not_part_of_what_to_remind(setup):
    index, store = setup
    await pipe(index, store, add("про зарядку", time="08:00", repeat="every day")).handle(
        "напоминай каждый день в 8 про зарядку", chat=ME)
    assert store.pending(42)[0].text == "зарядку"


def test_a_date_that_does_not_exist_is_not_a_date():
    from app.vault.staged import _Gate

    assert _Gate("напомни 31 сентября", "").date("2026-09-31") == ""
    assert _Gate("напомни 30 сентября", "").date("2026-09-30") == "2026-09-30"
    assert first_due("2026-02-30", "", "", local(2026, 1, 1, 9, 0)) is None


async def test_today_without_a_time_after_nine_asks_when(setup):
    index, store = setup
    turn = await pipe(index, store, add("позвонить", due="2026-10-15")).handle(
        "напомни сегодня позвонить", chat=ME)
    assert turn.reply_line() == texts.REMIND_WHEN and store.pending() == []


def test_a_reminder_set_while_the_task_write_failed_is_still_reported():
    from app.vault.pipeline import VaultTurn

    turn = VaultTurn(error="PermissionError", reminders=[5], answer="⏰ Напомню завтра: x")
    assert "⏰ Напомню завтра: x" in turn.reply_line()


async def test_a_fix_keeps_the_reminder_and_undo_still_cancels_it(staged_bot):  # noqa: F811
    store = ReminderStore(staged_bot.db)
    staged_bot.vault._reminders = store
    staged_bot.vault._zone = TZ
    staged_bot.script.answers.update(
        INTENT_PROMPT={"intent": "remind"},
        REMIND_PROMPT={"op": "add", "text": "купить хлеб", "due": "2026-09-23", "time": "",
                       "repeat": "", "target": "none"})
    first = await staged_bot.orch.handle_text(CHAT, USER, "напомни завтра купить хлеб")
    staged_bot.script.answers["FIX_PROMPT"] = {
        "changes": [{"key": "a1", "op": "set", "field": "text", "prop": "",
                     "value": "купить хлеб и молоко", "to": ""}], "add": [], "unclear": False}
    await staged_bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")
    fixed = await staged_bot.orch.handle_text(CHAT, USER, "купить хлеб и молоко")
    assert texts.FIX_REMINDER_KEPT in fixed.text and len(store.pending()) == 1
    await staged_bot.orch.handle_callback(CHAT, USER, f"u:{fixed.undo_id}")
    assert store.pending() == []
    store.close()
