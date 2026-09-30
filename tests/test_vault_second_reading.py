"""A second, stronger reading of a message the light filer visibly did not understand.

The case: «Добавь Доктор Живаго и регистрация Глуховского список следующих встреч книжного
клуба» — dictated, with the «в» swallowed. Measured on a copy of the real vault, Haiku got it
wrong 6 times out of 6 even with a rule in the prompt written for exactly this sentence (a book
in «Книги», and an empty note called «Список следующих встреч»); Sonnet got it right 3 out of 3.
Five of the six wrong answers contained a note with nothing in it, which is a thing code can
see."""

from __future__ import annotations

import anthropic
import pytest

from app.vault.filer import Filer, check, doubtful
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.writer import VaultWriter
from tests.test_vault_filer import FakeAnthropic

MESSAGE = ("Добавь Доктор Живаго и регистрация Глуховского список следующих встреч книжного "
           "клуба я Потом добавлю даты и описание")
# What Haiku answered, and what Sonnet answered, on the live replay.
LIGHT = {"actions": [
    {"action": "note", "folder": "Книги", "title": "Доктор Живаго",
     "props": [{"name": "status", "value": "To read"}]},
    {"action": "note", "folder": "Кнуб", "title": "Регистрация Глуховского"},
    {"action": "note", "folder": "Кнуб", "title": "Список следующих встреч"}]}
STRONG = {"actions": [
    {"action": "note", "folder": "Кнуб", "title": "Доктор Живаго",
     "props": [{"name": "book", "value": "Доктор Живаго"}]},
    {"action": "note", "folder": "Кнуб", "title": "Регистрация",
     "props": [{"name": "book", "value": "Регистрация"},
               {"name": "author", "value": "Глуховский"}]}]}


@pytest.fixture
def vault(tmp_path):
    for folder in ("Кнуб", "Книги", "Заметки"):
        (tmp_path / folder).mkdir()
    (tmp_path / "Кнуб/2026-09-21 Чёрные кувшинки.md").write_text(
        "---\nbook: Чёрные кувшинки\n---\n", encoding="utf-8")
    (tmp_path / "Книги/Чапаев.md").write_text("---\nstatus: Read\n---\n", encoding="utf-8")
    (tmp_path / "Заметки/идеи.md").write_text("текст\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return tmp_path, index


def _pipeline(index, light, strong):
    light_client, strong_client = FakeAnthropic(light), FakeAnthropic(strong)
    pipeline = VaultPipeline(
        index, VaultWriter(index), Filer("", "haiku", client=light_client), None,
        strong=Filer("", "sonnet", client=strong_client))
    return pipeline, light_client, strong_client


def _notes(root) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md"))


async def test_an_empty_note_sends_the_message_to_the_stronger_model(vault):
    root, index = vault
    before = _notes(root)
    pipeline, _, strong = _pipeline(index, LIGHT, STRONG)

    turn = await pipeline.handle(MESSAGE)

    assert len(strong.seen) == 1
    assert [n for n in _notes(root) if n not in before] == [
        "Кнуб/Доктор Живаго.md", "Кнуб/Регистрация.md"]
    assert turn.model == "sonnet"  # the audit names the model whose answer was written


async def test_an_answer_with_nothing_wrong_with_it_costs_no_second_call(vault):
    _, index = vault
    pipeline, _, strong = _pipeline(
        index, {"actions": [{"action": "task", "text": "купить лампочки"}]}, STRONG)

    turn = await pipeline.handle("купить лампочки")

    assert strong.seen == [] and turn.model == "haiku"


async def test_a_failed_second_reading_keeps_the_first(vault):
    root, index = vault
    down = anthropic.APIError("down", request=None, body=None)  # type: ignore[arg-type]
    pipeline, _, _ = _pipeline(index, LIGHT, down)

    turn = await pipeline.handle(MESSAGE)

    assert len(turn.writes) == 3 and turn.model == "haiku"


async def test_text_already_found_fills_the_note_so_nothing_is_empty(vault):
    """A note with no body is what a search's findings are about to be put into."""
    _, index = vault
    pipeline, _, strong = _pipeline(
        index, {"actions": [{"action": "note", "folder": "Заметки", "title": "Тории"}]}, STRONG)

    await pipeline.handle("найди про ворота тории", content="## Тории\nтекст")

    assert strong.seen == []


def test_what_counts_as_doubtful(vault):
    _, index = vault

    def reason(*raw: dict) -> str:
        return doubtful(check(list(raw), index, "сообщение"))

    assert reason({"action": "note", "folder": "Кнуб", "title": "Список встреч"}) == "empty note"
    assert reason({"action": "inbox", "text": "что-то"}) == "inbox"
    assert reason({"action": "task", "text": "купить лампочки"}) == ""
    assert reason({"action": "note", "folder": "Заметки", "title": "идея",
                   "body": ["текст идеи"]}) == ""
    assert reason({"action": "note", "folder": "Книги", "title": "Дюна",
                   "props": [{"name": "status", "value": "To read"}]}) == ""
