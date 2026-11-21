"""The cleanup as a conversation, through the orchestrator: a card per undated task, the next
message or a button answers it, and every answer that writes can be undone."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import texts
from app.chats import ChatScope
from app.vault.cleanup import Kept
from tests.test_orchestrator import (
    CHAT,
    NOW,
    USER,
    Clock,
    bot,  # noqa: F401 (a fixture)
    staged_bot,  # noqa: F401 (a fixture)
)

TASKS = """## дом

- [ ] смазать двери #home
- [ ] пылесос #home 📅 2026-11-25
- [ ] посадить растения #home

## моя херня

- [ ] врачи #personal
- [x] хлеб #home ✅ 2026-09-01
"""
ARCHIVE = f"{texts.VAULT_ARCHIVE_NOTE}.md"


@pytest.fixture
def clean(staged_bot, tmp_path):  # noqa: F811
    path = staged_bot.dir / f"{texts.VAULT_TASKS_NOTE}.md"
    path.write_text(TASKS, encoding="utf-8")
    staged_bot.index.refresh()
    staged_bot.vault._kept_tasks = Kept(tmp_path / "cleanup_kept.json")
    staged_bot.tasks = lambda: path.read_text(encoding="utf-8")
    return staged_bot


def ids(reply) -> list[str]:
    return [b.id for row in reply.buttons for b in row]


async def test_the_first_card_is_the_first_undated_task(clean):
    reply = await clean.orch.cleanup(CHAT, USER)
    assert reply.text == texts.CLEANUP_CARD.format(n=1, total=3, heading="дом",
                                                   task="смазать двери #home")
    assert ids(reply) == ["c:done:0", "c:drop:0", "c:keep:0", "c:stop:0"]


async def test_nothing_to_go_through_says_so(clean):
    (clean.dir / f"{texts.VAULT_TASKS_NOTE}.md").write_text("- [ ] x #home 📅 2026-12-01\n",
                                                             encoding="utf-8")
    reply = await clean.orch.cleanup(CHAT, USER)
    assert reply.text == texts.CLEANUP_NOTHING and reply.buttons == []


async def test_done_ticks_it_and_shows_the_next(clean):
    await clean.orch.cleanup(CHAT, USER)
    reply = await clean.orch.handle_callback(CHAT, USER, "c:done:0")
    assert "- [x] смазать двери #home ✅ 2026-09-22" in clean.tasks()
    assert reply.text.startswith(texts.CLEANUP_DID["done"].format(task="смазать двери #home"))
    assert texts.CLEANUP_CARD.format(n=2, total=3, heading="дом",
                                     task="посадить растения #home") in reply.text
    assert ids(reply)[0] == "c:done:1"


async def test_not_needed_goes_to_the_archive_marked_cancelled(clean):
    await clean.orch.cleanup(CHAT, USER)
    await clean.orch.handle_callback(CHAT, USER, "c:drop:0")
    assert "смазать двери" not in clean.tasks()
    archive = (clean.dir / ARCHIVE).read_text(encoding="utf-8")
    assert archive.startswith("- [-] смазать двери #home ❌ 2026-09-22")


async def test_kept_is_never_asked_about_again(clean):
    await clean.orch.cleanup(CHAT, USER)
    await clean.orch.handle_callback(CHAT, USER, "c:keep:0")
    assert clean.tasks() == TASKS  # nothing written
    await clean.orch.handle_callback(CHAT, USER, "c:stop:1")
    again = await clean.orch.cleanup(CHAT, USER)
    assert "смазать двери" not in again.text and "1 из 2" in again.text


async def test_an_answer_in_words_dates_the_task(clean):
    await clean.orch.cleanup(CHAT, USER)
    clean.script.answers["CLEANUP_PROMPT"] = {"kind": "date", "due": "2026-09-26",
                                             "time": "10:00", "text": "", "heading": "",
                                             "tag": ""}
    reply = await clean.orch.handle_text(CHAT, USER, "в субботу в 10")
    assert "- [ ] 10:00 смазать двери #home 📅 2026-09-26" in clean.tasks()
    assert "2 из 3" in reply.text
    assert "INTENT_PROMPT" not in clean.script.stages  # read as the answer, not a new message


async def test_a_rename_and_a_move_in_words(clean):
    await clean.orch.cleanup(CHAT, USER)
    clean.script.answers["CLEANUP_PROMPT"] = {"kind": "change", "due": "", "time": "",
                                             "text": "смазать петли", "heading": "моя херня",
                                             "tag": "personal"}
    await clean.orch.handle_text(CHAT, USER, "перенеси в мою херню и назови смазать петли")
    head, tail = clean.tasks().split("## моя херня")
    assert "смазать" not in head and "- [ ] смазать петли #personal" in tail


async def test_a_move_with_no_tag_named_takes_the_sections_own(clean):
    """«перенеси в мою херню»: the real model names the section and no tag."""
    await clean.orch.cleanup(CHAT, USER)
    clean.script.answers["CLEANUP_PROMPT"] = {"kind": "change", "due": "", "time": "",
                                             "text": "", "heading": "моя херня", "tag": ""}
    await clean.orch.handle_text(CHAT, USER, "перенеси в мою херню")
    assert "- [ ] смазать двери #personal" in clean.tasks().split("## моя херня")[1]


async def test_a_message_about_something_else_is_an_ordinary_message(clean):
    await clean.orch.cleanup(CHAT, USER)
    clean.script.answers.update(CLEANUP_PROMPT={"kind": "other", "due": "", "time": "",
                                                "text": "", "heading": "", "tag": ""},
                                INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                                TASKS_PROMPT={"items": [{"text": "купить чай", "due": "",
                                                         "repeat": "", "heading": "дом",
                                                         "tag": "home", "countdown": False}],
                                              "lookup": ""})
    reply = await clean.orch.handle_text(CHAT, USER, "купить чай")
    assert "- [ ] купить чай #home" in clean.tasks()
    assert reply.text.startswith("✅ Obsidian —")
    # The card is still open: a button of it still works.
    await clean.orch.handle_callback(CHAT, USER, "c:done:0")
    assert "- [x] смазать двери" in clean.tasks()


async def test_stop_ends_it_and_the_next_message_is_ordinary(clean):
    await clean.orch.cleanup(CHAT, USER)
    await clean.orch.handle_callback(CHAT, USER, "c:done:0")
    reply = await clean.orch.handle_callback(CHAT, USER, "c:stop:1")
    assert reply.text == texts.CLEANUP_STOPPED.format(done=1, total=3)
    clean.script.answers.update(INTENT_PROMPT={"intent": "unclear"},
                                TARGET_PROMPT={"target": "t"})
    await clean.orch.handle_text(CHAT, USER, "привет")
    assert "CLEANUP_PROMPT" not in clean.script.stages


async def test_stop_in_words(clean):
    await clean.orch.cleanup(CHAT, USER)
    clean.script.answers["CLEANUP_PROMPT"] = {"kind": "stop", "due": "", "time": "",
                                             "text": "", "heading": "", "tag": ""}
    reply = await clean.orch.handle_text(CHAT, USER, "хватит")
    assert reply.text == texts.CLEANUP_STOPPED.format(done=0, total=3)


async def test_the_last_card_ends_with_a_summary(clean):
    await clean.orch.cleanup(CHAT, USER)
    for n in range(2):
        await clean.orch.handle_callback(CHAT, USER, f"c:keep:{n}")
    reply = await clean.orch.handle_callback(CHAT, USER, "c:done:2")
    assert reply.text.endswith(texts.CLEANUP_END.format(done=3, total=3))
    assert reply.buttons == []


async def test_a_button_of_an_earlier_card_does_nothing(clean):
    await clean.orch.cleanup(CHAT, USER)
    await clean.orch.handle_callback(CHAT, USER, "c:keep:0")
    reply = await clean.orch.handle_callback(CHAT, USER, "c:done:0")
    assert reply.text == texts.CLEANUP_OVER
    assert "- [ ] смазать двери #home" in clean.tasks()


async def test_undo_takes_the_last_answer_back(clean):
    await clean.orch.cleanup(CHAT, USER)
    await clean.orch.handle_callback(CHAT, USER, "c:drop:0")
    assert (await clean.orch.undo(CHAT)).text == texts.UNDONE
    assert "- [ ] смазать двери #home" in clean.tasks()
    archive = clean.dir / ARCHIVE  # it did not exist before: Undo takes it to the trash
    assert not archive.exists() or "смазать двери" not in archive.read_text(encoding="utf-8")


async def test_a_task_changed_meanwhile_is_skipped(clean):
    await clean.orch.cleanup(CHAT, USER)
    path = clean.dir / f"{texts.VAULT_TASKS_NOTE}.md"
    path.write_text(TASKS.replace("смазать двери", "смазать двери в спальне"), encoding="utf-8")
    reply = await clean.orch.handle_callback(CHAT, USER, "c:done:0")
    assert reply.text.startswith(texts.CLEANUP_GONE)
    assert "2 из 3" in reply.text
    assert "- [ ] смазать двери в спальне #home" in path.read_text(encoding="utf-8")


async def test_a_session_left_for_half_an_hour_is_forgotten(clean):
    await clean.orch.cleanup(CHAT, USER)
    clean.orch._clock = Clock(NOW + timedelta(minutes=31))
    clean.script.answers.update(INTENT_PROMPT={"intent": "unclear"},
                                TARGET_PROMPT={"target": "t"})
    await clean.orch.handle_text(CHAT, USER, "в субботу")
    assert "CLEANUP_PROMPT" not in clean.script.stages


async def test_asked_for_in_words_it_starts(clean):
    clean.script.answers.update(INTENT_PROMPT={"intent": "cleanup"},
                                TARGET_PROMPT={"target": "t"})
    reply = await clean.orch.handle_text(CHAT, USER, "давай разберём задачи без срока")
    assert reply.text == texts.CLEANUP_CARD.format(n=1, total=3, heading="дом",
                                                   task="смазать двери #home")
    assert clean.tasks() == TASKS


async def test_a_group_may_not_go_through_the_owners_tasks(clean):
    group = ChatScope(chat_id=-100777, title="club", rights=frozenset({"ask", "add", "tasks"}),
                      tag="клуб", folder="Клуб")
    clean.orch._scopes = lambda chat_id: group if chat_id == -100777 else None
    assert (await clean.orch.cleanup(-100777, 501)).text == texts.CLEANUP_PRIVATE
    clean.script.answers.update(INTENT_PROMPT={"intent": "cleanup"},
                                TARGET_PROMPT={"target": "t"})
    reply = await clean.orch.handle_text(-100777, 501, "давай разберём задачи")
    assert reply.text.startswith(texts.SCOPE_NOT_HERE.split("{")[0])


async def test_a_task_gone_between_reading_and_writing_leaves_no_copy_in_the_archive(
        clean, monkeypatch):
    """«Не нужно» writes the archive first, then takes the line out. If the line went in
    between (a sync, a hand edit), the archive's new line is taken back too."""
    from app.vault import cleanup

    real = cleanup.dropped
    calls = []

    def flaky(text, line, today):
        calls.append(line)
        return real(text, line, today) if len(calls) == 1 else None

    monkeypatch.setattr(cleanup, "dropped", flaky)
    await clean.orch.cleanup(CHAT, USER)
    reply = await clean.orch.handle_callback(CHAT, USER, "c:drop:0")
    assert reply.text.startswith(texts.CLEANUP_GONE)
    archive = clean.dir / ARCHIVE
    assert not archive.exists() or "смазать двери" not in archive.read_text(encoding="utf-8")
