"""Why Claude could not be used, said once in plain words.

Every Claude call in the bot fails the same way when the API refuses: a short technical string
in the log, and an answer that is quietly worse — the interpreter falls back to the local
model, the Obsidian side writes nothing at all. Nothing told the user that the account simply
needed topping up.

This module names the reason (no credit, a rejected key, a rate limit, an outage), keeps it
until a call succeeds again, and hands out one sentence about it at most once an hour. One
`Health` instance is shared by every component that talks to Anthropic, so whichever call fails
first is the one that explains it."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import anthropic

from app import texts

log = logging.getLogger(__name__)

# The reasons, in the order they matter. "" means the call failed for a reason of our own
# making (a malformed request, an unknown model) — not something the user can act on.
CREDIT = "credit"
KEY = "key"
LIMIT = "limit"
DOWN = "down"

# How long between two showings of the same warning. An hour: often enough that a user who
# missed the first one still learns why the answers got worse, rare enough not to nag.
REMIND_S = 3600.0
# A failure older than this is checked before it is reported again. The state is only ever
# cleared by a call that goes through, and a reply that makes no call (an undo, a digest
# with nothing to sort) would otherwise keep repeating a failure the user has since fixed.
STALE_S = 300.0

_CREDIT_MARKERS = ("credit balance", "billing", "insufficient_quota", "insufficient quota",
                   "purchase credits")


def detail(e: anthropic.APIError) -> str:
    """The API's own error message, or "" — never the request, which carries the key."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        message = body.get("error", {}).get("message")
        if isinstance(message, str):
            return message
    message = getattr(e, "message", None)
    return message if isinstance(message, str) else ""


def describe(e: anthropic.APIError) -> str:
    """Status and message, short enough for a log line and an audit row."""
    if isinstance(e, anthropic.APITimeoutError):
        return "claude timed out"
    if isinstance(e, anthropic.APIConnectionError):
        return "claude unreachable"
    status = getattr(e, "status_code", None)
    return f"claude {status}: {(detail(e) or type(e).__name__)[:300]}"


def reason(e: anthropic.APIError) -> str:
    """Which kind of failure this is, as one of the constants above.

    A 400 is normally our own mistake, so it is not reported as a service problem — except the
    one 400 that is: an account out of credit is refused as a bad request."""
    if isinstance(e, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return DOWN
    status = getattr(e, "status_code", None)
    text = detail(e).casefold()
    if status in (400, 402) and any(m in text for m in _CREDIT_MARKERS):
        return CREDIT
    if status == 402:
        return CREDIT
    if status in (401, 403):
        return KEY
    if status == 429:
        return LIMIT
    if isinstance(status, int) and status >= 500:
        return DOWN
    return ""


class Health:
    """Whether Claude is usable right now, and whether the user has been told."""

    def __init__(self, *, remind_s: float = REMIND_S,
                 clock: Callable[[], float] = time.monotonic,
                 probe: Callable[[], Awaitable[object]] | None = None,
                 stale_s: float = STALE_S) -> None:
        self._remind_s = remind_s
        self._clock = clock
        # The smallest real call (main.py: one token from the cheapest model). Raises
        # anthropic.APIError when Claude still refuses.
        self._probe = probe
        self._stale_s = stale_s
        self._reason = ""
        self._told_at: float | None = None
        self._failed_at: float | None = None

    @property
    def reason(self) -> str:
        return self._reason

    def record(self, e: anthropic.APIError) -> str:
        """Remember this failure and return its reason code (see `reason`)."""
        why = reason(e)
        if why and why != self._reason:
            log.warning("claude is down (%s): %s", why, describe(e))
            self._reason = why
            self._told_at = None  # a new reason is worth saying even if the old one was just said
        if why:
            self._failed_at = self._clock()
        return why

    def ok(self) -> None:
        """A call went through: whatever was wrong is over."""
        if self._reason:
            log.info("claude answers again (was: %s)", self._reason)
        self._reason = ""
        self._told_at = None
        self._failed_at = None

    def short(self) -> str:
        """A few words for a line that is already about a failure, or ""."""
        return texts.LLM_DOWN_SHORT.get(self._reason, "")

    async def checked_note(self) -> str:
        """`note()`, but a failure older than STALE_S is checked with one real call first.

        The 12:00 mail digest found the account out of credit, which was true; the user
        topped up, and at 16:33 an /undo — which calls no model — was told there was no
        credit. Only a call that goes through clears the state, and none had been made. The
        check runs only when a warning is about to be given, so at most once an hour."""
        if not self._reason:
            return ""
        now = self._clock()
        due = self._told_at is None or now - self._told_at >= self._remind_s
        stale = self._failed_at is None or now - self._failed_at >= self._stale_s
        if due and stale and self._probe is not None:
            try:
                await self._probe()
            except anthropic.APIError as e:
                self.record(e)
                self._failed_at = self._clock()
            except Exception as e:  # a check that cannot run is no news either way
                log.info("could not check whether claude answers again: %s",
                         type(e).__name__)
            else:
                self.ok()
                return ""
        return self.note()

    def note(self) -> str:
        """The sentence to append to a reply, or "" when there is nothing to say or it has been
        said within the last hour. Asking marks it as said."""
        if not self._reason:
            return ""
        now = self._clock()
        if self._told_at is not None and now - self._told_at < self._remind_s:
            return ""
        self._told_at = now
        return texts.LLM_DOWN_NOTE.get(self._reason, "")
