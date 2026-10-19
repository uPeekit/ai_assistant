"""A multi-step message run as a plan (app/vault/pipeline.py with app/vault/planner.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from app import texts
from app.chats import ChatScope
from app.llm import staged_prompts as P
from app.vault.filer import FilerError
from app.vault.index import VaultIndex
from app.vault.pipeline import MAX_INPUT, VaultPipeline
from app.vault.planner import Plan, Step
from app.vault.staged import StagedFiler
from app.vault.writer import VaultWriter
from tests.test_vault_staged import Script

NOW = datetime(2026, 10, 19, 12, 0)


@pytest.fixture
def index(tmp_path):
    (tmp_path / "Заметки").mkdir()
    (tmp_path / "Заметки/старое.md").write_text("старая заметка\n", encoding="utf-8")
    (tmp_path / "_bot.md").write_text("# Правила\n\n- Заметки — в «Заметки».\n",
                                      encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


class FakePlanner:
    def __init__(self, plan: Plan | None = None, several: bool = True, derive=None) -> None:
        self._plan = plan
        self._several = several
        self._derive = derive or (lambda text, inputs: f"вывод: {text}")
        self.calls: list[str] = []
        self.derived: list[tuple[str, str]] = []

    async def several(self, message, context):
        self.calls.append("several")
        return self._several

    async def plan(self, message, context):
        self.calls.append("plan")
        return self._plan

    async def derive(self, text, inputs):
        self.derived.append((text, inputs))
        out = self._derive(text, inputs)
        if isinstance(out, Exception):
            raise out
        return out


def pipe(index, script, planner, **kw) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         StagedFiler(script, "haiku"), now=lambda: NOW, planner=planner, **kw)


def note_script(title="Детективы", **more) -> Script:
    return Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                  FOLDER_PROMPT={"items": [{"title": title, "props": [], "body": ["список"],
                                            "tags": []}], "lookup": "", "web": "",
                                 "media": "text"}, **more)


async def research_of(found: dict):
    async def research(message, query, media):
        return found[query]
    return research


DETECTIVES = Plan((
    Step(1, "research", "найди интересные детективы"),
    Step(2, "derive", "составь ранжированный список из #1", (1,)),
    Step(3, "write", "создай заметку Детективы в папке Заметки со списком из #2", (2,)),
    Step(4, "derive", "посоветуй три книги — верх списка #2", (2,), reply=True),
))


async def test_a_one_step_message_runs_as_today_after_one_filter_call(index):
    planner = FakePlanner(several=False)
    turn = await pipe(index, note_script("Мысли"), planner).handle("запиши мысли в заметки")

    assert planner.calls == ["several"]
    assert [w.kind for w in turn.writes] == ["note"] and not turn.planned


async def test_a_plan_runs_in_order_and_hands_results_on(index):
    script = note_script()
    planner = FakePlanner(DETECTIVES, derive=lambda text, inputs: (
        "1. Имя розы\n2. Шпион" if "ранжированный" in text else "Советую «Имя розы»."))
    research = await research_of({"найди интересные детективы": "Имя розы, Шпион — находки"})

    turn = await pipe(index, script, planner).handle(
        "найди детективы, создай страницу со списком и посоветуй", research=research)

    assert planner.calls == ["several", "plan"]
    assert "Имя розы, Шпион — находки" in planner.derived[0][1]  # #2 was handed #1
    folder = next(c for name, _, c in script.asked if name == "FOLDER_PROMPT")
    assert P.H_INPUTS in folder and "1. Имя розы" in folder  # the write was handed #2
    assert [w.kind for w in turn.writes] == ["note"] and turn.planned
    assert turn.reply_line() == "Советую «Имя розы»."  # the short answer, no ✅ line


async def test_independent_steps_run_together(index):
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def research(message, query, media):
        started[query].set()
        other = started["b" if query == "a" else "a"]
        await asyncio.wait_for(other.wait(), 1)  # deadlocks if the steps ran one by one
        return query

    plan = Plan((Step(1, "research", "a"), Step(2, "research", "b"),
                 Step(3, "derive", "сравни", (1, 2), reply=True)))
    turn = await pipe(index, note_script(), FakePlanner(plan)).handle("x", research=research)
    assert turn.reply_line() == "вывод: сравни"


async def test_a_failed_step_is_named_and_its_dependents_skipped_others_run(index):
    plan = Plan((Step(1, "research", "найди"), Step(2, "derive", "отбери", (1,)),
                 Step(3, "write", "создай заметку Детективы из #2", (2,)),
                 Step(4, "research", "другое", reply=True)))
    planner = FakePlanner(plan, derive=lambda text, inputs: FilerError("down"))
    research = await research_of({"найди": "находки", "другое": "ответ про другое"})

    message = "найди, отбери, запиши и ещё другое"
    turn = await pipe(index, note_script(), planner).handle(message, research=research)

    line = turn.reply_line()
    assert "ответ про другое" in line
    assert texts.PLAN_PART_FAILED.format(what="отбери") in line
    assert texts.PLAN_PART_FAILED.format(what="создай заметку Детективы из #2") in line
    # The words are kept once, as the owner wrote them, because a write part failed.
    [kept] = turn.writes
    assert message in index.read(kept.path)


async def test_a_step_never_falls_back_to_the_inbox(index):
    """A write step the stages could not place is a failed part, not planner text in the
    inbox; the owner's own message is what is kept."""
    unclear = Script(INTENT_PROMPT={"intent": "unclear"})
    plan = Plan((Step(1, "research", "найди"), Step(2, "write", "запиши что-то из #1", (1,))))
    research = await research_of({"найди": "находки"})
    turn = await pipe(index, unclear, FakePlanner(plan)).handle("сообщение", research=research)

    [kept] = turn.writes
    assert "запиши что-то" not in index.read(kept.path)
    assert "сообщение" in index.read(kept.path)


async def test_a_refused_plan_falls_back_to_today(index):
    planner = FakePlanner(None)
    turn = await pipe(index, note_script("Мысли"), planner).handle("запиши мысли и найди")
    assert planner.calls == ["several", "plan"] and [w.kind for w in turn.writes] == ["note"]


async def test_a_held_turn_never_plans(index):
    planner = FakePlanner(DETECTIVES)

    async def go():
        return True

    await pipe(index, note_script("Мысли"), planner).handle("запиши мысли", go=go)
    assert planner.calls == []


async def test_a_groups_plan_writes_only_in_its_scope(index):
    """A chat that may only ask: the plan's write step is refused like any write there."""
    scope = ChatScope(chat_id=-100, title="club", rights=frozenset({"ask"}), tag="club",
                      folder="Заметки")
    plan = Plan((Step(1, "research", "найди"),
                 Step(2, "write", "создай заметку Детективы из #1", (1,), reply=True)))
    research = await research_of({"найди": "находки"})
    p = pipe(index, note_script(), FakePlanner(plan)).scoped(scope)

    turn = await p.handle("найди и запиши", research=research)
    assert turn.writes == []


async def test_a_huge_result_is_cut_before_it_is_handed_on(index):
    plan = Plan((Step(1, "research", "найди"), Step(2, "derive", "сократи", (1,), reply=True)))
    planner = FakePlanner(plan)
    research = await research_of({"найди": "x" * 50_000})
    await pipe(index, note_script(), planner).handle("найди и сократи", research=research)

    assert len(planner.derived[0][1]) <= MAX_INPUT + 20
