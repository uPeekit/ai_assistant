"""Fixing part of what a turn wrote (app/vault/fix.py): one question about the difference, code
that turns the answer into writes to take back and writes to make, and a gate on every value."""

from __future__ import annotations

from datetime import datetime

import pytest

from app import texts
from app.llm import staged_prompts as P
from app.vault.filer import context
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultAction, VaultWriter

NOW = datetime(2026, 10, 6, 18, 30)  # a Tuesday


class Script:
    """Answers each stage by its prompt, and records what each was shown."""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict, str]] = []

    async def __call__(self, system: str, schema: dict, content: str, max_tokens: int = 0):
        name = next(k for k, v in vars(P).items() if k.endswith("_PROMPT") and v == system)
        self.asked.append((name, schema, content))
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer, 100, 10

    @property
    def stages(self) -> list[str]:
        return [name for name, _, _ in self.asked]


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Книги").mkdir()
    for name, status in (("Дюна", "Read"), ("Солярис", "To read")):
        (tmp_path / f"Книги/{name}.md").write_text(
            f"---\nstatus: {status}\nauthor: x\n---\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(
        "## дом\n\n- [ ] счета #home\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_GROCERIES_NOTE}.md").write_text(
        "- [x] молоко\n- [x] хлеб\n- [x] яйца\n", encoding="utf-8")
    (tmp_path / "_bot.md").write_text("## Области\n- [[дом]] `#home`\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def index(vault):
    index = VaultIndex(vault)
    index.refresh()
    return index


def pipe(index, script: Script) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         StagedFiler(script, "haiku"), now=lambda: NOW)


def write(index, *actions: VaultAction):
    writer = VaultWriter(index, now=lambda: NOW)
    return [writer.run(a).undo for a in actions]


# ---- the question ------------------------------------------------------------------------------

async def test_the_model_is_shown_what_was_written_and_the_places_but_not_the_guide(index):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    script = Script(FIX_PROMPT={"changes": [], "add": [], "unclear": True})
    turn = await pipe(index, script).fix("молоко", "в продукты", undos)

    [(name, schema, content)] = script.asked
    assert name == "FIX_PROMPT"
    assert "a1: " + P.FIX_LINES["task"].format(words="молоко", note=texts.VAULT_TASKS_NOTE,
                                              folder="") in content
    assert texts.VAULT_GROCERIES_NOTE in content  # the place list
    assert "Области" not in content  # the guide is not sent
    assert schema["properties"]["changes"]["items"]["properties"]["key"]["enum"] == ["a1"]
    assert not turn.applied and texts.FIX_UNCLEAR in turn.reply_line()


# ---- applying it -------------------------------------------------------------------------------

async def test_a_task_moved_to_groceries(index):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    script = Script(FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "",
                                             "prop": "", "value": "", "to": "g"}],
                                "add": [], "unclear": False})
    turn = await pipe(index, script).fix("надо молоко", "не в задачи, а в продукты", undos)

    assert turn.applied and script.stages == ["FIX_PROMPT"]  # one call, nothing else
    assert "молоко" not in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] молоко" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert turn.reply_line() == texts.FIX_DONE.format(what=texts.FIX_MOVED.format(
        item="молоко", place=texts.VAULT_GROCERIES_NOTE))


async def test_a_due_date_is_set_only_when_the_correction_names_a_day(index):
    undos = write(index, VaultAction(action="task", text="позвонить маме", heading="дом"))
    change = {"key": "a1", "op": "set", "field": "due", "prop": "", "value": "2026-10-09",
              "to": ""}
    script = Script(FIX_PROMPT={"changes": [change], "add": [], "unclear": False})
    turn = await pipe(index, script).fix("позвонить маме", "на пятницу", undos)
    assert turn.applied
    assert "- [ ] позвонить маме 📅 2026-10-09" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")

    undos = write(index, VaultAction(action="task", text="полить цветы", heading="дом"))
    turn = await pipe(index, Script(FIX_PROMPT={"changes": [change], "add": [],
                                                "unclear": False})).fix(
        "полить цветы", "это важно", undos)
    assert not turn.applied  # no day was said: the date has no source
    assert "- [ ] полить цветы\n" in index.read(f"{texts.VAULT_TASKS_NOTE}.md") + "\n"


async def test_one_product_of_three_is_dropped_and_the_others_stay(index):
    undos = write(index, VaultAction(action="grocery", body=["молоко", "хлеб", "яйца"]))
    assert "- [ ] хлеб" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    script = Script(FIX_PROMPT={"changes": [{"key": "a2", "op": "drop", "field": "",
                                             "prop": "", "value": "", "to": ""}],
                                "add": [], "unclear": False})
    turn = await pipe(index, script).fix("купить молоко хлеб яйца", "хлеб не надо", undos)

    page = index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert "- [x] хлеб" in page  # back as it was before the first message
    assert "- [ ] молоко" in page and "- [ ] яйца" in page
    assert turn.reply_line() == texts.FIX_DONE.format(
        what=texts.FIX_DROPPED.format(item="хлеб"))


async def test_an_item_is_added_in_the_same_place(index):
    undos = write(index, VaultAction(action="grocery", body=["молоко"]))
    script = Script(FIX_PROMPT={"changes": [], "add": ["яйца", "икра"], "unclear": False})
    turn = await pipe(index, script).fix("купить молоко", "и ещё яйца", undos)

    page = index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert "- [ ] яйца" in page and "икра" not in page  # nobody said caviar
    assert turn.applied and turn.kept == undos  # the milk write was not touched


async def test_a_task_moved_into_a_folder_asks_that_folders_question_once(index):
    undos = write(index, VaultAction(action="task", text="Пикник на обочине", heading="дом"))
    books = next(k for k, f in enumerate(context(index, "", NOW).folders, start=1)
                 if f == "Книги")
    script = Script(
        FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "", "prop": "",
                                 "value": "", "to": f"f{books}"}], "add": [],
                    "unclear": False},
        FOLDER_PROMPT={"items": [{"title": "Пикник на обочине", "body": [], "tags": [],
                                  "props": [{"name": "status", "value": "To read"}]}],
                       "lookup": "", "web": "", "media": "text"})
    turn = await pipe(index, script).fix("надо Пикник на обочине", "это книга", undos)

    assert script.stages == ["FIX_PROMPT", "FOLDER_PROMPT"]
    assert turn.applied
    assert "Пикник" not in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    note = index.by_name("Пикник на обочине")
    assert note is not None and note.folder == "Книги"


async def test_a_line_edited_by_hand_since_leaves_every_file_untouched(index):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"),
                  VaultAction(action="task", text="позвонить", heading="дом"))
    path = index.root / f"{texts.VAULT_TASKS_NOTE}.md"
    edited = path.read_text(encoding="utf-8").replace("- [ ] молоко", "- [x] молоко")
    path.write_text(edited, encoding="utf-8", newline="\n")
    script = Script(FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "",
                                             "prop": "", "value": "", "to": "g"}],
                                "add": [], "unclear": False})
    turn = await pipe(index, script).fix("молоко, позвонить", "молоко в продукты", undos)

    assert not turn.applied and turn.left == [texts.VAULT_TASKS_NOTE]
    assert path.read_text(encoding="utf-8") == edited
    assert "- [ ] молоко" not in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")


async def test_nothing_is_dropped_unless_the_correction_says_so(index):
    """Found live: «ну это» made the model drop the task it was shown. A drop loses words, so
    it takes a word of the user's that asks for it."""
    drop = {"changes": [{"key": "a1", "op": "drop", "field": "", "prop": "", "value": "",
                         "to": ""}], "add": [], "unclear": False}
    undos = write(index, VaultAction(action="task", text="забрать посылку", heading="дом"))
    turn = await pipe(index, Script(FIX_PROMPT=drop)).fix("забрать посылку", "ну это", undos)
    assert not turn.applied
    assert "- [ ] забрать посылку" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")

    turn = await pipe(index, Script(FIX_PROMPT=drop)).fix("забрать посылку", "убери это",
                                                         undos)
    assert turn.applied
    assert "забрать посылку" not in index.read(f"{texts.VAULT_TASKS_NOTE}.md")


def test_the_words_that_may_drop_something():
    from app.vault.fix import may_drop

    for said in ("кефир не надо", "нет, без хлеба", "убери кефир", "удали это",
                 "лишнее", "отмени вторую"):
        assert may_drop(said), said
    for said in ("ну это", "на пятницу", "в продукты", "нечто другое"):
        assert not may_drop(said), said


async def test_a_rewritten_note_is_changed_again_rather_than_patched(index, vault):
    (vault / "Идеи.md").write_text("старый текст\n", encoding="utf-8")
    index.refresh()
    undos = write(index, VaultAction(action="rewrite", note="Идеи", text="перепиши",
                                     body=["новый текст"]))
    script = Script(CHANGE_NOTE_PROMPT={"kind": "rewrite", "props": [], "heading": "",
                                        "web": "", "media": "text"})
    plan, _, _ = await StagedFiler(script, "haiku").fix(
        "перепиши идеи", "верни первую строку", [VaultAction(**undos[0].action)],
        context(index, "идеи", NOW))

    assert script.stages == ["CHANGE_NOTE_PROMPT"]
    assert plan.take_back == [] and plan.write[0]["action"] == "rewrite"
    assert plan.write[0]["note"] == "Идеи"


async def test_claude_down_changes_nothing_and_says_why(index):
    import anthropic

    from app.vault.staged import claude_ask

    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))

    class Down:
        def __init__(self) -> None:
            self.messages = self

        async def create(self, **kwargs):
            raise anthropic.APIError("down", request=None, body=None)  # type: ignore[arg-type]

    filer = StagedFiler(claude_ask(Down(), "haiku"), "haiku")  # type: ignore[arg-type]
    turn = await VaultPipeline(index, VaultWriter(index), filer, now=lambda: NOW).fix(
        "молоко", "в продукты", undos)
    assert not turn.applied and turn.error
    assert "- [ ] молоко" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_a_grocery_moved_to_tasks_lands_in_the_task_file(index):
    undos = write(index, VaultAction(action="grocery", body=["батарейки"]))
    script = Script(FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "",
                                             "prop": "", "value": "", "to": "t"}],
                                "add": [], "unclear": False})
    turn = await pipe(index, script).fix("купить батарейки", "это в задачи", undos)

    assert turn.applied
    assert "- [ ] батарейки" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "батарейки" not in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert turn.reply_line() == texts.FIX_DONE.format(what=texts.FIX_MOVED.format(
        item="батарейки", place=texts.VAULT_TASKS_NOTE))


async def test_a_move_that_could_not_be_done_is_named_in_the_reply(index):
    from app.vault.filer import FilerError

    undos = write(index, VaultAction(action="task", text="Пикник на обочине", heading="дом"))
    books = next(k for k, f in enumerate(context(index, "", NOW).folders, start=1)
                 if f == "Книги")
    script = Script(
        FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "", "prop": "",
                                 "value": "", "to": f"f{books}"}], "add": [],
                    "unclear": False},
        FOLDER_PROMPT=FilerError("down"))
    turn = await pipe(index, script).fix("Пикник на обочине", "это книга", undos)

    assert not turn.applied
    assert turn.reply_line() == texts.FIX_FAILED.format(
        error=texts.FIX_NOT_MOVED.format(item="Пикник на обочине"))


async def test_a_failed_folder_question_leaves_that_item_where_it_was(index):
    from app.vault.filer import FilerError

    undos = write(index, VaultAction(action="task", text="Пикник на обочине", heading="дом"),
                  VaultAction(action="task", text="позвонить", heading="дом"))
    books = next(k for k, f in enumerate(context(index, "", NOW).folders, start=1)
                 if f == "Книги")
    script = Script(
        FIX_PROMPT={"changes": [
            {"key": "a1", "op": "move", "field": "", "prop": "", "value": "",
             "to": f"f{books}"},
            {"key": "a2", "op": "set", "field": "text", "prop": "", "value": "позвонить маме",
             "to": ""}], "add": [], "unclear": False},
        FOLDER_PROMPT=FilerError("down"))
    turn = await pipe(index, script).fix("Пикник на обочине, позвонить",
                                         "первое книга, второе позвонить маме", undos)

    page = index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert turn.applied and "- [ ] позвонить маме" in page
    assert "Пикник на обочине" in page  # the first task stayed where it was
    assert texts.FIX_NOT_MOVED.format(item="Пикник на обочине") in turn.reply_line()


async def test_a_set_then_a_move_of_the_same_item_keeps_the_new_words(index):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    script = Script(FIX_PROMPT={"changes": [
        {"key": "a1", "op": "set", "field": "text", "prop": "", "value": "кефир", "to": ""},
        {"key": "a1", "op": "move", "field": "", "prop": "", "value": "", "to": "g"}],
        "add": [], "unclear": False})
    turn = await pipe(index, script).fix("молоко", "не молоко, а кефир, в продукты", undos)

    assert turn.applied
    assert "- [ ] кефир" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert "молоко" not in index.read(f"{texts.VAULT_TASKS_NOTE}.md")


# ---- a fix never raises, and never loses the user's words --------------------------------------

MOVE_TO_GROCERIES = {"changes": [{"key": "a1", "op": "move", "field": "", "prop": "",
                                  "value": "", "to": "g"}], "add": [], "unclear": False}


async def test_a_write_that_raises_inside_a_fix_is_an_error_not_a_crash(index, monkeypatch):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    p = pipe(index, Script(FIX_PROMPT=MOVE_TO_GROCERIES))

    def locked(old, new):
        raise PermissionError("locked")

    monkeypatch.setattr(p._writer, "replace_writes", locked)
    turn = await p.fix("молоко", "в продукты", undos)

    assert not turn.applied and turn.error == "PermissionError"
    assert turn.reply_line() == texts.FIX_FAILED.format(error="PermissionError")


async def test_a_take_back_that_could_not_be_written_is_named(index, monkeypatch):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    p = pipe(index, Script(FIX_PROMPT=MOVE_TO_GROCERIES))

    def locked(undo):
        raise PermissionError("locked")

    monkeypatch.setattr(p._writer, "undo", locked)
    turn = await p.fix("молоко", "в продукты", undos)

    assert turn.applied  # the new line is written: a duplicate beats a lost line
    assert "- [ ] молоко" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert turn.error == texts.FIX_NOT_TAKEN_BACK.format(notes=f"«{texts.VAULT_TASKS_NOTE}»")
    assert turn.error in turn.reply_line()


async def test_a_new_write_that_fails_inside_a_fix_is_named_by_its_words(index, monkeypatch):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    script = Script(FIX_PROMPT={**MOVE_TO_GROCERIES, "add": ["хлеб"]})
    p = pipe(index, script)
    real = p._writer.run

    def run(action):
        if action.text == "хлеб":
            raise PermissionError("locked")
        return real(action)

    monkeypatch.setattr(p._writer, "run", run)
    turn = await p.fix("молоко", "в продукты, и ещё хлеб", undos)

    assert turn.applied
    assert turn.error == texts.FIX_NOT_WRITTEN.format(items="хлеб")
    assert texts.FIX_NOT_WRITTEN.format(items="хлеб") in turn.reply_line()
