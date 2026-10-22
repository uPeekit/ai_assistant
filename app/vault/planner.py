"""Several steps in one message: told apart, planned, and the steps that work over results.

The staged reader reads a message as one intent into one place. A message that asks for
different things, or for things that build on each other ("find X, make a page of it, compare it
with our records and recommend"), is first recognised by a cheap question to the light model —
can this be done as one step? — and only then planned by the strong model into linked steps.
Each step is a self-contained request that today's machinery runs as if it were a message of its
own (app/vault/pipeline.py); `derive` steps — filter, rank, compare, recommend — run here, over the
results they are handed. How a message splits is the model's judgement, never a rule on words or
punctuation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import anthropic

from app.llm import staged_prompts as P
from app.llm.health import Health
from app.vault.filer import FilerError
from app.vault.staged import Ask, claude_ask

log = logging.getLogger(__name__)

KINDS = ("write", "ask", "research", "derive", "read")
MAX_STEPS = 8
MAX_RESEARCH = 3
PLAN_MAX_TOKENS = 3_000
DERIVE_MAX_TOKENS = 3_000

_STRING = {"type": "string"}
SPLIT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["several"],
                "properties": {"several": {"type": "boolean"}}}
PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["steps"],
               "properties": {"steps": {"type": "array", "items": {
                   "type": "object", "additionalProperties": False,
                   "required": ["id", "kind", "text", "needs", "reply"],
                   "properties": {"id": {"type": "integer"}, "kind": {"enum": list(KINDS)},
                                  "text": _STRING,
                                  "needs": {"type": "array", "items": {"type": "integer"}},
                                  "reply": {"type": "boolean"}}}}}}
DERIVE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["text"],
                 "properties": {"text": _STRING}}


@dataclass(frozen=True)
class Step:
    id: int
    kind: Literal["write", "ask", "research", "derive", "read"]
    text: str
    needs: tuple[int, ...] = ()
    reply: bool = False


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]

    def layers(self) -> list[list[Step]]:
        """The steps in rounds: each round needs only what earlier rounds produced, so the
        steps of one round can run together."""
        done: set[int] = set()
        left = list(self.steps)
        out: list[list[Step]] = []
        while left:
            ready = [s for s in left if set(s.needs) <= done]
            if not ready:  # a cycle (only a plan not built by plan_from has one): run the rest
                out.append(left)
                break
            out.append(ready)
            done |= {s.id for s in ready}
            left = [s for s in left if s.id not in done]
        return out


def plan_from(answer: dict) -> Plan | None:
    """The plan the model gave, or None when it is not one this code can run."""
    raw = answer.get("steps") if isinstance(answer, dict) else None
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_STEPS:
        return None
    steps: list[Step] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        text = str(item.get("text", "")).strip()
        kind = item.get("kind")
        try:
            sid = int(item.get("id"))
            needs = tuple(int(n) for n in item.get("needs") or [])
        except (TypeError, ValueError):
            return None
        if not text or kind not in KINDS or any(s.id == sid for s in steps):
            return None
        # A need is a step written before this one: that alone rules out cycles.
        if any(n not in {s.id for s in steps} for n in needs):
            return None
        if kind == "derive" and not needs:  # memory, not a result: no source for a write
            return None
        steps.append(Step(sid, kind, text, needs, bool(item.get("reply"))))
    if sum(s.kind == "research" for s in steps) > MAX_RESEARCH:
        return None
    return Plan(tuple(steps))


def _count(tally: list[int] | None, used_in: int, used_out: int) -> None:
    """What a planner call used, added to the turn's tokens for the audit."""
    if tally is not None:
        tally[0] += used_in
        tally[1] += used_out


def one_step(plan: Plan) -> bool:
    """A plan that is one search feeding one write: what the one-step path already does — the
    findings written as they stand — so it goes that way. So are questions that hand nothing
    on to each other: one answer reads for all of them, with what the message said for all
    (split, «who won the quiz» lost the book the quiz was on)."""
    if all(s.kind == "ask" and not s.needs for s in plan.steps):
        return True
    kinds = sorted(s.kind for s in plan.steps)
    if kinds != ["research", "write"]:
        return False
    search = next(s for s in plan.steps if s.kind == "research")
    write = next(s for s in plan.steps if s.kind == "write")
    return search.id in write.needs


class VaultPlanner:
    def __init__(self, light: Ask, strong: Ask, light_model: str = "",
                 strong_model: str = "") -> None:
        self._light = light
        self._strong = strong
        self.light_model = light_model
        self.strong_model = strong_model

    @classmethod
    def claude(cls, api_key: str, light_model: str, strong_model: str, *,
               health: Health | None = None,
               client: anthropic.AsyncAnthropic | None = None) -> VaultPlanner:
        sdk = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=90.0, max_retries=1)
        # Thinking off: Sonnet 5 thinks unless told not to, which spent a whole answer's tokens
        # on one narrow question before.
        return cls(claude_ask(sdk, light_model, health, think=False),
                   claude_ask(sdk, strong_model, health, think=False), light_model, strong_model)

    async def several(self, message: str, context: str, tally: list[int] | None = None) -> bool:
        """Whether the message needs several steps. A failed question means one: the message
        then goes the way it always went."""
        try:
            answer, used_in, used_out = await self._light(
                P.SPLIT_PROMPT, SPLIT_SCHEMA,
                P.message(P.section(P.H_PLAN_CONTEXT, context), text=message), 200)
            _count(tally, used_in, used_out)
        except FilerError as e:
            log.info("plan filter failed (%s); one step", e)
            return False
        return answer.get("several") is True

    async def plan(self, message: str, context: str,
                   tally: list[int] | None = None) -> Plan | None:
        try:
            answer, used_in, used_out = await self._strong(
                P.PLAN_STEPS_PROMPT, PLAN_SCHEMA,
                P.message(P.section(P.H_PLAN_CONTEXT, context), text=message),
                PLAN_MAX_TOKENS)
            _count(tally, used_in, used_out)
        except FilerError as e:
            log.warning("planning failed (%s); one step", e)
            return None
        plan = plan_from(answer)
        if plan is None:
            log.info("no plan of several steps; one step")
        elif one_step(plan):
            log.info("the plan is one step's work (%s); one step",
                     ", ".join(s.kind for s in plan.steps))
            return None
        return plan

    async def derive(self, text: str, inputs: str, tally: list[int] | None = None) -> str:
        """A step over the results it is handed. Raises FilerError when the model fails."""
        answer, used_in, used_out = await self._strong(
            P.DERIVE_PROMPT, DERIVE_SCHEMA,
            P.message(P.section(P.H_INPUTS, inputs), text=text), DERIVE_MAX_TOKENS)
        _count(tally, used_in, used_out)
        out = str(answer.get("text", "")).strip()
        if not out:
            raise FilerError("empty derived step")
        return out
