"""Reminders in the bot's SQLite database (migration 0002), so a restart loses none.

Its own connection to the same file as the audit store: the scheduler reads it from its own
loop, the vault pipeline writes it from a worker thread. Times go in as aware datetimes and are
kept as UTC ISO strings, which sort as times."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Reminder:
    id: int
    chat_id: int
    user_id: int
    text: str
    due_at: datetime
    # The first time it was due (aware, UTC): a repeating reminder counts its occurrences
    # from here, on the wall clock of the user's time zone.
    start_at: datetime
    repeat: str = ""
    event_id: int | None = None


def _row(row: sqlite3.Row) -> Reminder:
    return Reminder(id=row["id"], chat_id=row["chat_id"], user_id=row["user_id"],
                    text=row["text"], due_at=_time(row["due_at"]),
                    start_at=_time(row["start_at"]), repeat=row["repeat"] or "",
                    event_id=row["event_id"])


class ReminderStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(self, chat_id: int, user_id: int, text: str, due_at: datetime, *,
            repeat: str = "", event_id: int | None = None,
            now: datetime | None = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO reminders (chat_id, user_id, event_id, text, due_at, start_at, "
                "repeat, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, user_id, event_id, text, _iso(due_at), _iso(due_at), repeat,
                 _iso(now or datetime.now(UTC))))
            return int(cur.lastrowid)

    def get(self, reminder_id: int) -> Reminder | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reminders WHERE id = ?",
                                     (reminder_id,)).fetchone()
        return _row(row) if row else None

    def pending(self, chat_id: int | None = None) -> list[Reminder]:
        """Not sent, not cancelled; soonest first. All chats when `chat_id` is None."""
        query = "SELECT * FROM reminders WHERE cancelled = 0 AND sent_at IS NULL"
        args: tuple = ()
        if chat_id is not None:
            query += " AND chat_id = ?"
            args = (chat_id,)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY due_at, id", args).fetchall()
        return [_row(r) for r in rows]

    def due(self, now: datetime) -> list[Reminder]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE cancelled = 0 AND sent_at IS NULL "
                "AND due_at <= ? ORDER BY due_at, id", (_iso(now),)).fetchall()
        return [_row(r) for r in rows]

    def next_due(self) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(due_at) AS due FROM reminders WHERE cancelled = 0 "
                "AND sent_at IS NULL").fetchone()
        return _time(row["due"]) if row and row["due"] else None

    def sent(self, reminder_id: int, at: datetime, next_due: datetime | None = None) -> None:
        """A one-off is done; a repeating one moves to `next_due` and stays pending."""
        with self._lock, self._conn:
            if next_due is None:
                self._conn.execute("UPDATE reminders SET sent_at = ? WHERE id = ?",
                                   (_iso(at), reminder_id))
            else:
                self._conn.execute("UPDATE reminders SET due_at = ? WHERE id = ?",
                                   (_iso(next_due), reminder_id))

    def move_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        """A group that became a supergroup: its pending reminders go with it."""
        with self._lock, self._conn:
            self._conn.execute("UPDATE reminders SET chat_id = ? WHERE chat_id = ? "
                               "AND cancelled = 0 AND sent_at IS NULL",
                               (new_chat_id, old_chat_id))

    def cancel(self, reminder_id: int) -> bool:
        """True when it was pending and is now cancelled."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE reminders SET cancelled = 1 WHERE id = ? AND cancelled = 0 "
                "AND sent_at IS NULL", (reminder_id,))
            return cur.rowcount == 1

    def fired(self, rule_id: str, key: str, fire_at: datetime) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM reminder_fired WHERE rule_id = ? AND item_key = ? "
                "AND fire_at = ?", (rule_id, key, _iso(fire_at))).fetchone()
        return row is not None

    def mark_fired(self, rule_id: str, key: str, fire_at: datetime, at: datetime) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO reminder_fired (rule_id, item_key, fire_at, sent_at) "
                "VALUES (?, ?, ?, ?)", (rule_id, key, _iso(fire_at), _iso(at)))
