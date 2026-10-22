"""The vault planner (app/vault/planner.py): one step or several, the plan, and derived steps."""

from __future__ import annotations

import pytest

from app.llm import staged_prompts as P
from app.vault.filer import FilerError
from app.vault.planner import MAX_STEPS, Plan, VaultPlanner, plan_from


class Ask:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, dict, str]] = []

    async def __call__(self, system, schema, content, max_tokens=0):
        self.asked.append((system, schema, content))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, 100, 10


def step(i, kind="write", needs=(), reply=False, text=None):
    return {"id": i, "kind": kind, "text": text or f"шаг {i}", "needs": list(needs),
            "reply": reply}


DETECTIVES = [step(1, "research"), step(2, "derive", [1]), step(3, "write", [2]),
              step(4, "ask"), step(5, "derive", [4]), step(6, "derive", [2, 5], reply=True)]


async def test_one_step_messages_are_told_apart_from_several():
    light = Ask({"several": False}, {"several": True})
    planner = VaultPlanner(light, Ask())
    assert await planner.several("купи молоко", "ctx") is False
    assert await planner.several("найди детективы, создай страницу и посоветуй", "ctx") is True
    assert light.asked[0][0] == P.SPLIT_PROMPT and "ctx" in light.asked[0][2]


async def test_a_filter_that_fails_means_one_step():
    planner = VaultPlanner(Ask(FilerError("down")), Ask())
    assert await planner.several("найди и создай", "") is False


async def test_a_plan_is_read_into_ordered_layers():
    strong = Ask({"steps": DETECTIVES})
    plan = await VaultPlanner(Ask(), strong).plan("найди детективы…", "ctx")

    assert [[s.id for s in layer] for layer in plan.layers()] == [[1, 4], [2, 5], [3, 6]]
    assert plan.steps[5].reply and plan.steps[5].needs == (2, 5)
    assert strong.asked[0][0] == P.PLAN_STEPS_PROMPT


@pytest.mark.parametrize("steps", [
    [step(1), step(2, needs=[3]), step(3)],  # a need that comes later: a cycle in disguise
    [step(1), step(2, needs=[7])],  # unknown id
    [step(1), step(1)],  # duplicate id
    [step(1)],  # one step is no plan
    [step(i) for i in range(1, MAX_STEPS + 2)],  # too many
    [step(1, "research"), step(2, "research"), step(3, "research"), step(4, "research")],
    [step(1), step(2, text=" ")],  # empty text
    [step(1), step(2, "dance")],  # unknown kind
])
def test_a_broken_plan_is_refused(steps):
    assert plan_from({"steps": steps}) is None


async def test_a_plan_the_model_could_not_give_is_none():
    assert await VaultPlanner(Ask(), Ask(FilerError("down"))).plan("x", "") is None


async def test_derive_gets_the_inputs_as_data():
    strong = Ask({"text": "Советую «Имя розы»."})
    out = await VaultPlanner(Ask(), strong).derive("посоветуй из списка", "#2: Имя розы, …")

    assert out == "Советую «Имя розы»."
    system, _, content = strong.asked[0]
    assert system == P.DERIVE_PROMPT and "#2: Имя розы" in content and P.H_INPUTS in content


async def test_a_derive_that_fails_raises():
    with pytest.raises(FilerError):
        await VaultPlanner(Ask(), Ask(FilerError("down"))).derive("x", "y")


def test_plan_is_a_value():
    plan = plan_from({"steps": [step(1), step(2, needs=[1])]})
    assert isinstance(plan, Plan) and [s.kind for s in plan.steps] == ["write", "write"]


async def test_one_search_feeding_one_write_is_one_step():
    """«find X and write it to a page» split into a search and a write is what the one-step
    path already does, findings written as they stand: such a plan is no plan."""
    strong = Ask({"steps": [step(1, "research"), step(2, "write", [1], reply=True)]},
                 {"steps": [step(1, "research"), step(2, "research"),
                            step(3, "derive", [1, 2], reply=True)]})
    planner = VaultPlanner(Ask(), strong)
    assert await planner.plan("найди и запиши", "") is None
    assert await planner.plan("найди два варианта и сравни", "") is not None


def test_a_derive_with_nothing_to_derive_from_is_refused():
    """A derive step with no needs is the model answering from memory, and its output would
    count as a source for the writes after it."""
    assert plan_from({"steps": [step(1, "derive"), step(2, "write", [1])]}) is None


def test_a_step_keeps_the_users_own_verb():
    """Two reminders were split into two steps worded as plain tasks: no reminder was set."""
    from app.llm import staged_prompts as SP

    assert SP.STEP_VERB_RULE in SP.PLAN_STEPS_PROMPT


async def test_independent_questions_are_one_step():
    """«скинь выводы по книге X и кто победил в квизе» was split into two questions, and the
    second lost the book: one answer reads both, with the book. Questions that hand nothing
    on to each other are one message, as it was asked."""
    strong = Ask({"steps": [step(1, "ask"), step(2, "ask", reply=True)]},
                 {"steps": [step(1, "ask"), step(2, "derive", [1], reply=True)]})
    planner = VaultPlanner(Ask(), strong)
    assert await planner.plan("выводы по книге и кто победил в квизе", "") is None
    assert await planner.plan("найди в заметках и посоветуй", "") is not None
