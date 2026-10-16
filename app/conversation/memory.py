"""What each chat said lately, so a follow-up ("and in Paris?") is understood.

The orchestrator writes the user's message as it was handled and the reply the user saw; a turn
reads the chat's last exchanges of the last two hours, oldest first. Rows older than that are
deleted when the chat is read. One table in the bot's database (migration 0003): a restart
keeps the thread; a group's memory is its own, never the owner's private chat's."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

WINDOW = timedelta(hours=2)
MAX_EXCHANGES = 10
MAX_TEXT = 2000  # a long answer is remembered by its start: enough to know what it was about
# The user's message, the bot's whole reply, and — next to the reply — the answer to a
# question in it, as words alone.
USER, BOT, ANSWER = "user", "bot", "answer"


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


class ChatMemory:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(self, chat_id: int, user_id: int, role: str, text: str, at: datetime) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO chat_memory (chat_id, user_id, role, text, at) VALUES "
                "(?, ?, ?, ?, ?)", (chat_id, user_id, role, text[:MAX_TEXT], _iso(at)))
            # Every chat's old rows, not only this one's: a chat gone quiet is never read again.
            self._conn.execute("DELETE FROM chat_memory WHERE at < ?", (_iso(at - WINDOW),))

    def recent(self, chat_id: int, now: datetime) -> list[tuple[str, str]]:
        """(role, text) of the chat's last exchanges within the window, oldest first."""
        since = _iso(now - WINDOW)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chat_memory WHERE chat_id = ? AND at < ?",
                               (chat_id, since))
            rows = self._conn.execute(
                "SELECT id, role, text FROM chat_memory WHERE chat_id = ? AND at >= ? "
                "ORDER BY id DESC LIMIT ?", (chat_id, since, 3 * MAX_EXCHANGES)).fetchall()
        # The last MAX_EXCHANGES messages and replies, and the answers among them: an answer
        # row sits next to its reply and must not push the conversation out.
        said = [r for r in rows if r[1] != ANSWER][:2 * MAX_EXCHANGES]
        first = said[-1][0] if said else 0
        kept = [r for r in rows if r[1] != ANSWER and r in said or r[1] == ANSWER and r[0] >= first]
        return [(role, text) for _, role, text in reversed(kept)]

    def last_answer(self, chat_id: int, now: datetime) -> str:
        """The bot's latest reply in the window, whole as remembered: what "save this" saves."""
        for role, text in reversed(self.recent(chat_id, now)):
            if role == BOT:
                return text
        return ""
