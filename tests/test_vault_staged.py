"""The staged Obsidian reader: intent, then one target, then that target's details — and the
gate that checks every returned value against the message."""

from __future__ import annotations

import pytest

from app.llm import staged_prompts as P
from app.vault.filer import VaultContext, check
from app.vault.index import VaultIndex
from app.vault.staged import StagedFiler, _Gate, tokens

GUIDE = ("- Книги — заметка на книгу в папке «Книги»: `status` (To read / Reading / Read)\n"
         "- Встречи книжного клуба — в папке «Кнуб». Свойства `book`, `author`, `date`.\n"
         "## Области\n- [[дом]] `#home` — домашние дела\n- [[knub]] `#knub` — книжный клуб\n")


def ctx(**over) -> VaultContext:
    base = dict(
        guide=GUIDE, folders=["Заметки", "Книги", "Кнуб"], tags=["home", "knub"],
        known_notes=["пройекты", "pehmevara"],
        open_tasks=["Забрать посылку из кауп #personal", "платить счета #home 📅 2026-09-15"],
        groceries=["молоко", "яйца"], tasks_note="Задачи", daily_folder="Дневник",
        today="2026-09-30", weekday="среда",
        folder_props={"Кнуб": ["author", "book", "date"], "Книги": ["author", "status"]},
        note_props={"пройекты": ["status"]}, note_headings={"пройекты": ["telega", "идеи"]})
    base.update(over)
    return VaultContext(**base)


class Script:
    """Answers each stage by which prompt it was asked with, and records what it was shown."""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict, str]] = []

    async def __call__(self, system: str, schema: dict, content: str):
        name = next(k for k, v in vars(P).items() if k.endswith("_PROMPT") and v == system)
        self.asked.append((name, schema, content))
        answer = self.answers[name]
        if isinstance(answer, list):  # one answer per call of this stage
            answer = answer.pop(0)
        return answer, 100, 10

    @property
    def stages(self) -> list[str]:
        return [name for name, _, _ in self.asked]


def key_of(script: Script, stage: str, label: str) -> str:
    """The key the code gave to the place whose line contains `label`."""
    content = next(c for name, _, c in script.asked if name == stage)
    line = next(ln for ln in content.splitlines() if label in ln)
    return line.split(":", 1)[0]


KNUB = ("Добавь Доктор Живаго и регистрация Глуховского список следующих встреч книжного клуба "
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
            {"title": "Регистрация", "body": [], "tags": ["knub"],
             "props": [{"name": "book", "value": "Регистрация"},
                       {"name": "author", "value": "Глуховский"}]},
            {"title": "Встречи клуба на осень", "props": [], "body": [], "tags": []}]})

    actions, prompt_tokens, _ = await StagedFiler(script, "haiku").file(KNUB, ctx())

    assert script.stages == ["INTENT_PROMPT", "TARGET_PROMPT", "FOLDER_PROMPT"]
    assert key_of(script, "TARGET_PROMPT", "папка «Кнуб»") == "f3"
    assert [(a["action"], a["folder"], a["title"]) for a in actions[:2]] == [
        ("note", "Кнуб", "Доктор Живаго"), ("note", "Кнуб", "Регистрация")]
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
    assert script.stages == ["INTENT_PROMPT"]


async def test_a_note_is_offered_only_when_the_guide_links_it_or_the_message_names_it():
    """`known_notes` holds every note that shares a word *start* with the message, which is
    how «Смотреть кино надо Uncharted» was appended to a note called «Смотритель»."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n2"},
                    APPEND_PROMPT={"heading": "", "body": ["Uncharted"]})

    actions, _, _ = await StagedFiler(script, "haiku").file(
        "Смотреть кино надо Uncharted", ctx(known_notes=["Смотритель", "пройекты"]))

    places = script.asked[1][2]
    assert "«Смотритель»" not in places and "«пройекты»" not in places
    assert "заметка «дом»" in places and "заметка «knub»" in places  # the guide's hubs
    assert actions == [{"action": "append", "note": "knub", "heading": "",
                        "body": ["Uncharted"]}]


async def test_done_ticks_the_task_it_names_by_key():
    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"target": "o1"})

    actions, _, _ = await StagedFiler(script, "haiku").file("посылку забрал", ctx())

    assert actions == [{"action": "update", "note": "Задачи", "done": True,
                        "task": "Забрать посылку из кауп #personal"}]


async def test_done_with_no_such_task_is_a_line_about_the_day_and_bought_food_is_a_grocery():
    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"target": "none"})
    actions, _, _ = await StagedFiler(script, "haiku").file("починил кран", ctx())
    assert actions == [{"action": "log", "text": "починил кран"}]

    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"target": "g"},
                    GROCERY_PROMPT={"names": ["молоко"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("купил молоко", ctx())
    assert actions == [{"action": "grocery", "body": ["молоко"], "done": True}]


async def test_change_reschedules_a_task_or_edits_a_note():
    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "o2"},
                    CHANGE_TASK_PROMPT={"due": "2026-10-02", "done": False})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "счета перенеси на пятницу", ctx())
    assert actions == [{"action": "update", "note": "Задачи", "due": "2026-10-02",
                        "task": "платить счета #home 📅 2026-09-15"}]

    # n1 and n2 are the guide's hubs; «пройекты» is offered because the message names it.
    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "n3"},
                    CHANGE_NOTE_PROMPT={"kind": "rewrite", "props": [], "heading": "идеи"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "сократи раздел идеи в пройектах", ctx())
    assert actions == [{"action": "rewrite", "note": "пройекты", "heading": "идеи",
                        "text": "сократи раздел идеи в пройектах"}]

    script = Script(INTENT_PROMPT={"intent": "change"}, CHANGE_PROMPT={"target": "n3"},
                    CHANGE_NOTE_PROMPT={"kind": "props", "heading": "", "props": [
                        {"name": "status", "value": "Read"},
                        {"name": "author", "value": "Кто-то"}]})
    actions, _, _ = await StagedFiler(script, "haiku").file("пройекты статус read", ctx())
    assert actions == [{"action": "update", "note": "пройекты",
                        "props": [{"name": "status", "value": "Read"}]}]


async def test_move_names_the_source_by_key_and_the_destination_in_the_users_words():
    script = Script(INTENT_PROMPT={"intent": "move"},
                    MOVE_PROMPT={"source": "n3", "to": "pehmevara", "what": "гитхаб проекты"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "перемести гитхаб проекты из пройектов в pehmevara", ctx())
    assert actions == [{"action": "move", "note": "пройекты", "to": "pehmevara",
                        "text": "гитхаб проекты"}]

    # A destination the user never named, and a vault with nothing to move from: the inbox.
    script = Script(INTENT_PROMPT={"intent": "move"},
                    MOVE_PROMPT={"source": "n1", "to": "Архив", "what": "всё"})
    actions, _, _ = await StagedFiler(script, "haiku").file("перенеси проекты", ctx())
    assert actions[0]["action"] == "inbox"
    script = Script(INTENT_PROMPT={"intent": "move"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "перенеси", ctx(known_notes=[], guide=""))
    assert actions[0]["action"] == "inbox" and script.stages == ["INTENT_PROMPT"]


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
    (tmp_path / "Кнуб").mkdir()
    (tmp_path / "Кнуб/встреча.md").write_text("---\nbook: x\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f3"},
                    FOLDER_PROMPT={"items": [{"title": "Доктор Живаго", "props": [
                        {"name": "book", "value": "Доктор Живаго"}], "body": [], "tags": []}]})

    raw, _, _ = await StagedFiler(script, "haiku").file(KNUB, ctx())
    [action] = check(raw, index, KNUB)

    assert (action.action, action.folder, action.title) == ("note", "Кнуб", "Доктор Живаго")
    assert action.props == {"book": "Доктор Живаго"}


def test_the_gate_sees_short_words_and_knows_a_time_from_a_word_that_contains_one():
    assert tokens("сыра, Чай и ёж") == {"сыр", "чай", "и", "еж"}
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

    (tmp_path / "Кнуб").mkdir()
    for name in ("2026-08-31 Танго", "2026-09-21 Кувшинки"):
        (tmp_path / f"Кнуб/{name}.md").write_text(
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

    turn = await pipeline.handle(KNUB)

    assert sorted(w.note for w in turn.writes) == ["Доктор Живаго", "Регистрация"]
    assert "event_posted: false" in (tmp_path / "Кнуб/Регистрация.md").read_text(encoding="utf-8")
    assert "свойства: book, event_posted" in script.asked[1][2]
    await pipeline.undo(turn.undos)
    assert not (tmp_path / "Кнуб/Регистрация.md").exists()


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
