"""One digest run: read what arrived since last time, sort it, and write the message.

Reading is read-only by construction — the mailbox is opened read-only to read. The one
change it can make is the digest's own button (`mark`): it flags exactly the letters that
digest listed as read, or unread again. State (where the last run stopped) lives in a small
JSON file next to the database."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app import texts
from app.mail.classify import OTHER, Classifier, Sorted
from app.mail.imap import GmailIMAP, MailboxError, Message
from app.mail.local import LocalClassifier
from app.mail.marks import DigestMarks

log = logging.getLogger(__name__)

MAX_PER_BUCKET = 8
FIRST_RUN_HOURS = 12


@dataclass
class ShadowRun:
    """One local model's go at the same mail: what it sorted, why it failed, what it took."""

    model: str
    sorted: list[Sorted] = field(default_factory=list)
    error: str = ""
    ms: int = 0


@dataclass
class MailRun:
    sorted: list[Sorted] = field(default_factory=list)
    error: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    # The mailbox generation these uids belong to, for marking them read later.
    validity: str = ""

    @property
    def empty(self) -> bool:
        return not self.sorted and not self.error


class MailState:
    """Where the last run stopped: the newest uid it saw, and the mailbox generation that uid
    belongs to. A changed UIDVALIDITY means Gmail renumbered everything, so the uid is dropped
    rather than trusted."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> tuple[str | None, str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw.get("uid") or None, str(raw.get("validity", ""))
        except (OSError, ValueError):
            return None, ""

    def write(self, uid: str | None, validity: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps({"uid": uid, "validity": validity}), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("could not save the mail state: %s", e)


def digest(run: MailRun, buckets: list[str]) -> str:
    """The message, grouped by bucket in the order the user configured them."""
    if run.error:
        return texts.MAIL_FAILED.format(error=run.error)
    if not run.sorted:
        return ""
    grouped: OrderedDict[str, list[Sorted]] = OrderedDict((b, []) for b in buckets)
    for item in run.sorted:
        grouped.setdefault(item.bucket, []).append(item)
    lines = [texts.MAIL_HEADER.format(n=len(run.sorted))]
    for bucket, items in grouped.items():
        if not items:
            continue
        lines.append(texts.MAIL_BUCKET.format(bucket=bucket, n=len(items)))
        for item in items[:MAX_PER_BUCKET]:
            lines.append(texts.MAIL_ITEM.format(sender=item.message.short_sender,
                                                 summary=item.summary))
        if len(items) > MAX_PER_BUCKET:
            lines.append(texts.MAIL_MORE.format(n=len(items) - MAX_PER_BUCKET))
    return "\n".join(lines)


def shadow_digest(shadow: ShadowRun, buckets: list[str]) -> str:
    """A comparison digest: the same mail as one local model sorted it, headed by its cost.

    The user compares these with the real one by reading them side by side, so they are
    rendered by the very same code — only the header differs.
    """
    head = texts.MAIL_SHADOW_HEADER.format(model=shadow.model, seconds=shadow.ms / 1000)
    if shadow.error:
        return head + "\n" + texts.MAIL_SHADOW_FAILED.format(error=shadow.error)
    body = digest(MailRun(sorted=shadow.sorted), buckets)
    # Its own MAIL_HEADER line would only repeat the real digest's, one message above.
    _, _, rest = body.partition("\n")
    return head + "\n" + (rest.strip() or texts.MAIL_SHADOW_EMPTY)


class MailService:
    def __init__(self, mailbox: GmailIMAP, classifier: Classifier | LocalClassifier,
                 state: MailState,
                 *, buckets: list[str] | None = None, max_per_run: int = 40,
                 source: Callable[[], tuple[list[str], dict[str, str]]] | None = None,
                 shadows: list | None = None, marks: DigestMarks | None = None) -> None:
        self._box = mailbox
        self.marks = marks
        self._classifier = classifier
        self._shadows = list(shadows or [])
        self._state = state
        self.buckets = buckets or classifier.buckets
        self._source = source  # the admin page's text, re-read on every run
        self._max = max_per_run

    async def aclose(self) -> None:
        await self._classifier.aclose()
        for shadow in self._shadows:
            await shadow.aclose()

    def _refresh_buckets(self) -> None:
        """Whatever the user has on the admin page right now. A change needs no restart."""
        if self._source is None:
            return
        names, meanings = self._source()
        if not names:
            return
        self._classifier.buckets = ([*names, OTHER] if OTHER not in names else list(names))
        self._classifier.meanings = meanings
        self.buckets = self._classifier.buckets

    async def run(self) -> MailRun:
        """Fetch, sort, and remember where we stopped. Never raises: a mailbox that is down
        becomes one line in the digest, and the next run picks up from the same place."""
        self._refresh_buckets()
        uid, validity = self._state.read()
        try:
            messages, newest, now_validity = await asyncio.to_thread(
                self._box.fetch_since, uid, FIRST_RUN_HOURS, self._max)
        except MailboxError as e:
            log.warning("mailbox unreachable: %s", e)
            return MailRun(error=str(e))
        if validity and now_validity and validity != now_validity:
            log.info("the mailbox was renumbered; starting from the last %d hours",
                     FIRST_RUN_HOURS)
            try:
                messages, newest, now_validity = await asyncio.to_thread(
                    self._box.fetch_since, None, FIRST_RUN_HOURS, self._max)
            except MailboxError as e:
                return MailRun(error=str(e))
        if not messages:
            self._state.write(newest, now_validity)
            log.info("mail: nothing new")
            return MailRun()
        sorted_, prompt_tokens, output_tokens = await self._classifier.sort(messages)
        if not sorted_:
            # Nothing came back for mail that is there: a local model whose Ollama is not
            # running. The bookmark stays put, so the next run fetches this mail again; moving
            # it on would leave the mail out of every digest.
            error = getattr(self._classifier, "last_error", "") or "no answer"
            log.warning("mail: the classifier answered nothing (%s); kept for the next run",
                        error)
            return MailRun(error=error)
        self._state.write(newest, now_validity)
        log.info("mail: %d message(s), buckets %s", len(sorted_),
                 ", ".join(sorted({s.bucket for s in sorted_})))
        run = MailRun(sorted=sorted_, prompt_tokens=prompt_tokens,
                      output_tokens=output_tokens, validity=now_validity)
        return run

    def remember(self, run: MailRun) -> str | None:
        """Record which letters this digest covered; the id goes in its button. None when
        there is nothing to mark: no mail, a failed run, or no record kept."""
        if self.marks is None or not run.sorted:
            return None
        return self.marks.add([s.message.uid for s in run.sorted], run.validity)

    async def mark(self, digest_id: str, *, seen: bool) -> tuple[bool, str]:
        """Mark every letter of one digest read (or unread again), and what to tell the
        user. Only that digest's letters: a later digest has its own button."""
        found = self.marks.get(digest_id) if self.marks is not None else None
        if found is None:
            return False, texts.MAIL_MARK_GONE
        uids, validity = found
        try:
            n = await asyncio.to_thread(self._box.set_seen, uids, validity, seen)
        except MailboxError as e:
            log.warning("could not mark a digest's mail: %s", e)
            return False, texts.MAIL_MARK_FAILED.format(error=e)
        log.info("mail: %d letter(s) of digest %s marked %s", n, digest_id,
                 "read" if seen else "unread")
        return True, (texts.MAIL_MARKED if seen else texts.MAIL_UNMARKED).format(n=n)

    async def compare(self, run: MailRun) -> AsyncIterator[ShadowRun]:
        """The same mail through each local model, yielded as each one finishes.

        Called only after the real digest has gone out, so that digest never waits for an
        experiment: a local model that hangs would otherwise hold it for the whole request
        timeout. One model at a time — each unloads before the next loads. The messages are the
        ones the real run sorted, in its order: `gate` never drops one.
        """
        if not run.sorted:
            return
        messages = messages_of(run)
        for shadow in self._shadows:
            yield await self._run_shadow(shadow, messages)

    async def _run_shadow(self, shadow, messages: list[Message]) -> ShadowRun:
        """The same mail through one local model, for the user to compare against.

        It runs after the state is written and its failures stay inside it: a model that is not
        running, or is too slow, must cost the real digest — and the other models — nothing.
        """
        result = ShadowRun(model=getattr(shadow, "model", "?"))
        shadow.buckets = list(self._classifier.buckets)
        shadow.meanings = dict(self._classifier.meanings)
        start = time.monotonic()
        try:
            result.sorted, _, _ = await shadow.sort(messages)
        except Exception as e:  # a local model fails in ways the Claude path cannot
            result.error = f"{type(e).__name__}: {e}"
            log.warning("the shadow classifier %s failed: %s", result.model, result.error)
        if not result.sorted and not result.error:
            result.error = getattr(shadow, "last_error", "") or "no answer"
        result.ms = int((time.monotonic() - start) * 1000)
        log.info("mail shadow (%s): %d message(s) in %d ms%s", result.model,
                 len(result.sorted), result.ms, f" — {result.error}" if result.error else "")
        return result

    async def digest(self) -> str:
        return digest(await self.run(), self.buckets)


def messages_of(run: MailRun) -> list[Message]:
    return [s.message for s in run.sorted]
