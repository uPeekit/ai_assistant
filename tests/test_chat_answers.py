"""Chat answers with memory: questions answered in the chat (notes / general / web), never
filed; follow-ups read the chat's recent exchanges; «запиши это» saves what was said, verbatim."""

from __future__ import annotations

from datetime import datetime

import pytest

from app import texts
from app.llm import staged_prompts as P
from app.vault.answer import VaultAnswerer
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultWriter
from tests.test_vault_scoped_pipeline import Script

NOW = datetime(2026, 10, 15, 21, 0)
ANSWER = "Брутализм — стиль архитектуры 1950–70-х:\nголый бетон, тяжёлые формы."


@pytest.fixture
def index(tmp_path):
    (tmp_path / "Заметки").mkdir()
    (tmp_path / "Области").mkdir()
    (tmp_path / "Области/дом.md").write_text("---\ntag: home\n---\n\n## Идеи\n",
                                             encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text("## дом\n\n- [ ] счета #home\n",
                                                          encoding="utf-8")
    (tmp_path / "_bot.md").write_text("- [[дом]] `#home`\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


class AnswerAsk:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.shown: list[tuple[str, str]] = []

    async def __call__(self, system, schema, content, max_tokens=0):
        name = next(k for k, v in vars(P).items() if k.endswith("_PROMPT") and v == system)
        self.shown.append((name, content))
        return self.answers.pop(0), 100, 10


def pipe(index, script, answerer=None) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         StagedFiler(script, "haiku"), now=lambda: NOW, answerer=answerer)


ASK = {"kind": "search", "text": "брутализм", "folder": "", "tag": "", "props": [],
       "due_from": "", "due_to": ""}


# ---- routing ------------------------------------------------------------------------------------

async def test_a_question_read_as_add_is_answered_not_filed(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, ASK_PROMPT=ASK)
    answerer = VaultAnswerer(AnswerAsk({"answer": ANSWER, "sources": [], "more": [],
                                        "kind": "general", "web": ""}), "haiku")
    turn = await pipe(index, script, answerer).handle("что такое брутализм?")
    assert turn.writes == [] and turn.reply_line() == texts.ANSWER_GENERAL.format(answer=ANSWER)


async def test_a_question_that_asks_to_write_is_still_filed(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                    TASKS_PROMPT={"items": [{"text": "купить билеты", "due": "", "time": "",
                                             "repeat": "", "heading": "", "tag": "",
                                             "countdown": False}], "lookup": ""})
    turn = await pipe(index, script).handle("может запишешь купить билеты?")
    assert len(turn.writes) == 1


async def test_the_intent_stage_sees_the_recent_conversation(index):
    script = Script(INTENT_PROMPT={"intent": "ask"}, ASK_PROMPT=ASK)
    history = f"{texts.MEMORY_USER}: что такое брутализм?\n{texts.MEMORY_BOT}: {ANSWER}"
    answerer = VaultAnswerer(AnswerAsk({"answer": "В Париже — Орг де Фландр.", "sources": [],
                                        "more": [], "kind": "general", "web": ""}), "haiku")
    await pipe(index, script, answerer).handle("а в Париже?", history=history)
    assert P.H_HISTORY in script.shown[0] and "брутализм" in script.shown[0]


# ---- where the answer comes from ----------------------------------------------------------------

async def test_a_fresh_fact_is_searched_on_the_web_with_a_progress_line(index):
    script = Script(INTENT_PROMPT={"intent": "ask"}, ASK_PROMPT=ASK)
    ask = AnswerAsk({"answer": "", "sources": [], "more": [], "kind": "web",
                     "web": "цена билета Париж Лион автобус"},
                    {"answer": "От 15 € у FlixBus."})
    searched, progress = [], []

    async def research(message, query, media):
        searched.append(query)
        return "FlixBus: Париж — Лион от 15 €"

    async def said(text):
        progress.append(text)

    turn = await pipe(index, script, VaultAnswerer(ask, "haiku")).handle(
        "сколько стоит билет в Лион?", research=research, progress=said)
    assert searched == ["цена билета Париж Лион автобус"]
    assert progress == [texts.ANSWER_SEARCHING]
    assert turn.reply_line() == texts.ANSWER_WEB.format(answer="От 15 € у FlixBus.")
    assert ask.shown[-1][0] == "WEB_ANSWER_PROMPT" and "FlixBus" in ask.shown[-1][1]


async def test_a_failed_web_search_says_so(index):
    from app.llm.research import ResearchError

    script = Script(INTENT_PROMPT={"intent": "ask"}, ASK_PROMPT=ASK)
    ask = AnswerAsk({"answer": "", "sources": [], "more": [], "kind": "web", "web": "q"})

    async def research(message, query, media):
        raise ResearchError("timeout")

    turn = await pipe(index, script, VaultAnswerer(ask, "haiku")).handle(
        "сколько стоит билет в Лион?", research=research)
    assert turn.reply_line().startswith(texts.ANSWER_WEB_FAILED.split("{")[0])


# ---- «запиши это» -----------------------------------------------------------------------------

async def test_save_this_puts_the_last_answer_into_a_new_note_verbatim(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                    FOLDER_PROMPT={"items": [{"title": "Брутализм", "props": [],
                                              "body": ["пересказ моделью"], "tags": []}],
                                   "lookup": "", "web": "", "media": "text",
                                   "from": "a2"})
    # The latest answer was "nothing in the notes"; the one meant is the one before.
    turn = await pipe(index, script).handle(
        "запиши это в заметки", answers=("В заметках об этом ничего нет.", ANSWER))
    [write] = turn.writes
    text = index.read(write.path)
    assert "голый бетон, тяжёлые формы." in text and "пересказ моделью" not in text
    assert "a1: (" in script.shown[-1] and "a2: (" in script.shown[-1]


async def test_save_what_was_quoted_appends_that_message(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    APPEND_PROMPT={"heading": "Идеи", "body": ["что-то своё"], "lookup": "",
                                   "web": "", "media": "text", "from": "q"})
    await pipe(index, script).handle("добавь это в дом",
                                     pointed=(("q", "Аня: купить стеллаж в прихожую"),))
    assert "Аня: купить стеллаж в прихожую" in index.read("Области/дом.md")
    assert "что-то своё" not in index.read("Области/дом.md")


async def test_with_nothing_to_point_at_nothing_is_taken(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    APPEND_PROMPT={"heading": "", "body": ["своими словами"], "lookup": "",
                                   "web": "", "media": "text"})
    await pipe(index, script).handle("добавь в дом идею про полки")
    assert "своими словами" in index.read("Области/дом.md")
    assert P.H_POINTED not in script.shown[-1]


# ---- memory through the orchestrator --------------------------------------------------------

from app.conversation.memory import ChatMemory  # noqa: E402
from tests.test_orchestrator import CHAT, USER  # noqa: E402
from tests.test_vault_orchestrator import bot, staged_bot  # noqa: E402,F401 (fixtures)


async def test_the_next_message_is_read_with_the_last_exchange(staged_bot):  # noqa: F811
    memory = ChatMemory(staged_bot.db)
    staged_bot.orch._memory = memory
    staged_bot.script.answers.update(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
        TASKS_PROMPT={"items": [{"text": "молоко", "due": "", "time": "", "repeat": "",
                                 "heading": "дом", "tag": "", "countdown": False}],
                      "lookup": ""})
    first = await staged_bot.orch.handle_text(CHAT, USER, "надо молоко")
    await staged_bot.orch.handle_text(CHAT, USER, "и ещё хлеб")
    shown = [c for name, _, c in staged_bot.script.asked if name == "INTENT_PROMPT"][-1]
    assert "надо молоко" in shown and first.text.splitlines()[0][:20] in shown
    # Another chat starts clean.
    await staged_bot.orch.handle_text(CHAT + 1, USER, "привет")
    shown = [c for name, _, c in staged_bot.script.asked if name == "INTENT_PROMPT"][-1]
    assert "надо молоко" not in shown
    memory.close()


def test_only_answers_are_offered_for_saving_without_their_marks(tmp_path):
    """Answers are remembered as such, next to the reply: a refusal or a hit list that starts
    with the same mark is never one."""
    from datetime import UTC, datetime

    from app.audit.store import AuditStore
    from app.conversation.memory import ANSWER, BOT, USER, ChatMemory
    from app.pointing import clean_answer

    db = tmp_path / "bot.sqlite"
    audit = AuditStore(db)
    audit.migrate()
    audit.close()
    memory = ChatMemory(db)
    t = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
    memory.add(1, 7, USER, "что такое брутализм?", t)
    memory.add(1, 7, BOT, texts.ANSWER_GENERAL.format(answer=ANSWER), t)
    memory.add(1, 7, ANSWER, ANSWER, t)
    memory.add(1, 7, USER, "сколько стоит билет?", t)
    memory.add(1, 7, BOT, texts.ANSWER_WEB_FAILED.format(error="timeout"), t)  # not an answer
    said = memory.recent(1, t)
    assert [text for role, text in said if role == ANSWER] == [ANSWER]
    memory.close()
    sources = texts.VAULT_ANSWER_SOURCES.format(links="[Ремонт](obsidian://x)")
    shown = texts.VAULT_ANSWER.format(answer="Плитка серая.") + "\n" + sources
    assert clean_answer(shown) == "Плитка серая."


# ---- from the branch review ------------------------------------------------------------------

async def test_a_search_that_asks_back_is_asked_not_a_broken_turn(index):
    from app.llm.research import ResearchQuestion

    script = Script(INTENT_PROMPT={"intent": "ask"}, ASK_PROMPT=ASK)
    ask = AnswerAsk({"answer": "", "sources": [], "more": [], "kind": "web", "web": "билет"})

    async def research(message, query, media):
        raise ResearchQuestion("Куда и на какую дату?")

    turn = await pipe(index, script, VaultAnswerer(ask, "haiku")).handle(
        "сколько стоит билет?", research=research)
    assert turn.reply_line() == texts.ANSWER_WEB_ASK.format(question="Куда и на какую дату?")


async def test_a_question_mark_inside_a_link_is_not_a_question(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "x"})
    turn = await pipe(index, script).handle("посмотреть позже https://youtu.be/abc?si=xyz")
    # Filed (the inbox line keeps the link), and the question stage was never asked.
    assert turn.writes and len(script.shown) == 2  # intent + target, nothing more


async def test_save_this_landing_on_the_inbox_becomes_a_note_of_the_answer(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "x"})
    turn = await pipe(index, script).handle("запиши это", answers=(ANSWER,))
    [write] = turn.writes
    assert write.path.startswith(f"{texts.VAULT_NOTES_DIR}/")
    assert "голый бетон" in index.read(write.path)


# ---- Google sources in an answer -----------------------------------------------------------------

async def test_an_answer_reads_google_sections_and_names_them_without_links(index):
    from app.chats import ChatRef
    from app.google.sources import Found

    class FakeGoogle:
        def __init__(self):
            self.asked = []

        async def find(self, query, chat_id, *, owner):
            self.asked.append((chat_id, owner))
            return [Found(source="Рейтинги", title="лист", text="Ганнибал — avg: 3.1",
                          instruction="строка — книга", score=3)]

        def sign_in_notice(self):
            return ""

    google = FakeGoogle()
    script = Script(INTENT_PROMPT={"intent": "ask"}, ASK_PROMPT=ASK)
    ask = AnswerAsk(*[{"answer": "3.1", "sources": ["g1"], "more": [], "kind": "notes",
                       "web": ""} for _ in range(2)])
    p = VaultPipeline(index, VaultWriter(index, now=lambda: NOW), StagedFiler(script, "haiku"),
                      now=lambda: NOW, answerer=VaultAnswerer(ask, "haiku"), google=google)
    turn = await p.handle("какой рейтинг у Ганнибала?", chat=ChatRef(1, 1, owner=True))
    shown = ask.shown[0][1]
    assert "g1: Рейтинги — лист" in shown and "строка — книга" in shown
    line = turn.reply_line()
    assert "Рейтинги — лист" in line and "docs.google" not in line and "](" not in line
    # In a group even an owner gets the group's sources only, never the Drive.
    await p.handle("какой рейтинг у Ганнибала?", chat=ChatRef(-100, 1, owner=True))
    assert google.asked == [(1, True), (-100, False)]


# ---- admin settings reach the vault side ------------------------------------------------------

async def test_the_admin_pages_web_words_are_the_ones_the_vault_uses(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    APPEND_PROMPT={"heading": "", "body": [], "lookup": "", "web": "полки",
                                   "media": "text"})
    searched = []

    async def research(message, query, media):
        searched.append(query)
        return "найдено"

    p = VaultPipeline(index, VaultWriter(index, now=lambda: NOW), StagedFiler(script, "haiku"),
                      now=lambda: NOW, web_words=lambda: ("разузнай",))
    await p.handle("разузнай про полки и допиши в дом", research=research)
    assert searched == ["полки"]  # «разузнай» is not a built-in web word, the page's is


async def test_the_admin_pages_date_properties_are_the_ones_the_agenda_uses(tmp_path):
    from datetime import date

    (tmp_path / "Встреча.md").write_text("---\nкогда: 2026-10-16\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    p = VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                      StagedFiler(Script(), "haiku"), now=lambda: NOW,
                      date_props=lambda: ("когда",))
    assert "Встреча" in p.digest(date(2026, 10, 16))
