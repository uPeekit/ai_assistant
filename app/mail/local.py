"""The mail classifier, answered by a local model instead of Claude.

Same prompt, same JSON schema, same `gate` — so a digest written from this is comparable with
the real one line for line, and a small model that answers off-list is as harmless here as a
large one: the only things it can produce are a configured bucket and a line of text.

It started as a comparison: `MailService` runs it beside the real classifier as a shadow, and
the bot sends both digests to be read side by side (MAIL_SHADOW_MODEL). After a week of that,
qwen3:8b took the job over: MAIL_MODEL set to a non-Claude name makes this the real classifier.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.llm.ollama import CTX_MARGIN, wants_think_flag
from app.llm.prompts import MAIL_PROMPT, mail_message
from app.mail.classify import MAX_SUMMARY, OTHER, ClassifyError, Sorted, _schema, gate
from app.mail.imap import Message

log = logging.getLogger(__name__)

# How many emails to try in one request. Measured: more at once sorts *better* — the model
# tells buckets apart by contrast (qwen3:4b went 7/16 -> 14/16 from one email a request to
# sixteen). The only limit is the context window, and a request that overflows it is split in
# half and tried again, so this is where to start, not a size anything must fit into.
BATCH = 20
# What one email's answer may take, and a little for the brackets around them all: the
# budget for a whole answer. A summary is capped at MAX_SUMMARY characters by the schema;
# this only stops a model that loops somewhere the schema allows.
TOKENS_PER_EMAIL = 160
TOKENS_OVERHEAD = 200
MAX_ID = 40


def capped_schema(buckets: list[str]) -> dict:
    """The classifier's schema with every free string given a length.

    Ollama holds a local model to the schema as a grammar, and a length in the grammar forces
    a string closed. Without one, gemma3:1b repeated a sentence inside a summary until the
    context was full, three answers in four — and a cut-off answer is not JSON. The cap costs
    nothing: the digest cuts a summary to MAX_SUMMARY anyway (classify.gate). Claude's schema
    is left as it is; it never looped, and its structured output is its own."""
    schema = _schema(buckets)
    item = schema["properties"]["messages"]["items"]["properties"]
    item["summary"] = {**item["summary"], "maxLength": MAX_SUMMARY}
    item["id"] = {**item["id"], "maxLength": MAX_ID}
    return schema


class ContextOverflow(ClassifyError):
    """Ollama drops the head of a prompt longer than num_ctx without saying so: the system
    prompt goes first, and the answer is to a question the model was never fully asked."""


class LocalClassifier:
    def __init__(self, base_url: str, model: str, buckets: list[str], *,
                 meanings: dict[str, str] | None = None, batch: int = BATCH,
                 num_ctx: int = 8192, timeout_s: float = 600.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.model = model
        self.buckets = [*buckets, OTHER] if OTHER not in buckets else list(buckets)
        self.meanings = dict(meanings or {})
        self._batch = max(1, batch)
        self._num_ctx = num_ctx
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s,
                                         transport=transport)
        # Why the last run produced nothing usable, when it produced nothing usable.
        self.last_error = ""

    async def aclose(self) -> None:
        await self._unload()
        await self._client.aclose()

    async def _unload(self) -> None:
        """Let go of the card. The digest runs a couple of times a day; a model left resident in
        between holds the VRAM the Notion fallback and the transcriber need."""
        try:
            await self._client.post("/api/generate", json={
                "model": self.model, "keep_alive": 0})
        except httpx.HTTPError:
            pass

    async def sort(self, messages: list[Message]) -> tuple[list[Sorted], int, int]:
        """Every message with its bucket and summary — or nothing, when the model never answered.

        A chunk that overflows the context is split in half and each half tried again, down to
        a single email, so the digest is complete whatever the batch size. A chunk that fails
        otherwise comes back as `other`, as in the real digest. But when *no* chunk got an
        answer, this returns nothing and `last_error` says why: a comparison showing every
        message under `other` because Ollama is not running would read as a judgement the
        model made.
        """
        self.last_error = ""
        answered = 0
        used_in = used_out = 0

        async def fit(chunk: list[Message]) -> list[Sorted]:
            nonlocal answered, used_in, used_out
            try:
                answer, tokens_in, tokens_out = await self._ask(chunk)
            except ContextOverflow as e:
                if len(chunk) > 1:
                    half = len(chunk) // 2
                    log.info("mail shadow: %d emails overflow the context, splitting",
                             len(chunk))
                    return await fit(chunk[:half]) + await fit(chunk[half:])
                self.last_error = str(e)  # one email longer than the window: listed, not sorted
                return gate({}, chunk, self.buckets)
            except ClassifyError as e:
                self.last_error = str(e)
                log.warning("mail shadow failed for a batch: %s", e)
                return gate({}, chunk, self.buckets)
            answered += 1
            used_in += tokens_in
            used_out += tokens_out
            return gate(answer, chunk, self.buckets)

        out: list[Sorted] = []
        try:
            for start in range(0, len(messages), self._batch):
                out += await fit(messages[start:start + self._batch])
        finally:
            await self._unload()
        if messages and not answered:
            return [], used_in, used_out
        return out, used_in, used_out

    async def _ask(self, batch: list[Message]) -> tuple[object, int, int]:
        payload = mail_message([{
            "id": m.uid,
            "from": m.sender,
            "subject": m.subject,
            "bulk": m.bulk,
            "text": m.body,
        } for m in batch], self.buckets, self.meanings)
        body: dict = {
            "model": self.model,
            # A system *message*: /api/chat ignores a top-level "system" field outright.
            "messages": [{"role": "system", "content": MAIL_PROMPT},
                         {"role": "user", "content": payload}],
            "stream": False,
            "format": capped_schema(self.buckets),
            "options": {"temperature": 0.0, "num_ctx": self._num_ctx,
                        "num_predict": TOKENS_OVERHEAD + TOKENS_PER_EMAIL * len(batch)},
            "keep_alive": "10m",
        }
        if wants_think_flag(self.model):
            body["think"] = False
        try:
            resp = await self._client.post("/api/chat", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ClassifyError(f"ollama: {e}") from None
        try:
            data = resp.json()
        except ValueError:  # a proxy's error page, a half-written reply
            raise ClassifyError("ollama answered something that is not JSON") from None
        if not isinstance(data, dict):
            raise ClassifyError("ollama answered something that is not an object")
        prompt_tokens = data.get("prompt_eval_count", 0) or 0
        if prompt_tokens >= self._num_ctx - CTX_MARGIN:
            raise ContextOverflow(
                f"{len(batch)} email(s) filled the context ({prompt_tokens}/{self._num_ctx})")
        text = (data.get("message") or {}).get("content", "")
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            if data.get("done_reason") == "length":
                # Not a model that cannot write JSON: one that did not stop writing.
                raise ClassifyError(
                    f"answer cut off after {data.get('eval_count', 0)} tokens "
                    "(the model kept writing)") from None
            raise ClassifyError("not JSON") from None
        return answer, prompt_tokens, data.get("eval_count", 0) or 0
