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


async def test_the_reply_is_the_answer(index):
    ask = Ask({"answer": "Плитка серая.", "sources": ["n1"], "more": []})
    turn = await pipe(index, ask).handle("что я решил про ремонт?")

    line = turn.reply_line()
    assert line == texts.VAULT_ANSWER.format(answer="Плитка серая.")
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


async def test_the_answerer_is_told_who_asks_and_may_ask_back_who_they_are(index):
    """«What did I rate it» was answered with the rating of the person asked about one message
    earlier. The answerer is shown the sender's Telegram name, and when the records do not say
    for sure which person that is it says so (kind who, with the names that may be theirs)
    instead of guessing; whatever it wrote as an answer then is dropped, and so is a name the
    records it read do not hold."""
    from app.google.sources import Found

    google = [Found(source="Оценки", title="лист", text="Книга — саша п: 3; саша в: 4",
                    score=3)]
    ask = Ask({"kind": "who", "options": ["саша п", "Александр", "саша в", "саша п"],
               "answer": "Вы поставили 4.2", "sources": [], "more": []})
    found = await VaultAnswerer(ask, "haiku").answer(
        "какую оценку я поставил?", [], index, "2026-10-15, чт", google=google,
        asker="Саша (@sasha)")

    assert P.H_ASKER + ":" + chr(10) + "Саша (@sasha)" in ask.shown[0]
    assert found.kind == "who" and found.options == ["саша п", "саша в"]
    assert found.text == ""  # never the model's guess
    assert "не подставляй" in P.ANSWER_PROMPT


def _who_pipe(index, *answers):
    """A pipeline whose answerer says `answers` in turn, over a note naming two people."""
    from app.vault.askers import Askers

    (index.root / f"{texts.VAULT_NOTES_DIR}/Ремонт.md").write_text(
        "Оценки за ремонт: саша п 3, саша в 3.5\n", encoding="utf-8")
    ask = Ask(*answers)
    search_ = {"actions": [{"action": "search", "text": "ремонт"}]}
    askers = Askers()
    p = VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                      Filer("", "m", client=FakeAnthropic(*[search_] * len(answers))), None,
                      now=lambda: NOW, answerer=VaultAnswerer(ask, "haiku"), askers=askers)
    return p, ask, askers


WHO = {"kind": "who", "options": ["саша п", "саша в"], "answer": "", "sources": [], "more": []}


async def test_the_bot_asks_back_and_the_next_short_message_answers_the_first_question(index):
    """The question back is the code's own; the same person's next short message is their name,
    kept for next time, and the question they asked first is the one answered."""
    from app.chats import ChatRef

    p, ask, askers = _who_pipe(index, WHO, {"kind": "notes", "options": [], "answer": "3.5",
                                            "sources": [], "more": []})
    chat = ChatRef(1, 7, name="Саша")
    first = await p.handle("какую оценку я поставил ремонту?", chat=chat)

    assert first.answer == texts.ANSWER_WHO_OPTIONS.format(lead="",
                                                           options="«саша п», «саша в»")
    assert first.said == ""  # nothing a «save this» could pick up

    second = await p.handle("саша в", chat=chat)

    assert askers.known(7) == ("саша в",)
    assert P.ASKER_KNOWN.format(name="Саша", known="«саша в»") in ask.shown[1]
    assert "какую оценку я поставил ремонту?" + chr(10) in ask.shown[1]  # the first question
    assert second.answer.startswith(texts.VAULT_ANSWER.format(answer="3.5"))


async def test_a_known_name_not_in_these_records_is_asked_once_more_then_not_found(index):
    """People go by other names elsewhere (a quiz under a joke name): a name they gave before
    that these records lack is said, and asked about once; told and still not found, the bot
    says so instead of asking in circles."""
    from app.chats import ChatRef

    p, ask, askers = _who_pipe(index, WHO, WHO)
    askers._remember(7, "женя")
    chat = ChatRef(1, 7, name="Саша")
    first = await p.handle("сколько баллов я набрал?", chat=chat)

    assert first.answer == texts.ANSWER_WHO_OPTIONS.format(
        lead=texts.WHO_NOT_HERE.format(names="«женя»"), options="«саша п», «саша в»")
    assert "«женя»" in ask.shown[0]

    second = await p.handle("капитан", chat=chat)

    assert second.answer == texts.ANSWER_WHO_NOT_FOUND.format(name="капитан")
    assert askers.known(7) == ("женя", "капитан")


async def test_who_the_asker_is_goes_to_the_stronger_model_before_asking_them(index):
    first = Ask(WHO)
    second = Ask({"kind": "notes", "options": [], "answer": "3.5", "sources": [], "more": []})
    answerer = VaultAnswerer(first, "haiku", second=VaultAnswerer(second, "sonnet"))
    found = await answerer.answer("какую оценку я поставил?", [], index, "2026-10-15, чт",
                                  asker="Саша")

    assert found.kind == "notes" and found.text == "3.5"
    assert len(first.shown) == len(second.shown) == 1
    assert found.prompt_tokens == 2000  # both calls are counted


async def test_a_failed_second_answer_still_asks_the_user(index):
    answerer = VaultAnswerer(Ask(WHO), "haiku",
                             second=VaultAnswerer(Ask(FilerError("down")), "sonnet"))
    found = await answerer.answer("какую оценку я поставил?", [], index, "2026-10-15, чт")

    assert found.kind == "who"


async def test_the_owners_instructions_stand_apart_from_the_documents_text(index):
    """Told to quote a part as written, the model quoted the instruction printed above the part
    too: the instructions now come once per document, in a section of their own."""
    from app.google.sources import Found

    rule = "приводи раздел дословно"
    google = [Found(source="Выводы", title="Книга 1", text="мысль один", instruction=rule,
                    score=3),
              Found(source="Выводы", title="Книга 2", text="мысль два", instruction=rule,
                    score=2)]
    ask = Ask({"answer": "мысль один", "sources": ["g1"], "more": []})
    await VaultAnswerer(ask, "haiku").answer("выводы по книге 1?", [], index, "2026-10-15, чт",
                                             google=google)

    shown = ask.shown[0]
    assert shown.count(rule) == 1
    assert shown.index(P.H_GOOGLE + ":") < shown.index(P.H_GOOGLE_RULES)
    assert "### g1: Выводы — Книга 1\nмысль один" in shown


async def test_an_instruction_copied_into_the_answer_is_taken_out(index):
    from app.google.sources import Found

    rule = "Только наши заметки по книгам. Приводи раздел дословно."
    google = [Found(source="Выводы", title="Книга", text="мысль", instruction=rule, score=3)]
    ask = Ask({"answer": f"{P.GOOGLE_INSTRUCTION}: {rule}\nмысль", "sources": ["g1"],
               "more": []})
    found = await VaultAnswerer(ask, "haiku").answer("выводы?", [], index, "2026-10-15, чт",
                                                     google=google)
    assert found.text == "мысль"


async def test_an_answer_names_no_notes_it_came_from(index):
    """Links go only to what was made: the notes an answer was read from were a line of
    links that told the owner little, in their own chat as in a group."""
    from app.chats import ChatRef

    answer = {"answer": "Плитка серая.", "sources": ["n1"], "more": []}
    for chat in (ChatRef(7, 7, owner=True), ChatRef(-100, 7, owner=True)):
        turn = await pipe(index, Ask(answer)).handle("что я решил про ремонт?", chat=chat)
        assert turn.reply_line() == texts.VAULT_ANSWER.format(answer="Плитка серая.")


async def test_the_answerer_sees_the_inputs(index):
    ask = Ask({"answer": "ок", "sources": [], "more": []})
    await VaultAnswerer(ask, "haiku").answer("что посоветуешь?", [], index, "2026-10-15, чт",
                                             inputs="#2: Имя розы")
    assert P.H_INPUTS in ask.shown[0] and "#2: Имя розы" in ask.shown[0]


async def test_nothing_found_about_the_owners_own_records_goes_to_the_stronger_model(index):
    """«когда следующие встречи клуба?» — the folder's dated notes were all in the catalog,
    and Haiku said there was nothing (or went to the web). A question the reader already tied
    to a folder or tag of the vault gets the stronger model's reading before such an answer."""
    for kind in ("none", "web"):
        first = Ask({"answer": "в заметках ничего нет", "sources": [], "more": [], "kind": kind,
                     "web": "встречи"})
        second = Ask({"answer": "9 ноября", "sources": [], "more": [], "kind": "notes"})
        answerer = VaultAnswerer(first, "haiku", second=VaultAnswerer(second, "sonnet"))
        found = await answerer.answer("когда следующие встречи?", [], index, "2026-10-19, пн",
                                      about_records=True)
        assert found.kind == "notes" and found.text == "9 ноября"


async def test_a_general_question_is_not_sent_twice(index):
    first = Ask({"answer": "", "sources": [], "more": [], "kind": "web", "web": "погода"})
    second = Ask()
    answerer = VaultAnswerer(first, "haiku", second=VaultAnswerer(second, "sonnet"))
    found = await answerer.answer("какая погода завтра?", [], index, "2026-10-19, пн")
    assert found.kind == "web" and second.shown == []


def test_the_answer_prompt_says_how_to_find_the_next_dated_note():
    assert P.NEXT_DATES_RULE in P.ANSWER_PROMPT


def test_the_answer_prompt_asks_for_each_listed_note_with_its_details():
    """«когда следующие встречи?» from Haiku was bare titles; the same notes carried the book's
    author in their properties, and Sonnet's answer named them."""
    assert P.LIST_DETAILS_RULE in P.ANSWER_PROMPT


def test_a_dated_catalog_line_says_how_far_its_date_is_from_today():
    """Haiku listed a meeting held yesterday as the next one, and once the past ones: the line
    says it in days, counted here, not left to the model."""
    import datetime

    from app.vault.answer import catalog_line
    from app.vault.index import Note

    today = datetime.date(2026, 10, 20)
    by_name = Note(path="Кнуб/2026-11-09 Встреча.md", name="2026-11-09 Встреча", folder="Кнуб")
    by_prop = Note(path="Кнуб/Встреча.md", name="Встреча", folder="Кнуб",
                   props={"date": "2026-10-19"})
    same_day = Note(path="a.md", name="a", folder="", props={"when": datetime.date(2026, 10, 20)})
    undated = Note(path="b.md", name="b", folder="", props={"author": "X"})

    assert catalog_line(by_name, today).endswith(P.WHEN_AHEAD.format(days=20))
    assert catalog_line(by_prop, today).endswith(P.WHEN_AGO.format(days=1))
    assert catalog_line(same_day, today).endswith(P.WHEN_TODAY)
    assert catalog_line(undated, today) == "b · author: X"
    assert catalog_line(undated) == "b · author: X"


async def test_a_read_dated_note_says_how_far_its_date_is_too(tmp_path):
    """The past meeting's own text, read in full, showed its bare date and Haiku named it next."""
    from app.vault.index import VaultIndex
    from app.vault.search import Hit

    (tmp_path / "Встречи").mkdir()
    (tmp_path / "Встречи/Танго.md").write_text("---\ndate: 2026-10-19\n---\n", encoding="utf-8")
    vault = VaultIndex(tmp_path)
    vault.refresh()
    ask = Ask({"answer": "ок", "sources": [], "more": []})
    await VaultAnswerer(ask, "haiku").answer("когда следующая?", [Hit("Танго", "Встречи/Танго.md")],
                                             vault, "2026-10-20, вт")
    assert f"### n1 · {P.WHEN_AGO.format(days=1)}\n" in ask.shown[0]


def test_the_answer_prompt_keeps_the_lines_of_what_it_quotes():
    """A club's conclusions, one per line in the document, came back joined by «; » into one
    paragraph nobody could read."""
    assert P.KEEP_LINES_RULE in P.ANSWER_PROMPT


def test_the_answer_prompt_takes_a_rows_count_from_its_tag():
    """«сколько человек оценили» was counted by the model down the row, ten of twelve."""
    assert P.ROW_COUNT_RULE in P.ANSWER_PROMPT


def test_kept_copies_are_read_again_for_the_row_counts():
    from app.google.sources import COPY_FORMAT

    assert COPY_FORMAT >= 5


def test_the_answer_prompt_keeps_its_keys_to_itself():
    """A group was told the quiz results were «в отдельных формах (g132–g297)»."""
    assert P.NO_KEYS_RULE in P.ANSWER_PROMPT



async def test_the_google_search_looks_for_the_askers_names_too(index):
    """«какой книге я поставила самую высокую оценку?» from Ольга, known as «оля»: her column
    of the sheet is found by that name, which the question does not carry."""
    from app.chats import ChatRef

    class FakeGoogle:
        def __init__(self):
            self.queries = []

        async def find(self, query, chat_id, *, owner):
            self.queries.append(query)
            return []

        def sign_in_notice(self):
            return ""

    answer = {"answer": "ок", "sources": [], "more": []}
    p, ask, askers = _who_pipe(index, answer)
    askers._remember(7, "оля")
    p._google = google = FakeGoogle()
    await p.handle("какой книге я поставила самую высокую оценку?",
                   chat=ChatRef(-100, 7, name="Ольга", owner=False))
    assert google.queries and "оля" in google.queries[-1].split()



def test_the_answerer_asks_without_thinking(monkeypatch):
    from app.vault import answer as answer_module

    made = []
    monkeypatch.setattr(answer_module, "claude_ask",
                        lambda sdk, model, health=None, *, think=True: made.append((model, think))
                        or (lambda *a: None))
    VaultAnswerer.claude("key", "claude-sonnet-5", second_model="claude-sonnet-5")
    assert made == [("claude-sonnet-5", False)]  # the same model is no second opinion
