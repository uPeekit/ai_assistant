"""A group chat's turn through the whole vault pipeline: every prompt it causes is recorded, and
none may hold the owner's notes; what it writes lands only in its scope."""

from __future__ import annotations

from datetime import datetime

import pytest

from app import texts
from app.chats import ChatScope
from app.llm import staged_prompts as P
from app.vault.answer import VaultAnswerer
from app.vault.filer import FilerError
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultAction, VaultWriter

NOW = datetime(2026, 10, 15, 12, 0)
CLUB = ChatScope(chat_id=-1, title="club", rights=frozenset({"ask", "add", "tasks"}),
                 tag="клуб", folder="Клуб", instruction="Встречи книжного клуба.")
# Words that exist only in the owner's part of the vault.
PRIVATE = ("лампочки", "Дюна", "мои выводы", "личная инструкция", "выводы запостить",
           "Доктор Живаго", "дневниковое")


@pytest.fixture
def index(tmp_path):
    (tmp_path / "Клуб").mkdir()
    (tmp_path / "Книги").mkdir()
    (tmp_path / "Дневник").mkdir()
    (tmp_path / "Клуб/Встреча клуба.md").write_text(
        "---\ntags: [клуб]\ndate: 2026-11-01\n---\n\nобщие планы встречи\n", encoding="utf-8")
    (tmp_path / "Клуб/Доктор Живаго.md").write_text(
        "---\nbook: Доктор Живаго\n---\n\nмои выводы о встрече\n", encoding="utf-8")
    (tmp_path / "Книги/Дюна.md").write_text("---\nstatus: Read\n---\nвстреча\n",
                                            encoding="utf-8")
    (tmp_path / "Дневник/2026-10-14.md").write_text("- дневниковое про встречу\n",
                                                   encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(
        "## дом\n\n- [ ] купить лампочки #home\n\n## club_my\n\n"
        "- [ ] выводы запостить #club_my\n\n## клуб\n\n- [ ] принести печенье #клуб\n",
        encoding="utf-8")
    (tmp_path / "_bot.md").write_text("личная инструкция\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


class Script:
    """Answers each stage by its prompt; records every prompt it is shown."""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.shown: list[str] = []

    async def __call__(self, system: str, schema: dict, content: str, max_tokens: int = 0):
        name = next(k for k, v in vars(P).items() if k.endswith("_PROMPT") and v == system)
        self.shown.append(content)
        if name not in self.answers:
            raise FilerError(f"no answer for {name}")
        answer = self.answers[name]
        return (answer.pop(0) if isinstance(answer, list) else answer), 100, 10


def pipe(index, script: Script, answerer=None) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         StagedFiler(script, "haiku"), now=lambda: NOW,
                         answerer=answerer).scoped(CLUB)


def assert_private_never_shown(script: Script) -> None:
    shown = "\n".join(script.shown)
    for word in PRIVATE:
        assert word not in shown, word


async def test_a_task_from_the_group_gets_the_club_tag_and_nothing_private_is_shown(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                    TASKS_PROMPT={"items": [{"text": "купить чай", "due": "", "time": "",
                                             "repeat": "", "heading": "дом", "tag": "home",
                                             "countdown": False}]})
    turn = await pipe(index, script).handle("встреча: купить чай")

    assert len(turn.writes) == 1
    tasks = index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] купить чай #клуб" in tasks and "#home\n- [ ] купить чай" not in tasks
    assert_private_never_shown(script)


async def test_ticking_is_refused_and_nothing_is_written(index):
    script = Script(INTENT_PROMPT={"intent": "done"}, DONE_PROMPT={"targets": ["o1"]})
    before = index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    turn = await pipe(index, script).handle("печенье принесли")
    assert turn.writes == []
    assert texts.SCOPE_KINDS["update"] in turn.reply_line()
    assert index.read(f"{texts.VAULT_TASKS_NOTE}.md") == before
    assert_private_never_shown(script)


async def test_a_message_nobody_understood_is_not_kept_in_the_owners_inbox(index):
    script = Script(INTENT_PROMPT={"intent": "unclear"})
    turn = await pipe(index, script).handle("ну такое")
    assert turn.writes == [] and turn.reply_line() == texts.SCOPE_NOTHING
    assert not (index.root / f"{texts.VAULT_INBOX_NOTE}.md").exists()


async def test_with_claude_down_the_groups_words_are_not_kept_either(index):
    turn = await pipe(index, Script()).handle("встреча в субботу")  # every stage fails
    assert turn.writes == [] and turn.error
    assert not (index.root / f"{texts.VAULT_INBOX_NOTE}.md").exists()


async def test_a_new_meeting_never_lands_in_the_owners_note_of_that_name(index):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                    FOLDER_PROMPT={"items": [{"title": "Доктор Живаго", "props": [],
                                              "body": ["обсудим финал"], "tags": []}]})
    turn = await pipe(index, script).handle("добавь встречу Доктор Живаго, обсудим финал")
    [write] = turn.writes
    assert write.path == "Клуб/Доктор Живаго (клуб).md"
    assert "мои выводы" in index.read("Клуб/Доктор Живаго.md")
    assert "обсудим финал" not in index.read("Клуб/Доктор Живаго.md")
    assert "клуб" in index.read(write.path)  # tagged, so the group sees it next time


class AnswerAsk:
    def __init__(self) -> None:
        self.shown: list[str] = []

    async def __call__(self, system, schema, content, max_tokens=0):
        self.shown.append(content)
        return {"answer": "Встреча 1 ноября.", "sources": ["n1"], "more": []}, 100, 10


async def test_a_question_reads_only_the_groups_notes_and_shows_no_links(index):
    script = Script(INTENT_PROMPT={"intent": "ask"},
                    ASK_PROMPT={"kind": "search", "text": "встреча", "folder": "", "tag": "",
                                "props": [], "due_from": "", "due_to": ""})
    ask = AnswerAsk()
    turn = await pipe(index, script, VaultAnswerer(ask, "haiku")).handle("когда встреча?")
    line = turn.reply_line()
    assert "Встреча 1 ноября." in line and "obsidian://" not in line
    shown = "\n".join(ask.shown)
    assert "Встреча клуба" in shown
    for word in PRIVATE:
        assert word not in shown, word
    assert_private_never_shown(script)


async def test_a_fix_from_the_group_is_held_to_the_same_scope(index):
    p = pipe(index, Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                           TASKS_PROMPT={"items": [{"text": "купить чай", "due": "",
                                                    "time": "", "repeat": "", "heading": "",
                                                    "tag": "", "countdown": False}]}))
    turn = await p.handle("купить чай")
    # The correction asks to move the task to the diary: not the group's to ask.
    fixer = pipe(index, Script(FIX_PROMPT={"changes": [{"key": "a1", "op": "move",
                                                        "field": "", "prop": "", "value": "",
                                                        "to": "d"}],
                                           "add": [], "unclear": False}))
    result = await fixer.fix("купить чай", "это в дневник", turn.undos)
    assert not result.applied
    assert not (index.root / "Дневник/2026-10-15.md").exists()
    assert "купить чай #клуб" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")


def test_a_full_access_scope_is_the_pipeline_itself(index):
    p = VaultPipeline(index, VaultWriter(index), StagedFiler(Script(), "haiku"))
    assert p.scoped(ChatScope(chat_id=-1, full=True)) is p


async def test_the_guarded_writer_refuses_what_the_gate_should_have(index):
    p = pipe(index, Script())
    await p._refreshed()
    with pytest.raises(ValueError):
        p._writer.run(VaultAction(action="append", note="Дюна", body=["x"]))


async def test_a_name_the_gate_found_in_scope_is_the_note_the_writer_writes(index, tmp_path):
    """The owner's «Книги» at the root and the club's «Клуб/Книги»: by name across the whole
    vault the writer used to pick the owner's (shallower) one, and the guard refused it."""
    (tmp_path / "Книги.md").write_text("личный список\n", encoding="utf-8")
    (tmp_path / "Клуб/Книги.md").write_text("---\ntags: [клуб]\n---\n\n- Дюна\n",
                                            encoding="utf-8")
    index.refresh()
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    APPEND_PROMPT={"body": ["Солярис"], "heading": "", "web": "",
                                   "media": "text", "lookup": ""})
    turn = await pipe(index, script).handle("добавь в Книги Солярис")
    assert [w.path for w in turn.writes] == ["Клуб/Книги.md"]
    assert "Солярис" in index.read("Клуб/Книги.md")
    assert index.read("Книги.md") == "личный список\n"
