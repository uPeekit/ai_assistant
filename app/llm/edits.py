"""Changing one place in a note instead of rewriting the whole of it.

The note is shown to the model as numbered lines (`app/vault/mdedit.py: numbered`); the model
answers with a short list of operations against those numbers, or — when the instruction really
is "rewrite all of this" — with the whole new text in `full`. Both come back from one call, and
the model picks.

Why an edit script rather than a rewritten note:

* it costs a couple of hundred output tokens instead of the whole note again;
* a picture is just a line, so removing one needs no special case;
* the reply can say exactly what changed, not only that the note got shorter.

Nothing here reads or writes anything: `app/vault/mdedit.py: apply_edits` carries a plan out.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import anthropic

from app.llm.health import Health, describe
from app.llm.prompts import EDIT_PROMPT, edit_message
from app.llm.rewrite import unfence

log = logging.getLogger(__name__)

OPS = ("replace", "insert", "delete", "image")
MAX_TOKENS = 8_000
# What one page may show the model. Past this the caller narrows to a section first.
MAX_INPUT = 20_000
_STRING = {"type": "string"}
_INT = {"type": "integer"}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props),
            "properties": props}


EDIT_SCHEMA = _obj({
    "op": {"enum": list(OPS)},
    # replace/delete: which line. insert/image: the line to put it after.
    "at": _INT,
    # delete only: the last line of a range, or 0 for the single line named by `at`.
    "to": _INT,
    "text": _STRING,
    "url": _STRING,
    "caption": _STRING,
})
PLAN_SCHEMA = _obj({"edits": {"type": "array", "items": EDIT_SCHEMA}, "full": _STRING})


class EditError(Exception):
    """`reason` is a code from app/llm/health.py when Claude could not be used at all."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class TooLong(EditError):
    """The page is longer than one call may read. The model would be shown the top of it, and
    a whole new text written from that would delete the rest unread."""


class NothingToChange(EditError):
    """The model read the page and found nothing the instruction applies to. Not a failure:
    the user asked for something that is not there, and telling them so is the answer."""


@dataclass(frozen=True)
class Edit:
    op: str
    at: int = 0
    to: int = 0
    text: str = ""
    url: str = ""
    caption: str = ""

    @property
    def span(self) -> range:
        """The lines a delete covers, `at` alone when no range was given."""
        end = self.to if self.to >= self.at else self.at
        return range(self.at, end + 1)


@dataclass
class EditPlan:
    edits: list[Edit] = field(default_factory=list)
    # The model chose to rewrite everything rather than edit places. The caller then uses the
    # whole-text path, which has its own rules about what may be replaced.
    full: str = ""

    def __bool__(self) -> bool:
        return bool(self.edits or self.full.strip())


def parse(data: dict) -> EditPlan:
    """The model's raw answer as a plan. Nothing here judges whether the edits are allowed."""
    edits = []
    for raw in data.get("edits") or []:
        if not isinstance(raw, dict):
            continue
        try:
            edits.append(Edit(
                op=str(raw.get("op", "")),
                at=int(raw.get("at") or 0),
                to=int(raw.get("to") or 0),
                text=str(raw.get("text", "")),
                url=str(raw.get("url", "")).strip(),
                caption=str(raw.get("caption", "")).strip(),
            ))
        except (TypeError, ValueError):
            continue
    return EditPlan(edits=edits, full=unfence(str(data.get("full", ""))))


class Editor:
    def __init__(self, api_key: str, model: str, *, timeout_s: float = 120.0,
                 client: anthropic.AsyncAnthropic | None = None,
                 health: Health | None = None) -> None:
        self.model = model
        self._health = health or Health()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def plan(self, page: str, instruction: str) -> tuple[EditPlan, int, int]:
        """What to change, and what it cost. Raises EditError."""
        if not page.strip():
            raise EditError("nothing to edit")
        if len(page) > MAX_INPUT:
            raise TooLong(f"{len(page)} characters, the limit is {MAX_INPUT}")
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=EDIT_PROMPT,
                messages=[{"role": "user",
                           "content": edit_message(page, instruction)}],
                output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
            )
        except anthropic.APIError as e:
            raise EditError(describe(e), self._health.record(e)) from None
        self._health.ok()
        if resp.stop_reason == "refusal":
            raise EditError("claude declined")
        if resp.stop_reason == "max_tokens":
            raise EditError("answer cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            plan = parse(json.loads(text or "{}"))
        except json.JSONDecodeError:
            raise EditError("not JSON") from None
        if not plan:
            raise NothingToChange("no changes")
        log.info("%s planned %d edits%s", self.model, len(plan.edits),
                 " (whole text)" if plan.full else "")
        return plan, resp.usage.input_tokens, resp.usage.output_tokens
