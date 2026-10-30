"""The staged Obsidian reader: intent, then one target, then that target's details — and the
gate that checks every returned value against the message."""

from __future__ import annotations

import asyncio

import pytest

from app.llm import staged_prompts as P
from app.vault.filer import VaultContext, check
from app.vault.index import VaultIndex
from app.vault.staged import StagedFiler, _Gate, tokens

GUIDE = ("- Книги — заметка на книгу в папке «Книги»: `status` (To read / Reading / Read)\n"
         "- Встречи книжного клуба — в папке «Клуб». Свойства `book`, `author`, `date`.\n"
         "## Области\n- [[дом]] `#home` — домашние дела\n- [[club]] `#club` — книжный клуб\n")


def ctx(**over) -> VaultContext:
    base = dict(
        guide=GUIDE, folders=["Заметки", "Книги", "Клуб"], tags=["home", "club"],
        known_notes=["Проекты", "Софт"],
        open_tasks=["Забрать посылку из кауп #personal", "платить счета #home 📅 2026-09-15"],
        groceries=["молоко", "яйца"], tasks_note="Задачи", daily_folder="Дневник",
        today="2026-09-30", weekday="среда",
        folder_props={"Клуб": ["author", "book", "date"], "Книги": ["author", "status"]},
        note_props={"Проекты": ["status"]}, note_headings={"Проекты": ["telega", "идеи"]})
    base.update(over)
    return VaultContext(**base)


class Script:
    """Answers each stage by which prompt it was asked with, and records what it was shown."""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict, str]] = []

    async def __call__(self, system: str, schema: dict, content: str,
                       max_tokens: int = 0):
        name = next(k for k, v in vars(P).items() if k.endswith("_PROMPT") and v == system)
        self.asked.append((name, schema, content))
        if name == "TARGET_PROMPT" and name not in self.answers:
            # Asked alongside the intent and thrown away for anything but an "add".
            await asyncio.sleep(0)
            raise KeyError(name)
        answer = self.answers[name]
        if isinstance(answer, list):  # one answer per call of this stage
            answer = answer.pop(0)
        return answer, 100, 10

    @property
    def stages(self) -> list[str]:
        return [name for name, _, _ in self.asked]

    @property
    def details(self) -> list[str]:
        """The stages after intent and target, which are always asked (together)."""
        return [s for s in self.stages if s not in ("INTENT_PROMPT", "TARGET_PROMPT")]


def key_of(script: Script, stage: str, label: str) -> str:
    """The key the code gave to the place whose line contains `label`."""
    content = next(c for name, _, c in script.asked if name == stage)
    line = next(ln for ln in content.splitlines() if label in ln)
    return line.split(":", 1)[0]


CLUB = ("Добавь Доктор Живаго и регистрация Глуховского список следующих встреч книжного клуба "
        "я Потом добавлю даты и описание")


async def test_the_dictated_meeting_message_becomes_two_meetings_and_nothing_else():
    """The message the one-question filer got wrong six times out of six: a book in «Книги»
    and an empty note named after the list. Here the target is one place for the whole
    message, and the details are asked with the meeting fields only."""
    script = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f3"},
        FOLDER_PROMPT={"items": [
            {"title": "Доктор Живаго", "body": [], "tags": [],
             "props": [{"name": "book", "value": "Доктор Живаго"},
                       {"name": "author", "value": "Борис Пастернак"},   # never said
                       {"name": "date", "value": "2026-10-05"}]},        # never said
            {"title": "Регистрация", "body": [], "tags": ["club"],
             "props": [{"name": "book", "value": "Регистрация"},
                       {"name": "author", "value": "Глуховский"}]},
            {"title": "Встречи клуба на осень", "props": [], "body": [], "tags": []}]})

    actions, prompt_tokens, _ = await StagedFiler(script, "haiku").file(CLUB, ctx())

    assert script.stages == ["INTENT_PROMPT", "TARGET_PROMPT", "FOLDER_PROMPT"]
    assert key_of(script, "TARGET_PROMPT", "папка «Клуб»") == "f3"
    assert [(a["action"], a["folder"], a["title"]) for a in actions[:2]] == [
        ("note", "Клуб", "Доктор Живаго"), ("note", "Клуб", "Регистрация")]
    # An author and a date the user never named are not written.
    assert actions[0]["props"] == [{"name": "book", "value": "Доктор Живаго"}]
    assert actions[1]["props"] == [{"name": "book", "value": "Регистрация"},
                                   {"name": "author", "value": "Глуховский"}]
    assert prompt_tokens == 300


async def test_the_target_stage_offers_exactly_one_key_and_shows_what_each_folder_holds():
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "d"})

    actions, _, _ = await StagedFiler(script, "haiku").file("сегодня ходили в кино", ctx())

    _, schema, content = script.asked[1]
    assert schema["properties"]["target"]["enum"] == [
        "t", "g", "d", "f1", "f2", "f3", "n1", "n2", "x"]
    assert "свойства: author, book, date" in content
    assert actions == [{"action": "log", "text": "сегодня ходили в кино"}]  # no third call


async def test_the_target_stage_sees_the_whole_grocery_registry():
    """What was bought before is the surest sign of a grocery: twenty names cut a household's
    registry short (it already has more), and an item past them read as a one-off purchase."""
    names = [f"вещь{i}" for i in range(60)]
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "d"})

    await StagedFiler(script, "haiku").file("сегодня ходили в кино", ctx(groceries=names))

    assert "вещь59" in script.asked[1][2]


async def test_the_target_stage_gives_its_reason_before_the_key():
    """«купить таблетки для посудомойки, тряпки» went to the tasks as a bare key, from Haiku
    and Sonnet alike; asked to say which rule fits first, both took the groceries page."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "d"})

    await StagedFiler(script, "haiku").file("сегодня ходили в кино", ctx())

    _, schema, _ = script.asked[1]
    assert list(schema["properties"]) == ["why", "target"]
    assert schema["required"] == ["why", "target"]


async def test_a_list_of_chores_for_today_is_one_task_each_with_the_date():
    script = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
        TASKS_PROMPT={"items": [
            {"text": "покушать", "due": "2026-09-30", "repeat": "", "heading": "дом",
             "tag": "home", "countdown": False},
            {"text": "почистить зубы", "due": "2026-09-30", "repeat": "every day",
             "heading": "здоровье", "tag": "health", "countdown": False}]})

    actions, _, _ = await StagedFiler(script, "haiku").file(
        "дела на сегодня покушать почистить зубы", ctx())

    assert [(a["text"], a["due"]) for a in actions] == [
        ("покушать", "2026-09-30"), ("почистить зубы", "2026-09-30")]
    assert actions[0]["heading"] == "дом" and actions[0]["tags"] == ["home"]
    # A heading the guide does not know would grow the task file a new section; a tag the
    # vault does not use is not one.
    assert actions[1]["heading"] == "" and actions[1]["tags"] == []
    assert actions[1]["repeat"] == "every day"


async def test_a_due_date_is_kept_only_when_the_message_names_a_time():
    answer = {"items": [{"text": "заказать очки", "due": "2026-09-30", "repeat": "",
                         "heading": "", "tag": "", "countdown": False}]}
    for message, due in (("надо заказать новые очки", ""),
                         ("заказать очки до пятницы", "2026-09-30"),
                         ("посмотреть что внутри коробки с очками", "")):
        script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                        TASKS_PROMPT=answer)
        actions, _, _ = await StagedFiler(script, "haiku").file(message, ctx())
        assert actions[0]["due"] == due, message


async def test_a_time_the_message_names_is_kept_and_one_it_does_not_is_not():
    """«вторник 17:30 смотреть квартиру oak lane 4» was filed with its date and without 17:30."""
    for message, time, said in (
            ("вторник 17:30 смотреть квартиру oak lane 4", "17:30", "17:30"),
            ("в пятницу в 6 вечера забрать посылку", "18:00", "18:00"),
            ("завтра утром позвонить в банк", "9:00", "09:00"),
            ("завтра позвонить в банк", "09:00", ""),          # the model's own pick
            ("сходить на elm street 14", "25:00", "")):             # not a time at all
        script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                        TASKS_PROMPT={"items": [{"text": "дело", "due": "", "time": time,
                                                 "repeat": "", "heading": "", "tag": "",
                                                 "countdown": False}]})
        actions, _, _ = await StagedFiler(script, "haiku").file(message, ctx())
        assert actions[0]["time"] == said, message


async def test_a_task_moved_to_another_time_gets_that_time():
    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "o2"},
                    CHANGE_TASK_PROMPT={"due": "", "time": "19:00", "done": False})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "счета перенеси на 19:00", ctx())
    assert actions == [{"action": "update", "note": "Задачи", "time": "19:00",
                        "task": "платить счета #home 📅 2026-09-15"}]


async def test_groceries_keep_only_names_the_user_said_short_ones_included():
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                    GROCERY_PROMPT={"names": ["сыр", "молоко", "масло"]})

    actions, _, _ = await StagedFiler(script, "haiku").file("купить сыра и молока", ctx())

    assert actions == [{"action": "grocery", "body": ["сыр", "молоко"], "done": False}]


async def test_a_message_goes_to_one_place_and_the_target_stage_is_asked_once():
    """Three ways of letting a message reach several places were tried and each one hurt plain
    messages more than it helped mixed ones. The details stage's answer is final."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                    GROCERY_PROMPT={"names": ["молоко"], "rest": "запиши идею про сайт"})

    actions, _, _ = await StagedFiler(script, "haiku").file(
        "купи молоко и запиши идею про сайт", ctx())

    assert [a["action"] for a in actions] == ["grocery"]
    assert script.stages == ["INTENT_PROMPT", "TARGET_PROMPT", "GROCERY_PROMPT"]
    assert "rest" not in script.asked[2][1]["properties"]


async def test_nonsense_is_kept_in_the_inbox_without_another_question():
    """«трум трум», «hj» and «Bonjour» were read as searches."""
    script = Script(INTENT_PROMPT={"intent": "unclear"})
    actions, _, _ = await StagedFiler(script, "haiku").file("трум трум", ctx())
    assert actions == [{"action": "inbox", "text": "трум трум"}]
    assert script.details == []


async def test_a_note_is_offered_only_when_the_guide_links_it_or_the_message_names_it():
    """`known_notes` holds every note that shares a word *start* with the message, which is
    how «Смотреть кино надо Uncharted» was appended to a note called «Смотритель»."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n2"},
                    APPEND_PROMPT={"heading": "", "body": ["Uncharted"]})

    actions, _, _ = await StagedFiler(script, "haiku").file(
        "Смотреть кино надо Uncharted", ctx(known_notes=["Смотритель", "Проекты"]))

    places = script.asked[1][2]
    assert "«Смотритель»" not in places and "«Проекты»" not in places
    assert "заметка «дом»" in places and "заметка «club»" in places  # the guide's hubs
    assert actions == [{"action": "append", "note": "club", "heading": "",
                        "body": ["Uncharted"]}]


async def test_done_ticks_the_task_it_names_by_key():
    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"targets": ["o1"]})

    actions, _, _ = await StagedFiler(script, "haiku").file("посылку забрал", ctx())

    assert actions == [{"action": "update", "note": "Задачи", "done": True,
                        "task": "Забрать посылку из кауп #personal"}]


async def test_done_with_no_such_task_is_a_line_about_the_day_and_bought_food_is_a_grocery():
    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"targets": ["none"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("починил кран", ctx())
    assert actions == [{"action": "log", "text": "починил кран"}]

    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"targets": ["g"]},
                    GROCERY_PROMPT={"names": ["молоко"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("купил молоко", ctx())
    assert actions == [{"action": "grocery", "body": ["молоко"], "done": True}]


async def test_done_ticks_every_task_a_list_names():
    """«Отметь выполненными про выводы, голову мыть, …, зубы и рассказ» named seven tasks; asked
    for one key the model answered "none" and the request was kept in the diary."""
    script = Script(INTENT_PROMPT={"intent": "done"},
                    DONE_PROMPT={"targets": ["o1", "o2", "o1"]})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "отметь выполненными посылку и счета", ctx())
    assert actions == [
        {"action": "update", "note": "Задачи", "done": True,
         "task": "Забрать посылку из кауп #personal"},
        {"action": "update", "note": "Задачи", "done": True,
         "task": "платить счета #home 📅 2026-09-15"}]


async def test_asked_to_tick_a_task_that_is_not_there_is_not_a_diary_line():
    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"targets": []})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "отметь выполненным полив цветов", ctx())
    assert [a["action"] for a in actions] == ["inbox"]


async def test_change_reschedules_a_task_or_edits_a_note():
    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "o2"},
                    CHANGE_TASK_PROMPT={"due": "2026-10-02", "done": False})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "счета перенеси на пятницу", ctx())
    assert actions == [{"action": "update", "note": "Задачи", "due": "2026-10-02",
                        "task": "платить счета #home 📅 2026-09-15"}]

    # n1 and n2 are the guide's hubs; «Проекты» is offered because the message names it.
    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "n3"},
                    CHANGE_NOTE_PROMPT={"kind": "rewrite", "props": [], "heading": "идеи"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "сократи раздел идеи в Проектах", ctx())
    assert actions == [{"action": "rewrite", "note": "Проекты", "heading": "идеи",
                        "text": "сократи раздел идеи в Проектах"}]

    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "n3"},
                    CHANGE_NOTE_PROMPT={"kind": "props", "heading": "", "props": [
                        {"name": "status", "value": "Read"},
                        {"name": "author", "value": "Кто-то"}]})
    actions, _, _ = await StagedFiler(script, "haiku").file("Проекты статус read", ctx())
    assert actions == [{"action": "update", "note": "Проекты",
                        "props": [{"name": "status", "value": "Read"}]}]


async def test_move_names_the_source_by_key_and_the_destination_in_the_users_words():
    script = Script(INTENT_PROMPT={"intent": "move"},
                    MOVE_PROMPT={"source": "n3", "to": "Софт", "what": "гитхаб проекты"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "перемести гитхаб проекты из Проектов в Софт", ctx())
    assert actions == [{"action": "move", "note": "Проекты", "to": "Софт",
                        "text": "гитхаб проекты"}]

    # A destination the user never named, and a vault with nothing to move from: the inbox.
    script = Script(INTENT_PROMPT={"intent": "move"},
                    MOVE_PROMPT={"source": "n1", "to": "Архив", "what": "всё"})
    actions, _, _ = await StagedFiler(script, "haiku").file("перенеси проекты", ctx())
    assert actions[0]["action"] == "inbox"
    script = Script(INTENT_PROMPT={"intent": "move"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "перенеси", ctx(known_notes=[], guide=""))
    assert actions[0]["action"] == "inbox" and script.details == []


@pytest.mark.parametrize("answer, expected", [
    ({"kind": "overdue"}, {"action": "agenda", "scope": "overdue"}),
    ({"kind": "now"}, {"action": "agenda", "scope": "now"}),
    ({"kind": "groceries"}, {"action": "grocery", "scope": "list", "body": []}),
    ({"kind": "day", "due_from": "2026-10-02", "due_to": "2026-10-02"},
     {"action": "agenda", "scope": "day", "due_from": "2026-10-02", "due_to": "2026-10-02"}),
    ({"kind": "search", "text": "борщ", "folder": "Заметки", "tag": "home", "props": []},
     {"action": "search", "text": "борщ", "folder": "Заметки", "tags": ["home"], "props": []}),
])
async def test_questions_write_nothing(answer, expected):
    full = {"due_from": "", "due_to": "", "text": "", "folder": "", "tag": "", "props": [],
            **answer}
    script = Script(INTENT_PROMPT={"intent": "ask"}, ASK_PROMPT=full)
    actions, _, _ = await StagedFiler(script, "haiku").file("что у меня в пятницу про борщ", ctx())
    assert actions == [expected]


async def test_whatever_the_stages_answer_the_result_passes_the_same_gate_as_before(tmp_path):
    """The staged reader returns the raw actions the old one does, so `check` still decides
    what may be written: a folder that is not there, a note that is gone."""
    (tmp_path / "Клуб").mkdir()
    (tmp_path / "Клуб/встреча.md").write_text("---\nbook: x\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f3"},
                    FOLDER_PROMPT={"items": [{"title": "Доктор Живаго", "props": [
                        {"name": "book", "value": "Доктор Живаго"}], "body": [], "tags": []}]})

    raw, _, _ = await StagedFiler(script, "haiku").file(CLUB, ctx())
    [action] = check(raw, index, CLUB)

    assert (action.action, action.folder, action.title) == ("note", "Клуб", "Доктор Живаго")
    assert action.props == {"book": "Доктор Живаго"}


def test_the_gate_sees_short_words_and_knows_a_time_from_a_word_that_contains_one():
    assert tokens("сыра, Чай и ёж") == {"сыра", "чай", "и", "еж"}
    assert _Gate("купить борща", "").said("борщ")  # a four-letter word, inflected
    assert _Gate("два кило сыра", "").said("сыр")  # a short one
    gate = _Gate("купить сыр 12 октября, статус читаю", GUIDE)
    assert gate.said("сыр") and not gate.said("Пастернак")
    assert gate.prop("To read") == "To read"        # from the user's guide
    assert gate.prop("Борис Пастернак") is None     # from nowhere
    assert gate.prop("") == "" and gate.prop("FALSE") == "false"
    assert gate.date("2026-10-12") == "2026-10-12" and gate.date("12 октября") == ""
    assert _Gate("посмотреть что внутри", "").date("2026-10-12") == ""


async def test_the_pipeline_writes_what_the_staged_reader_read(tmp_path):
    """The reader is a drop-in: the pipeline, the writer and the undo neither know nor care
    which of the two produced the actions."""
    from app.vault.pipeline import VaultPipeline
    from app.vault.writer import VaultWriter

    (tmp_path / "Клуб").mkdir()
    for name in ("2026-08-31 Танго", "2026-09-21 Кувшинки"):
        (tmp_path / f"Клуб/{name}.md").write_text(
            "---\nbook: x\nevent_posted: true\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    script = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
        FOLDER_PROMPT={"items": [
            {"title": "Доктор Живаго", "body": [], "tags": [],
             "props": [{"name": "book", "value": "Доктор Живаго"}]},
            {"title": "Регистрация", "body": [], "tags": [],
             "props": [{"name": "author", "value": "Глуховский"}]}]})
    pipeline = VaultPipeline(index, VaultWriter(index), StagedFiler(script, "haiku"), None)

    turn = await pipeline.handle(CLUB)

    assert sorted(w.note for w in turn.writes) == ["Доктор Живаго", "Регистрация"]
    assert "event_posted: false" in (tmp_path / "Клуб/Регистрация.md").read_text(encoding="utf-8")
    assert "свойства: book, event_posted" in script.asked[1][2]
    await pipeline.undo(turn.undos)
    assert not (tmp_path / "Клуб/Регистрация.md").exists()


async def test_an_empty_details_answer_gets_one_narrower_question_before_the_inbox():
    """«добавь область таймлапсы»: the folder was chosen right and then nothing was listed for
    it, three times out of three. Asked only what to call the note, the model answers."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                    FOLDER_PROMPT={"items": []}, TITLE_PROMPT={"title": "таймлапсы"})
    actions, _, _ = await StagedFiler(script, "haiku").file("добавь область таймлапсы", ctx())
    assert [(a["action"], a["folder"], a["title"]) for a in actions] == [
        ("note", "Заметки", "таймлапсы")]

    # A title the user never said is still refused, and the message is kept in the inbox.
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                    FOLDER_PROMPT={"items": []}, TITLE_PROMPT={"title": "Видеосъёмка"})
    actions, _, _ = await StagedFiler(script, "haiku").file("добавь область таймлапсы", ctx())
    assert actions == [{"action": "inbox", "text": "добавь область таймлапсы"}]


async def test_a_task_that_is_already_open_is_not_filed_a_second_time():
    """Same words as an open line, in any form and order: the task is there. The answer points
    at the existing line instead of adding a twin under it."""
    items = [{"text": "платить счета", "due": "", "repeat": "", "heading": "дом", "tag": "home",
              "countdown": False},
             {"text": "заказать очки", "due": "", "repeat": "", "heading": "", "tag": "",
              "countdown": False}]
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                    TASKS_PROMPT={"items": items})

    actions, _, _ = await StagedFiler(script, "haiku").file(
        "надо платить счета и заказать очки", ctx())

    assert actions[0] == {"action": "update", "note": "Задачи",
                          "task": "платить счета #home 📅 2026-09-15"}
    assert actions[1]["action"] == "task" and actions[1]["text"] == "заказать очки"


# ---- the vault plans for itself: lookups and web searches ---------------------------------

async def test_a_set_the_user_did_not_list_is_looked_up_and_each_item_becomes_a_note():
    """«я прочитал все романы Пелевина, добавь в список прочитанных»: the vault asks the lookup
    model for the list and writes one note per book."""
    light = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f2"},
        FOLDER_PROMPT={"items": [], "lookup": "романы Пелевина, все прочитаны", "web": "",
                       "media": "text"})
    strong = Script(LOOKUP_NOTES_PROMPT={"items": [
        {"title": "Омон Ра", "props": [{"name": "author", "value": "Виктор Пелевин"},
                                      {"name": "status", "value": "Read"},
                                      {"name": "url", "value": "https://example.com/omon"}]},
        {"title": "Generation П", "props": [{"name": "date", "value": "1999-03-01"}]},
        {"title": "омон ра", "props": []}]})  # the same book twice is one note

    actions, prompt_tokens, _ = await StagedFiler(light, "haiku", lookup=strong).file(
        "я прочитал все романы Пелевина добавь в список прочитанных книг", ctx())

    assert [(a["folder"], a["title"]) for a in actions] == [
        ("Книги", "Омон Ра"), ("Книги", "Generation П")]
    # Knowing the author is what the lookup was for; a link or a date from memory is not.
    assert actions[0]["props"] == [{"name": "author", "value": "Виктор Пелевин"},
                                   {"name": "status", "value": "Read"}]
    assert actions[1]["props"] == []
    assert strong.stages == ["LOOKUP_NOTES_PROMPT"] and "TITLE_PROMPT" not in light.stages
    assert "романы Пелевина, все прочитаны" in strong.asked[0][2]
    assert prompt_tokens == 400  # four calls, the lookup included


async def test_a_web_search_is_asked_for_only_when_the_user_asked_for_one():
    folder = {"items": [{"title": "рецепт борща", "props": [], "body": [], "tags": []}],
              "lookup": "", "web": "рецепт борща из копчёной курицы", "media": "text"}
    for message, research in (("найди рецепт борща и запиши в заметки",
                               "рецепт борща из копчёной курицы"),
                              ("запиши рецепт борща в заметки", None)):
        script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                        FOLDER_PROMPT=dict(folder))
        [action], _, _ = await StagedFiler(script, "haiku").file(message, ctx())
        assert action.get("research") == research, message


async def test_a_list_page_gets_the_looked_up_lines_and_a_search_goes_on_the_append():
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    APPEND_PROMPT={"heading": "", "body": [], "lookup": "фильмы Нолана",
                                   "web": "", "media": "text"},
                    LOOKUP_LINES_PROMPT={"items": ["Помни", "Начало", " "]})
    [action], _, _ = await StagedFiler(script, "haiku").file(
        "добавь в дом все фильмы Нолана", ctx())
    assert action["body"] == ["Помни", "Начало"]

    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    APPEND_PROMPT={"heading": "", "body": [], "lookup": "",
                                   "web": "уход за фикусом", "media": "text_and_images"})
    [action], _, _ = await StagedFiler(script, "haiku").file(
        "найди как ухаживать за фикусом и допиши в дом", ctx())
    assert action["body"] == [] and action["research"] == "уход за фикусом"
    assert action["media"] == "text_and_images"


async def test_looked_up_chores_take_the_date_the_user_gave_the_whole_list():
    script = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
        TASKS_PROMPT={"items": [{"text": "собрать рюкзак", "due": "2026-10-01", "repeat": "",
                                 "heading": "дом", "tag": "home", "countdown": False}],
                      "lookup": "что взять в поход"},
        LOOKUP_TASKS_PROMPT={"items": ["взять палатку", "взять спальник"]})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "завтра поход, собрать рюкзак и всё что нужно взять", ctx())
    assert [(a["text"], a["due"], a["tags"]) for a in actions] == [
        ("собрать рюкзак", "2026-10-01", ["home"]), ("взять палатку", "2026-10-01", ["home"]),
        ("взять спальник", "2026-10-01", ["home"])]


async def test_a_single_chore_is_never_expanded_into_a_made_up_checklist():
    """«надо внести траты» came back as five chores the user never mentioned: the tasks stage
    asked for a lookup, and a lookup on the task file invents work. It takes a word that asks
    for a set («все», «список», «что взять»)."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                    TASKS_PROMPT={"items": [{"text": "внести траты", "due": "", "repeat": "",
                                             "heading": "", "tag": "", "countdown": False}],
                                  "lookup": "как вести учёт трат"})
    actions, _, _ = await StagedFiler(script, "haiku").file("надо внести траты", ctx())
    assert [a["text"] for a in actions] == ["внести траты"]
    assert "LOOKUP_TASKS_PROMPT" not in script.stages


# ---- from the review of the 81 real messages ------------------------------------------------

async def test_titles_the_user_never_said_are_a_set_to_look_up_not_notes():
    """e18: asked for «все романы Пелевина», the details stage listed three novels from memory
    instead of asking for a lookup. «Чапаев и Пустота» got past the check only because of «и»."""
    light = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f2"},
        FOLDER_PROMPT={"items": [{"title": "Чапаев и Пустота", "props": [], "body": [],
                                  "tags": []}], "lookup": "", "web": "", "media": "text"})
    strong = Script(LOOKUP_NOTES_PROMPT={"items": [{"title": "Омон Ра", "props": []},
                                                   {"title": "Чапаев и Пустота", "props": []}]})
    message = "я прочитал все романы Пелевина добавь их в список прочитанных книг"

    actions, _, _ = await StagedFiler(light, "haiku", lookup=strong).file(message, ctx())

    assert [a["title"] for a in actions] == ["Омон Ра", "Чапаев и Пустота"]
    assert all(a.get("looked_up") for a in actions)  # both from the lookup, both guarded
    assert message in strong.asked[0][2]  # asked with the user's words, having none of its own


async def test_a_note_is_named_when_most_of_its_name_is_there_despite_dictation():
    """e93: «Найди мне страницу мою проворота тории … каждый вид и подвид» — «про» glued onto
    «ворота», and «виды» said as «вид». Two of three words is naming the note."""
    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "none"})
    await StagedFiler(script, "haiku").file(
        "Найди мне страницу мою проворота тории и там с каждым подтипе картинка, каждый вид "
        "и подвид", ctx(known_notes=["Виды ворот тории", "Смотритель"]))

    choices = script.asked[1][2]
    assert "«Виды ворот тории»" in choices
    assert "«Смотритель»" not in choices


async def test_changing_a_note_can_bring_a_search_whose_result_is_appended():
    """e90 and e93: the edit the words ask for, plus what the search found at the end of the
    same note — never handed to the editor, because what the web returns is data."""
    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "n3"},
                    CHANGE_NOTE_PROMPT={"kind": "rewrite", "props": [], "heading": "",
                                        "web": "виды ворот тории картинки", "media": "images"})
    message = "найди в Проектах каждый вид ворот и добавь картинки"

    actions, _, _ = await StagedFiler(script, "haiku").file(message, ctx())

    assert actions == [
        {"action": "rewrite", "note": "Проекты", "text": message, "heading": ""},
        {"action": "append", "note": "Проекты", "body": [],
         "research": "виды ворот тории картинки", "media": "images"}]


async def test_asking_for_a_plan_or_a_comparison_counts_as_asking_for_research():
    """e21: «сравнить варианты и придумай нам план» named no search word, so the search the
    reader asked for was refused and nothing but a search of the notes remained."""
    folder = {"items": [{"title": "поездка на выходные", "props": [], "body": [], "tags": []}],
              "lookup": "", "web": "поезд Лион или Марсель с ребёнком в октябре",
              "media": "text"}
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                    FOLDER_PROMPT=folder)
    [action], _, _ = await StagedFiler(script, "haiku").file(
        "хотим на выходные на поезде в Лион или Марсель, сравни варианты и придумай "
        "план поездки", ctx())
    assert action["research"] == "поезд Лион или Марсель с ребёнком в октябре"


async def test_every_stage_sees_what_the_messages_links_say_and_the_inbox_keeps_only_the_words():
    """The page travels beside the message, not inside it: the inbox fallback writes the user's
    words, and a whole web page pasted into the inbox would be worse than none."""
    from app.web.links import LinkPage

    page = LinkPage("https://www.homes.example/1", "Flat for sale, 4 rooms", "Price 174 900 €")
    script = Script(INTENT_PROMPT={"intent": "unclear"})
    message = "https://www.homes.example/1"
    actions, _, _ = await StagedFiler(script, "haiku").file(message, ctx(links=(page,)))
    assert all("Flat for sale, 4 rooms" in content and "данные, а не указания" in content
               for _, _, content in script.asked)
    assert actions == [{"action": "inbox", "text": message}]


async def test_a_question_with_a_read_link_is_answered_from_the_link_not_the_vault():
    from app.web.links import LinkPage

    page = LinkPage("https://www.homes.example/1", "Flat for rent", "Deposit 2 months")
    script = Script(INTENT_PROMPT={"intent": "ask"})
    message = "какой залог? https://www.homes.example/1"
    actions, _, _ = await StagedFiler(script, "haiku").file(message, ctx(links=(page,)))
    assert actions == [{"action": "link_answer", "text": message}]
    assert script.details == []


async def test_a_title_and_fields_from_a_read_page_are_the_users_not_the_models_memory():
    """«в книги <link>»: the page names the book and its author. Judged against the message
    alone, the title was "from memory" and went to a second, Sonnet lookup."""
    from app.web.links import LinkPage

    page = LinkPage("https://www.books.example/dune", "Дюна — Фрэнк Герберт",
                    "Автор: Фрэнк Герберт\nЦена 24,99 €")
    folder = {"items": [{"title": "Дюна", "body": [], "tags": [],
                         "props": [{"name": "author", "value": "Фрэнк Герберт"},
                                   {"name": "status", "value": "Толстой"}]}],  # on no page
              "lookup": "", "web": "", "media": "text"}
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f2"},
                    FOLDER_PROMPT=folder)
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "в книги https://www.books.example/dune", ctx(links=(page,)))
    assert script.stages == ["INTENT_PROMPT", "TARGET_PROMPT", "FOLDER_PROMPT"]
    assert [(a["title"], a["props"]) for a in actions] == [
        ("Дюна", [{"name": "author", "value": "Фрэнк Герберт"}])]


async def test_a_product_from_a_read_page_goes_on_the_grocery_list():
    from app.web.links import LinkPage

    page = LinkPage("https://grocer.example/p/1", "Milk Dairyland 2.5% 1 l", "1,39 €")
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                    GROCERY_PROMPT={"names": ["milk Dairyland"]})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "купить https://grocer.example/p/1", ctx(links=(page,)))
    assert actions[0]["body"] == ["milk Dairyland"]


async def test_a_page_full_of_numbers_does_not_let_an_invented_due_date_through():
    from app.web.links import LinkPage

    page = LinkPage("https://cinema.example/1", "Dune", "Showtimes 2026-10-05 19:00, 21:30")
    answer = {"items": [{"text": "сходить на Дюну", "due": "2026-09-30", "repeat": "",
                         "heading": "", "tag": "", "countdown": False}]}
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                    TASKS_PROMPT=answer)
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "сходить на Дюну https://cinema.example/1", ctx(links=(page,)))
    assert actions[0]["due"] == ""


async def test_the_target_is_asked_alongside_the_intent_and_dropped_for_other_intents():
    """Two of the three calls of an ordinary message run at the same time, so the answer
    comes one call sooner. A message that is not an "add" has still asked, and the answer
    is thrown away: no details stage runs on it."""
    order: list[str] = []

    class Slow(Script):
        async def __call__(self, system, schema, content, max_tokens=0):
            name = "intent" if system == P.INTENT_PROMPT else "other"
            order.append(f"{name} asked")
            if name == "intent":
                await asyncio.sleep(0.01)  # the target question is in flight meanwhile
            order.append(f"{name} answered")
            return await super().__call__(system, schema, content, max_tokens)

    script = Slow(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                  GROCERY_PROMPT={"names": ["молоко"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("купи молоко", ctx())
    assert [a["action"] for a in actions] == ["grocery"]
    assert order.index("other asked") < order.index("intent answered")
    assert script.details == ["GROCERY_PROMPT"]

    script = Script(INTENT_PROMPT={"intent": "done"}, TARGET_PROMPT={"target": "t"},
                    DONE_PROMPT={"targets": ["o1"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("забрал посылку", ctx())
    assert actions[0]["action"] == "update" and script.details == ["DONE_PROMPT"]


async def test_a_target_call_that_failed_is_asked_again_rather_than_lost():
    calls = {"n": 0}

    class Flaky(Script):
        async def __call__(self, system, schema, content, max_tokens=0):
            if system == P.TARGET_PROMPT and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("connection reset")
            return await super().__call__(system, schema, content, max_tokens)

    script = Flaky(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                   GROCERY_PROMPT={"names": ["молоко"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("купи молоко", ctx())
    assert [a["action"] for a in actions] == ["grocery"]


async def test_a_target_call_that_fails_after_the_intent_failed_is_not_left_unretrieved():
    """The intent call fails and the target call is cancelled, but the client turns the
    cancellation into an error of its own (an HTTP connection closing does): that exception
    must still be retrieved, or asyncio logs "Task exception was never retrieved"."""
    import gc

    class Down(Script):
        async def __call__(self, system, schema, content, max_tokens=0):
            if system == P.INTENT_PROMPT:
                await asyncio.sleep(0)  # the target call is in flight
                raise RuntimeError("down")
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise RuntimeError("connection closed") from None
            return {}, 0, 0

    seen: list[str] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: seen.append(context.get("message", "")))
    try:
        with pytest.raises(RuntimeError):
            await StagedFiler(Down(), "haiku").file("купи молоко", ctx())
        await asyncio.sleep(0.01)  # the cancelled target call ends with its own error
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert not any("never retrieved" in m for m in seen), seen


def test_a_one_letter_word_does_not_vouch_for_every_word_it_begins():
    """«и» (and) began «икра»: a product nobody named passed the gate as said. A short word
    still matches its own longer form: «сыр» and «сыра»."""
    gate = _Gate("купить молоко и яйца", "")
    assert not gate.said("икра")
    assert gate.said("яйца")
    assert _Gate("купить сыр", "").said("сыра")


async def test_asked_to_find_what_the_folders_notes_carry_the_lookup_fills_it():
    """«создай встречи … найди авторов» named the books but wrote no author: a value the
    message does not hold is refused, and nothing asked for the lookup. Asked to find, the
    lookup model fills the folder's properties for the items named — an author, never a date
    or a link from memory."""
    light = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f3"},
        FOLDER_PROMPT={"items": [
            {"title": "Дом листьев", "props": [{"name": "date", "value": "2026-12-07"}],
             "body": [], "tags": []},
            {"title": "Сатанинское танго", "props": [], "body": [], "tags": []}],
            "lookup": "", "web": "", "media": "text", "fill": True})
    strong = Script(FILL_PROPS_PROMPT={"items": [
        {"title": "дом листьев", "props": [{"name": "author", "value": "Марк Данилевский"},
                                          {"name": "date", "value": "2001-03-07"}]},
        {"title": "Сатанинское танго", "props": [
            {"name": "author", "value": "Ласло Краснахоркаи"},
            {"name": "date", "value": "1985-01-01"},
            {"name": "url", "value": "https://example.com/tango"}]}]})

    actions, _, _ = await StagedFiler(light, "haiku", lookup=strong).file(
        "создай в клубе встречи: 7 декабря дом листьев, сатанинское танго. найди авторов",
        ctx())

    assert actions[0]["props"] == [{"name": "date", "value": "2026-12-07"},
                                   {"name": "author", "value": "Марк Данилевский"}]
    assert actions[1]["props"] == [{"name": "author", "value": "Ласло Краснахоркаи"}]
    assert strong.stages == ["FILL_PROPS_PROMPT"]
    shown = strong.asked[0][2]
    assert "Дом листьев" in shown and "Сатанинское танго" in shown and "author" in shown


async def test_not_asked_to_find_nothing_is_filled_from_memory():
    light = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f3"},
        FOLDER_PROMPT={"items": [{"title": "Дом листьев", "props": [], "body": [],
                                  "tags": []}], "lookup": "", "web": "", "media": "text"})
    strong = Script()

    actions, _, _ = await StagedFiler(light, "haiku", lookup=strong).file(
        "создай в клубе встречу дом листьев", ctx())

    assert actions[0]["props"] == [] and strong.stages == []


async def test_a_new_note_gets_tags_only_where_its_folders_notes_have_them():
    """The meetings came out tagged with the hub's tag, which no meeting in the folder carries:
    a note follows its folder's notes, tags included."""
    answer = {"items": [{"title": "Дом листьев", "props": [], "body": [], "tags": ["club"]}],
              "lookup": "", "web": "", "media": "text"}
    untagged = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f3"},
                      FOLDER_PROMPT=dict(answer))
    actions, _, _ = await StagedFiler(untagged, "haiku").file(
        "создай в клубе встречу дом листьев", ctx())
    assert actions[0]["tags"] == []

    tagged = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f3"},
                    FOLDER_PROMPT=dict(answer))
    actions, _, _ = await StagedFiler(tagged, "haiku").file(
        "создай в клубе встречу дом листьев", ctx(folder_tagged=frozenset({"Клуб"})))
    assert actions[0]["tags"] == ["club"]


def _book_with_author():
    return Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f2"},
                  FOLDER_PROMPT={"items": [{"title": "Омон Ра", "props": [
                      {"name": "author", "value": "Виктор Пелевин"}], "body": [], "tags": []}],
                      "lookup": "", "web": "", "media": "text"})


async def test_handed_inputs_are_shown_and_count_as_a_source():
    """A step of a plan is handed the results of the steps it needs: the stages see them, and
    a value taken from them is not one the model made up."""
    script = _book_with_author()
    handed = "#1: Омон Ра — Виктор Пелевин, 1992"
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "добавь в книги омон ра", ctx(inputs=handed, facts=handed))

    assert actions[0]["props"] == [{"name": "author", "value": "Виктор Пелевин"}]
    # Shown but not a source (a model's general answer, say): the author is refused.
    shown_only, _, _ = await StagedFiler(_book_with_author(), "haiku").file(
        "добавь в книги омон ра", ctx(inputs=handed))
    assert shown_only[0]["props"] == []
    folder = next(c for name, _, c in script.asked if name == "FOLDER_PROMPT")
    assert P.H_INPUTS in folder and "1992" in folder


async def test_without_inputs_nothing_changes():
    actions, _, _ = await StagedFiler(_book_with_author(), "haiku").file(
        "добавь в книги омон ра", ctx())
    assert actions[0]["props"] == []



def test_the_reader_asks_without_thinking_and_its_lookup_with(monkeypatch):
    """Sonnet 5 thinks unless told not to: as the reader, every stage would think, slow and
    over its token cap. A list from memory is the one ask that thinks — on the same model too."""
    from app.vault import staged

    made = []
    monkeypatch.setattr(staged, "claude_ask",
                        lambda sdk, model, health=None, *, think=True: made.append((model, think))
                        or (lambda *a: None))
    staged.StagedFiler.claude("key", "claude-sonnet-5", lookup_model="claude-sonnet-5")
    assert sorted(made) == [("claude-sonnet-5", False), ("claude-sonnet-5", True)]


async def test_the_stages_see_the_coming_week_by_weekday():
    """«напомни завтра … и в пятницу …» on a Thursday: «в пятницу» became Saturday, the model
    counting days itself. The coming week is given, each day with its weekday."""
    script = Script(INTENT_PROMPT={"intent": "remind"}, REMIND_PROMPT={
        "op": "add", "items": [], "target": "none"})

    await StagedFiler(script, "haiku").file("в пятницу забрать посылку",
                                            ctx(today="2026-10-22", weekday="четверг"))

    content = next(c for name, _, c in script.asked if name == "REMIND_PROMPT")
    assert "2026-10-23 пт (" in content and "2026-10-24 сб" in content
    assert "2026-10-28 ср" in content and "2026-10-29" not in content


def test_a_day_without_a_month_is_the_nearest_one():
    """«27 числа» on 20 October: Sonnet set 27 November, every time."""
    assert P.DAY_ONLY_RULE in P.REMIND_PROMPT and P.DAY_ONLY_RULE in P.TASKS_PROMPT
