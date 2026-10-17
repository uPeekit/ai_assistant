"""Answering a question from the notes (app/vault/answer.py): what the model is shown, the one
extra round when the answer is in notes the search did not reach, and the reply."""

from __future__ import annotations

from datetime import datetime

import pytest

from app import texts
from app.llm import staged_prompts as P
from app.vault import answer as answer_mod
from app.vault.answer import VaultAnswerer
from app.vault.filer import Filer, FilerError
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.search import search
from app.vault.writer import VaultWriter
from tests.test_vault_filer import FakeAnthropic

BOOKS = "Книги"

NOW = datetime(2026, 10, 15, 12, 0)


@pytest.fixture
def index(tmp_path):
    (tmp_path / BOOKS).mkdir()
    (tmp_path / texts.VAULT_NOTES_DIR).mkdir()
    (tmp_path / f"{BOOKS}/Доктор Живаго.md").write_text(
        "---\nstatus: Read\nauthor: '[[Авторы/Борис Пастернак]]'\n---\n\n"
        "Понравилось больше всего: стихи в конце.\n", encoding="utf-8")
    (tmp_path / f"{BOOKS}/Чапаев и Пустота.md").write_text(
        "---\nstatus: Reading\n---\n\nо книге\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_NOTES_DIR}/Ремонт.md").write_text(
        "Решили: плитка серая, ванна остаётся.\n", encoding="utf-8")
    (tmp_path / "_bot.md").write_text("инструкция боту\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


class Ask:
    """Answers each call in turn, and records what it was shown."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.shown: list[str] = []

    async def __call__(self, system: str, schema: dict, content: str, max_tokens: int = 0):
        assert system == P.ANSWER_PROMPT
        self.shown.append(content)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, 1000, 50


def key_of(content: str, path: str) -> str:
    line = next(ln for ln in content.splitlines() if f": {path}" in ln)
    return line.split(":", 1)[0]


async def test_the_found_notes_are_read_and_the_rest_is_a_catalog(index):
    ask = Ask({"answer": "Плитка серая, ванна остаётся.", "sources": ["n1"], "more": []})
    hits = search(index, "ремонт")
    found = await VaultAnswerer(ask, "haiku").answer(
        "что я решил про ремонт?", hits, index, "2026-10-15, чт")

    [content] = ask.shown
    assert key_of(content, "Заметки/Ремонт") == "n1"  # what the search found comes first
    assert "Решили: плитка серая" in content  # ... and is read
    assert "о книге" not in content  # the rest is only named
    # A catalog line shows properties, a link by the name it points at.
    assert "Книги/Доктор Живаго · status: Read · author: Борис Пастернак" in content
    assert "_bot" not in content  # the guide is about the bot, not about the user's life
    assert found.text == "Плитка серая, ванна остаётся."
    assert [n.name for n in found.sources] == ["Ремонт"]
    assert found.prompt_tokens == 1000


def test_the_summary_counts_notes_and_property_values_per_folder(index):
    for name in ("Идиот", "Бесы", "Игрок"):
        (index.root / f"{BOOKS}/{name}.md").write_text(
            f"---\nstatus: Read\nauthor: '[[Авторы/Фёдор Достоевский]]'\n"
            f"url: https://example.com/{name}\n---\n", encoding="utf-8")
    index.refresh()
    summary = answer_mod.folders_summary(index.notes)
    line = next(ln for ln in summary.splitlines() if ln.startswith(BOOKS))
    assert P.SUMMARY_NOTES.format(folder=BOOKS, n=5) in line
    assert "status — Read 4, Reading 1" in line
    assert "author — Фёдор Достоевский 3, Борис Пастернак 1" in line
    assert "url" not in line  # an address is not a category
    # Two properties counted together: how many of his books are read.
    assert "status + author — Read + Фёдор Достоевский 3, Read + Борис Пастернак 1" in summary
    # A folder of one or two notes has nothing worth counting.
    assert texts.VAULT_NOTES_DIR not in summary


async def test_a_note_the_words_did_not_reach_is_read_when_the_model_asks_for_it(index):
    first = {"answer": "", "sources": [], "more": ["n2", "n99"]}
    ask = Ask(first, {"answer": "Доктор Живаго — стихи в конце.", "sources": ["n2"],
                      "more": ["n3"]})  # a second "more" is not followed
    found = await VaultAnswerer(ask, "haiku").answer(
        "какая книга мне понравилась больше всего?", [], index, "2026-10-15, чт")

    assert len(ask.shown) == 2
    assert key_of(ask.shown[0], "Книги/Доктор Живаго") == "n2"
    assert "стихи в конце" not in ask.shown[0] and "стихи в конце" in ask.shown[1]
    assert [n.name for n in found.sources] == ["Доктор Живаго"]
    assert found.prompt_tokens == 2000


async def test_a_long_note_is_cut_and_the_budget_stops_at_a_whole_note(index, monkeypatch):
    (index.root / f"{texts.VAULT_NOTES_DIR}/Ремонт.md").write_text("а" * 50, encoding="utf-8")
    monkeypatch.setattr(answer_mod, "MAX_NOTE", 20)
    index.refresh()
    ask = Ask({"answer": "ок", "sources": [], "more": []})
    await VaultAnswerer(ask, "haiku").answer("ремонт", search(index, "ремонт"), index, "")
    assert "а" * 20 + "\n" + P.NOTE_CUT in ask.shown[0]


async def test_an_empty_answer_is_a_failure_not_a_blank_reply(index):
    ask = Ask({"answer": " ", "sources": [], "more": []})
    with pytest.raises(FilerError):
        await VaultAnswerer(ask, "haiku").answer("x", [], index, "")


# ---- in the pipeline -------------------------------------------------------------------------

def pipe(index, ask: Ask) -> VaultPipeline:
    claude = FakeAnthropic({"actions": [{"action": "search", "text": "ремонт"}]})
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         Filer("", "m", client=claude), None, now=lambda: NOW,
                         answerer=VaultAnswerer(ask, "haiku"))


async def test_the_reply_is_the_answer_with_links_to_where_it_came_from(index):
    ask = Ask({"answer": "Плитка серая.", "sources": ["n1"], "more": []})
    turn = await pipe(index, ask).handle("что я решил про ремонт?")

    line = turn.reply_line()
    assert line.startswith(texts.VAULT_ANSWER.format(answer="Плитка серая."))
    assert f"[Ремонт](obsidian://open?vault={turn.vault}&file=" in line
    assert texts.VAULT_SEARCH_HEADER not in line
    assert "2026-10-15, чт" in ask.shown[0]  # dates are counted from today
    assert turn.writes == [] and turn.prompt_tokens >= 1000


async def test_when_the_answer_fails_the_reply_is_what_the_search_found(index):
    turn = await pipe(index, Ask(FilerError("down"))).handle("что я решил про ремонт?")
    line = turn.reply_line()
    assert texts.VAULT_SEARCH_HEADER in line and "Ремонт" in line


async def test_an_owners_instruction_may_ask_for_a_passage_as_written(index):
    """«What did we conclude about X» came back retold in the model's words; the owner wanted
    their own notes. How to answer from a document is the owner's instruction, so the prompt
    lets it override brevity, and the cut leaves room for a passage of a few thousand
    characters. Every document a part of the answer came from is credited."""
    from app.google.sources import Found

    notes = "\n".join(f"- мысль номер {i} о книге, записанная на встрече" for i in range(60))
    google = [Found(source="Выводы", title="Книга", text=notes,
                    instruction="приводи раздел дословно", score=3),
              Found(source="Оценки", title="лист", text="Книга — avg: 3.3", score=2)]
    ask = Ask({"answer": notes + "\nСредняя оценка: 3.3", "sources": ["g1", "g2"], "more": []})
    found = await VaultAnswerer(ask, "haiku").answer(
        "какие выводы по книге?", [], index, "2026-10-15, чт", google=google)

    assert "дословно" in P.ANSWER_PROMPT and "если указание владельца" in P.ANSWER_PROMPT
    assert len(notes) > 2_500 and found.text.startswith(notes)  # whole, not cut at 1 500
    assert found.google == ["Выводы — Книга", "Оценки — лист"]
