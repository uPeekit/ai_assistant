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
    section = folder.split(P.H_INPUTS)[1].split("\n\n" + P.H_TODAY)[0]  # the inputs alone
    assert len(section) <= MAX_STEP_INPUTS + 200


async def test_the_remind_stage_sees_handed_inputs():
    script = Script(INTENT_PROMPT={"intent": "remind"},
                    REMIND_PROMPT={"op": "add", "items": [{"text": "прочитать Имя розы",
                                                             "due": "", "time": "",
                                                             "repeat": ""}],
                                   "target": "none"})
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


APPEND = {"heading": "", "body": ["строка"], "lookup": "", "web": "", "media": "text"}


@pytest.mark.parametrize("message", [
    "добавь в заметку Проекты: сделать сайт",
    "в заметку Проекты: новый кран",
    "добавь в заметку Проекты новую книгу Имя розы",
    "допиши в заметку Проекты, что сделали визы",
    "создай задачу в заметке Проекты: позвонить",
])
async def test_an_append_that_mentions_making_something_stays_an_append(message):
    """The new-page rule took any make word anywhere and any page word anywhere: «добавь в
    заметку Идеи: сделать сайт» became a new note. A page is asked for only by a make verb
    right before the page word, not by «в заметку», and not when the note is named."""
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    APPEND_PROMPT=dict(APPEND))
    actions, _, _ = await StagedFiler(script, "haiku").file(
        message, ctx(guide="- [[Проекты]] — списки"))
    assert actions[0]["action"] == "append"


@pytest.mark.parametrize("message", ["создай страницу детективы со списком",
                                     "сделай новую заметку детективы со списком"])
async def test_a_page_asked_for_by_name_is_a_new_note(message):
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "n1"},
                    FOLDER_PROMPT={"items": [{"title": "детективы", "props": [], "body": [],
                                              "tags": []}], "lookup": "", "web": "",
                                   "media": "text"})
    actions, _, _ = await StagedFiler(script, "haiku").file(
        message, ctx(guide="- [[Проекты]] — списки"))
    assert actions[0]["action"] == "note"


async def test_cancelling_a_message_while_the_reader_unwinds_writes_nothing(index):
    """Cancelled while the dropped reader was still unwinding, the message carried on into
    its plan and wrote: the cancellation was swallowed with the reader's own."""
    unwinding, inside = asyncio.Event(), asyncio.Event()

    class SlowToStop(Script):
        async def __call__(self, system, schema, content, max_tokens=0):
            inside.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                unwinding.set()
                await asyncio.sleep(0.2)  # an HTTP request takes a moment to give up
                raise
            return await super().__call__(system, schema, content, max_tokens)

    plan = Plan((Step(1, "research", "найди"),
                 Step(2, "write", "создай заметку Детективы из #1", (1,), reply=True)))
    class Planner(FakePlanner):
        async def plan(self, message, context, tally=None):
            await inside.wait()  # the reader is mid-call when the plan comes
            return await super().plan(message, context, tally)

    p = pipe(index, SlowToStop(**note_script().answers), Planner(plan))
    task = asyncio.create_task(p.handle("найди и запиши",
                                        research=await research_of({"найди": "x"})))
    await asyncio.wait_for(unwinding.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (index.root / "Заметки/Детективы.md").exists()


async def test_a_spent_search_budget_is_said_in_the_owners_language(index):
    plan = Plan((*(Step(i, "research", f"q{i}") for i in range(1, MAX_RESEARCH + 2)),
                 Step(9, "derive", "итог", tuple(range(1, MAX_RESEARCH + 1)), reply=True)))
    research = await research_of({f"q{i}": "x" for i in range(1, MAX_RESEARCH + 2)})
    turn = await pipe(index, note_script(), FakePlanner(plan)).handle("x", research=research)
    assert "spent" not in turn.reply_line()
    assert texts.PLAN_SEARCHES_SPENT in turn.reply_line()


async def test_the_dropped_readers_tokens_are_counted(index):
    class Later(FakePlanner):
        async def plan(self, message, context, tally=None):
            await asyncio.sleep(0.05)  # the reader has finished by now
            return await super().plan(message, context, tally)

    plan = Plan((Step(1, "research", "a"), Step(2, "derive", "итог", (1,), reply=True)))
    turn = await pipe(index, note_script(), Later(plan)).handle(
        "найди детективы", research=await research_of({"a": "находки"}))
    assert turn.prompt_tokens >= 300  # intent, place and folder stages, 100 each


async def test_notes_meant_by_a_read_step_are_not_crowded_out_by_a_weak_table(index):
    class Weak(FakeGoogle):
        def __init__(self):
            self.found = [Found(source="Таблица", title="t", group="t", order=1,
                                text="ряд " * 20_000, score=1)]

    (index.root / "Заметки/детективы.md").write_text("Детективы: Имя розы, Шпион\n",
                                                     encoding="utf-8")
    index.refresh()
    plan = Plan((Step(1, "read", "заметки про детективы"),
                 Step(2, "derive", "перечисли", (1,), reply=True)))
    planner = FakePlanner(plan)
    from app.chats import ChatRef
    await pipe(index, note_script(), planner, google=Weak()).handle(
        "что в заметках про детективы и перечисли", chat=ChatRef(1, 1))
    assert "Имя розы" in planner.derived[0][1]


async def test_a_search_repeated_in_one_message_takes_one_slot(index):
    plan = Plan((Step(1, "research", "одно"), Step(2, "research", "одно"),
                 Step(3, "research", "другое"), Step(4, "research", "третье"),
                 Step(5, "derive", "итог", (1, 2, 3, 4), reply=True)))
    research = await research_of({"одно": "a", "другое": "b", "третье": "c"})
    turn = await pipe(index, note_script(), FakePlanner(plan)).handle("x", research=research)
    assert texts.PLAN_SEARCHES_SPENT not in turn.reply_line()


def test_the_remind_and_change_task_prompts_say_what_handed_data_is():
    assert P.INPUTS_RULE in P.REMIND_PROMPT and P.INPUTS_RULE in P.CHANGE_TASK_PROMPT
