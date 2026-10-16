"""The loop that sends reminders when they are due, and runs the reminder rules.

It sleeps until the soonest pending reminder, at most a minute at a time, so one set a moment
ago is not missed for long. Whatever came due while the bot was off is sent on the first pass,
saying how late it is; a repeating one missed several times is sent once and moves on to its
next occurrence after now. A send that fails for a passing reason is tried again on the next
pass; a chat Telegram refuses for good (the bot was removed) cancels the reminder."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import texts
from app.reminders import repeat
from app.reminders.store import ReminderStore

log = logging.getLogger(__name__)

MAX_SLEEP_S = 60.0
MIN_SLEEP_S = 0.05
LATE = timedelta(minutes=2)  # later than this, the message says how late it is
RULES_EVERY = timedelta(minutes=10)


RETRY_FIRST_S = 30.0
RETRY_MAX_S = 600.0


class ChatGone(Exception):
    """The chat will never take a message from the bot again (removed, blocked, deleted)."""


class ChatMoved(Exception):
    """A group became a supergroup and has a new id: its reminders follow it."""

    def __init__(self, new_chat_id: int) -> None:
        super().__init__(new_chat_id)
        self.new_chat_id = new_chat_id


def late_by(delay: timedelta) -> str:
    minutes = int(delay.total_seconds() // 60)
    if minutes < 60:
        return texts.REMINDER_MINUTES.format(n=minutes)
    if minutes < 48 * 60:
        return texts.REMINDER_HOURS.format(n=minutes // 60)
    return texts.REMINDER_DAYS.format(n=minutes // (24 * 60))


class ReminderScheduler:
    def __init__(self, store: ReminderStore, send: Callable[[int, str], Awaitable[object]],
                 tz: str, *, rules: Callable[[datetime], Awaitable[object]] | None = None,
                 now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep) -> None:
        self._store = store
        self._send = send
        self._zone = ZoneInfo(tz)
        self._rules = rules
        self._now = now or (lambda: datetime.now(self._zone))
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._rules_at: datetime | None = None
        # Reminders whose send failed: id -> (when to try again, the wait that led there). A
        # pending reminder with a past due time would otherwise make the loop retry at once,
        # twenty times a second, for as long as the network is down.
        self._retry: dict[int, tuple[datetime, float]] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def run_once(self, now: datetime) -> int:
        """Send everything due by `now`; returns how many went out."""
        sent = 0
        for reminder in await asyncio.to_thread(self._store.due, now):
            retry = self._retry.get(reminder.id)
            if retry is not None and retry[0] > now:
                continue
            delay = now - reminder.due_at
            text = (texts.REMINDER_LATE.format(delay=late_by(delay), text=reminder.text)
                    if delay > LATE else texts.REMINDER.format(text=reminder.text))
            try:
                await self._send(reminder.chat_id, text)
            except ChatGone:
                log.warning("reminder %s: chat %s is gone; cancelled", reminder.id,
                            reminder.chat_id)
                await asyncio.to_thread(self._store.cancel, reminder.id)
                self._retry.pop(reminder.id, None)
                continue
            except ChatMoved as moved:
                log.info("reminder %s: chat %s moved to %s", reminder.id, reminder.chat_id,
                         moved.new_chat_id)
                await asyncio.to_thread(self._store.move_chat, reminder.chat_id,
                                        moved.new_chat_id)
                continue
            except Exception as e:  # a network hiccup: tried again later, waiting longer
                wait = min(retry[1] * 2, RETRY_MAX_S) if retry else RETRY_FIRST_S
                self._retry[reminder.id] = (now + timedelta(seconds=wait), wait)
                log.warning("reminder %s not sent (%s); next try in %.0f s", reminder.id,
                            type(e).__name__, wait)
                continue
            self._retry.pop(reminder.id, None)
            following = None
            if reminder.repeat:
                following = repeat.next_after(reminder.repeat,
                                              reminder.start_at.astimezone(self._zone), now)
            await asyncio.to_thread(self._store.sent, reminder.id, now, following)
            sent += 1
        return sent

    async def _rules_pass(self, now: datetime) -> None:
        if self._rules is None:
            return
        if self._rules_at is not None and now - self._rules_at < RULES_EVERY:
            return
        self._rules_at = now
        try:
            await self._rules(now)
        except Exception:  # a broken rule must never stop the reminders
            log.exception("reminder rules failed")

    def _wait(self, soonest: datetime | None) -> float:
        """Seconds until there is something to do: the soonest reminder not waiting for a
        retry, or the soonest retry, or a minute."""
        now = self._now()
        wait = MAX_SLEEP_S
        if soonest is not None and soonest > now:
            wait = min(wait, (soonest - now).total_seconds())
        for at, _ in self._retry.values():
            wait = min(wait, max((at - now).total_seconds(), MIN_SLEEP_S))
        return max(wait, MIN_SLEEP_S)

    async def _loop(self) -> None:
        while True:
            # Nothing may end the loop: one locked database read would otherwise stop every
            # reminder until a restart.
            try:
                now = self._now()
                await self.run_once(now)
                await self._rules_pass(now)
                soonest = await asyncio.to_thread(self._store.next_due)
                wait = self._wait(soonest)
            except Exception:
                log.exception("reminder pass failed")
                wait = MAX_SLEEP_S
            await self._sleep(wait)
