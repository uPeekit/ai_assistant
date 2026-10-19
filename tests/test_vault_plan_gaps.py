"""Plans, second round: the read step, input caps, budgets, and the gaps the review left."""

from __future__ import annotations

import asyncio

import pytest

from app import texts
from app.google.sources import Found
from app.llm import staged_prompts as P
from app.vault.index import VaultIndex
from app.vault.pipeline import MAX_INPUT, MAX_STEP_INPUTS
from app.vault.planner import MAX_RESEARCH, Plan, Step, VaultPlanner
from app.vault.staged import StagedFiler
from tests.test_vault_plan_run import FakePlanner, note_script, pipe, research_of
from tests.test_vault_staged import Script, ctx


@pytest.fixture
def index(tmp_path):
    (tmp_path / "Заметки").mkdir()
    (tmp_path / "Заметки/старое.md").write_text("старая заметка\n", encoding="utf-8")
    (tmp_path / "_bot.md").write_text("# Правила\n\n- Заметки — в «Заметки».\n",
                                      encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


class FakeGoogle:
    """Two tabs of one table and a notes document, ranked as find() would."""

    def __init__(self) -> None:
        rows = [Found(source="Оценки", title=f"all (строки, часть {i})", group="all",
                      order=i, text=f"книга {i}0 — avg {i}; " * 400, score=3)
                for i in range(1, 4)]
        other = [Found(source="Оценки", title="year1", group="year1", order=9,
                       text="другая вкладка", score=2)]
        notes = [Found(source="Выводы", title="Книга", text="заметки", score=1)]
        self.found = [rows[1], rows[0], *other, rows[2], *notes]

    async def find(self, query, chat_id, *, owner):
        return list(self.found)

    def sign_in_notice(self):
        return ""


async def test_a_read_step_hands_on_a_whole_tab_in_its_own_order(index):
    """«which of our books are detectives, with their ratings» needs every row: a question
    reads ~12 000 characters and answers in a paragraph. A read step fetches the records
    whole, as data, for a derive step to filter."""
    from app.chats import ChatRef

    plan = Plan((Step(1, "read", "таблица оценок клуба: все книги"),
                 Step(2, "derive", "какие из них детективы, с оценками", (1,), reply=True)))
    planner = FakePlanner(plan)
    p = pipe(index, note_script(), planner, google=FakeGoogle())
    await p.handle("какие детективы мы читали и с какой оценкой", chat=ChatRef(1, 1))

    [(_, inputs)] = planner.derived
    assert "другая вкладка" not in inputs and "заметки" not in inputs
    assert inputs.index("книга 10") < inputs.index("книга 20") < inputs.index("книга 30")
    assert len(inputs) > MAX_INPUT  # a derive step may read a whole table


async def test_a_write_or_ask_step_gets_at_most_the_total_input_cap(index):
    plan = Plan((Step(1, "research", "a"), Step(2, "research", "b"), Step(3, "research", "c"),
                 Step(4, "write", "создай заметку Детективы из #1, #2, #3", (1, 2, 3)),
                 Step(5, "derive", "итог", (4,), reply=True)))
    script = note_script()
    research = await research_of({q: q * 9000 for q in ("a", "b", "c")})
    await pipe(index, script, FakePlanner(plan)).handle("x", research=research)

    folder = next(c for name, _, c in script.asked if name == "FOLDER_PROMPT")
    section = folder.split(P.H_INPUTS)[1]
    assert len(section) <= MAX_STEP_INPUTS + 200


async def test_the_remind_stage_sees_handed_inputs():
    script = Script(INTENT_PROMPT={"intent": "remind"},
                    REMIND_PROMPT={"op": "add", "text": "прочитать Имя розы", "due": "",
                                   "time": "", "repeat": "", "target": "none"})
    await StagedFiler(script, "haiku").file("напомни прочитать первую книгу из #2",
                                            ctx(inputs="#2: 1. Имя розы"))
    remind = next(c for name, _, c in script.asked if name == "REMIND_PROMPT")
    assert P.H_INPUTS in remind and "Имя розы" in remind


async def test_the_filter_and_plan_tokens_are_counted(index):
    class Ask:
        async def __call__(self, system, schema, content, max_tokens=0):
            return {"several": False}, 100, 10

    planner = VaultPlanner(Ask(), Ask())
    turn = await pipe(index, note_script("Мысли"), planner).handle("запиши мысли в заметки")
    assert turn.prompt_tokens >= 100 + 300  # the filter and the three stages


async def test_without_a_reply_step_the_last_result_is_the_answer(index):
    plan = Plan((Step(1, "research", "a"), Step(2, "derive", "сравни", (1,))))
    turn = await pipe(index, note_script(), FakePlanner(plan)).handle(
        "x", research=await research_of({"a": "находки"}))
    assert turn.reply_line() == "вывод: сравни"


def test_a_cyclic_plan_built_by_hand_does_not_hang():
    plan = Plan((Step(1, "write", "a", (2,)), Step(2, "write", "b", (1,))))
    assert sum(len(layer) for layer in plan.layers()) == 2


async def test_a_message_runs_at_most_the_research_budget(index):
    """Searches inside write steps count as well: a plan could run eight research loops."""
    calls = []

    async def research(message, query, media):
        calls.append(query)
        return f"находки {query}"

    web = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                 FOLDER_PROMPT={"items": [{"title": "Детективы", "props": [], "body": [],
                                           "tags": []}], "lookup": "", "web": "детективы",
                                "media": "text"})
    plan = Plan((*(Step(i, "research", f"q{i}") for i in range(1, MAX_RESEARCH + 1)),
                 Step(9, "write", "найди и создай заметку Детективы", reply=True)))
    await pipe(index, web, FakePlanner(plan)).handle("найди всё", research=research)
    assert len(calls) == MAX_RESEARCH


async def test_the_filter_runs_beside_the_reader_not_before_it(index):
    """The quick filter cost every one-step message a model round-trip before anything else
    started: now the reader starts at once and its answer is dropped when a plan comes."""
    started = asyncio.Event()

    class Slow(FakePlanner):
        async def several(self, message, context, tally=None):
            await asyncio.wait_for(started.wait(), 1)  # times out if the reader waits for us
            return False

    class Marking(Script):
        async def __call__(self, system, schema, content, max_tokens=0):
            started.set()
            return await super().__call__(system, schema, content, max_tokens)

    script = Marking(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                     FOLDER_PROMPT={"items": [{"title": "Мысли", "props": [], "body": [],
                                               "tags": []}], "lookup": "", "web": "",
                                    "media": "text"})
    turn = await pipe(index, script, Slow()).handle("запиши мысли в заметки")
    assert [w.kind for w in turn.writes] == ["note"]


async def test_asked_to_create_a_page_a_hub_note_is_not_appended_to():
    """«создай страницу со списком» put the list into the owner's reading hub, which the guide
    links to lists: a new page was asked for."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    FOLDER_PROMPT={"items": [{"title": "Детективы", "props": [], "body": [],
                                              "tags": []}], "lookup": "", "web": "",
                                   "media": "text"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        "создай страницу детективы со списком", ctx(guide="- [[Проекты]] — списки"))
    assert actions[0]["action"] == "note"
    assert texts.VAULT_NOTES_DIR == actions[0]["folder"]
