"""An answer to a question about a page the user sent a link to, written nowhere.

"What's the deposit on this flat?" with a kv.ee link: one call, the question and the page, and
the reply goes back to the chat. No tools — the page is already read (app.web.links); this only
answers from it, and says so when the page does not."""

from __future__ import annotations

import logging

import anthropic

from app.llm.health import Health, describe
from app.llm.prompts import LINK_ANSWER_PROMPT, link_answer_message

log = logging.getLogger(__name__)

MAX_TOKENS = 1500


class LinkAnswerError(Exception):
    """`reason` is a code from app/llm/health.py when Claude could not be used at all."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class LinkAnswerer:
    def __init__(self, api_key: str, model: str, *, timeout_s: float = 60.0,
                 client: anthropic.AsyncAnthropic | None = None,
                 health: Health | None = None) -> None:
        self.model = model
        self._health = health or Health()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def answer(self, question: str, pages: list) -> str:
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=LINK_ANSWER_PROMPT,
                messages=[{"role": "user", "content": link_answer_message(question, pages)}],
            )
        except anthropic.APIError as e:
            raise LinkAnswerError(describe(e), self._health.record(e)) from None
        self._health.ok()
        if resp.stop_reason == "refusal":
            raise LinkAnswerError("claude declined")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise LinkAnswerError(f"no answer (stop_reason={resp.stop_reason})")
        log.info("link answer: %d page(s), %d chars | %d+%d tok", len(pages), len(text),
                 resp.usage.input_tokens, resp.usage.output_tokens)
        return text
