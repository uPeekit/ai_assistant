"""The message the bot sends on its own: the morning agenda.

One asyncio task that sleeps until the next occurrence of a wall-clock time in the user's own
time zone, sends, and sleeps again. Time zones and daylight saving are handled by computing the
next run from the current local time every round rather than by adding 24 hours to the last
one."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# How far past the scheduled minute a run may still fire (a laptop waking from sleep, a bot
# restarted at 09:04). Later than this, the digest waits for tomorrow — nobody wants yesterday's
# agenda at six in the evening.
GRACE = timedelta(hours=2)
# The shortest wait worth handing to asyncio: anything below a clock tick may return at once,
# which would spin the loop until the tick passes. Sending 50 ms late costs nothing.
MIN_SLEEP_S = 0.05
# How long a wait goes before the schedule is read again: a time moved on the admin page is
# honoured within this, instead of after the old time has fired.
RECHECK_S = 60.0


class LastRun:
    """When a message last went out, kept in a small JSON file under one key per message, so
    a restart inside the grace period does not send the morning agenda a second time."""

    def __init__(self, path: Path, key: str) -> None:
        self._path = path
        self._key = key

    def _all(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self) -> datetime | None:
        value = self._all().get(self._key)
        try:
            when = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None
        # A time without an offset (a hand-edited file) cannot be compared with the aware
        # schedule: treated as never run rather than killing it.
        return when if when is not None and when.tzinfo is not None else None

    def set(self, when: datetime) -> None:
        data = {**self._all(), self._key: when.isoformat()}
        try:
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError as e:  # a digest that cannot be remembered is still sent
            log.warning("could not remember the last %s run: %s", self._key, e)


def parse_at(value: str) -> time | None:
    """"09:00" -> time(9, 0). Empty or malformed: no schedule at all, and a warning."""
    text = value.strip()
    if not text:
        return None
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        return time(hour=hour, minute=minute)
    except (ValueError, TypeError):
        log.warning("bad daily time %r: expected HH:MM", value)
        return None


def parse_times(value: str) -> tuple[time, ...]:
    """"12:00,19:00" -> two times a day, in order. Bad entries are dropped with a warning."""
    found = [parse_at(part) for part in value.split(",")]
    return tuple(sorted({t for t in found if t is not None}))


class DailyMessage:
    """Calls `send()` once a day at `at`, in `tz`. `send` decides whether there is anything
    worth sending; this class only decides when."""

    def __init__(self, send: Callable[[], Awaitable[object]],
                 at: time | tuple[time, ...] | Callable[[], tuple[time, ...]],
                 tz: str, *, now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
                 remember: LastRun | None = None) -> None:
        self._send = send
        self._sleep = sleep
        self._remember = remember
        # A callable is re-read before every wait, so changing the times on the admin page
        # takes effect without a restart.
        if callable(at):
            self._source = at
        else:
            fixed = (at,) if isinstance(at, time) else tuple(sorted(at))
            self._source = lambda: fixed
        self._zone = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(self._zone))
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    @property
    def _times(self) -> tuple[time, ...]:
        return tuple(sorted(self._source())) or (time(hour=9),)

    def next_run(self, after: datetime) -> datetime:
        """The next moment the message is due, strictly after `after` — the earliest of the
        configured times today, or the first one tomorrow."""
        local = after.astimezone(self._zone)
        candidates = [local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                      for t in self._times]
        later = [c for c in candidates if c > after]
        return min(later) if later else min(candidates) + timedelta(days=1)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        # A bot started shortly after the time still sends today's (GRACE), so a restart at
        # 09:05 does not silently skip the day.
        now = self._now()
        # The most recent time that has already passed today (or yesterday's last one).
        passed = [now.astimezone(self._zone).replace(hour=t.hour, minute=t.minute, second=0,
                                                      microsecond=0) for t in self._times]
        previous = max([p for p in passed if p <= now], default=min(passed) - timedelta(days=1))
        if now - previous < GRACE and not self._sent_already(previous):
            await self._fire(previous)
        while True:
            await self._fire(await self._wait())

    def _sent_already(self, due: datetime) -> bool:
        """Did this run go out before the restart? Without the memory a bot updated at 09:30
        sent the morning agenda again."""
        last = self._remember.get() if self._remember is not None else None
        return last is not None and last >= due

    async def _wait(self) -> datetime:
        """Sleep until the next due time, and return it.

        The schedule is read again every RECHECK_S, from the moment the wait began: a time
        moved on the admin page is honoured then, not after the old one has fired. And the
        wall clock has to have really reached the time: asyncio's timers run on the monotonic
        clock and may fire up to a clock tick early — about 16 ms on Windows. Waking at
        08:59:59.985 and then asking for the next run gave 09:00 *today* again, and the
        morning digest went out twice, a second apart."""
        start = self._now()
        while True:
            due = self.next_run(start)
            left = (due - self._now()).total_seconds()
            if left <= 0:
                if self._now() - due < GRACE:
                    return due
                start = self._now()  # moved to a time long past: that one is not sent
                continue
            await self._sleep(min(max(left, MIN_SLEEP_S), RECHECK_S))

    async def _fire(self, due: datetime) -> None:
        try:
            await self._send()
        except Exception:  # a failed digest must never stop the schedule
            log.exception("daily message failed")
            return
        if self._remember is not None:
            self._remember.set(due)
