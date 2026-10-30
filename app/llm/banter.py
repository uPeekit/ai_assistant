"""A joke for a group that mentioned the bot with nothing to ask.

One short call, the message and the last few exchanges, and the line goes back to the chat. Nothing
of the vault is shown, so a joke can give nothing away. How it talks is the owner's, from the admin
page (`note`); the prompt alone keeps it to a line or two."""

from __future__ import annotations

import logging
from collections.abc import Callable

import anthropic

from app.llm.health import Health, describe
from app.llm.prompts import BANTER_PROMPT, banter_message, banter_system

log = logging.getLogger(__name__)

MAX_TOKENS = 400


class BanterError(Exception):
    """`reason` is a code from app/llm/health.py when Claude could not be used at all."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class Banter:
    def __init__(self, api_key: str, model: str, *, note: Callable[[], str] = lambda: "",
                 timeout_s: float = 30.0, client: anthropic.AsyncAnthropic | None = None,
                 health: Health | None = None) -> None:
        self.model = model
        self._note = note
        self._health = health or Health()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def line(self, message: str, name: str = "", history: str = "") -> tuple[str, int, int]:
        """The joke and what it cost. Thinking off: Sonnet 5 thinks unless told not to, and that
        would spend a one-line answer's tokens before the line."""
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS,
                system=banter_system(BANTER_PROMPT, self._note()),
                messages=[{"role": "user", "content": banter_message(message, name, history)}],
                thinking={"type": "disabled"},
            )
        except anthropic.APIError as e:
            raise BanterError(describe(e), self._health.record(e)) from None
        self._health.ok()
        if resp.stop_reason == "refusal":
            raise BanterError("claude declined")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise BanterError(f"no joke (stop_reason={resp.stop_reason})")
        return text, resp.usage.input_tokens, resp.usage.output_tokens
