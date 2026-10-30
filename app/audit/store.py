from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.audit.migrate import MigrationError, apply, discover, pending

EVENT_COLUMNS = frozenset(
    {"message_id", "kind", "raw_input", "transcription", "llm_model", "llm_context",
     "llm_response", "interpretation", "candidate_scores", "validation_result", "decision",
     "clarification_state", "command", "executed", "notion_page_id", "error", "duration_ms",
     "telegram_user_id", "chat_id"}
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self) -> None:
        with self._lock:
            apply(self._path)

    def assert_schema_current(self) -> None:
        with self._lock:
            todo = pending(self._conn, discover())
        if todo:
            versions = ", ".join(f"{m.version:04d}" for m in todo)
            raise MigrationError(f"database schema out of date; pending migrations: {versions}. "
                                 "Run: python -m tools.migrate --apply")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # events
    def new_event(self, *, telegram_user_id: int, chat_id: int, kind: str, **cols) -> int:
        with self._lock:
            cols.update(telegram_user_id=telegram_user_id, chat_id=chat_id, kind=kind)
            self._check_cols(cols)
            cols["ts"] = _iso(datetime.now(UTC))
            keys = ", ".join(cols)
            marks = ", ".join("?" for _ in cols)
            cur = self._conn.execute(f"INSERT INTO events ({keys}) VALUES ({marks})",
                                     list(cols.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    def update_event(self, event_id: int, **cols) -> None:
        with self._lock:
            self._check_cols(cols)
            if not cols:
                return
            sets = ", ".join(f"{k} = ?" for k in cols)
            self._conn.execute(f"UPDATE events SET {sets} WHERE id = ?",
                               [*cols.values(), event_id])
            self._conn.commit()

    def get_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?",
                                     (event_id,)).fetchone()
            return dict(row) if row else None

    # executions
    def add_execution(
        self, event_id: int, chat_id: int, reply_message_id: int | None, undo: str,
        expires_at: datetime,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO executions (event_id, chat_id, reply_message_id, undo, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, chat_id, reply_message_id, undo, _iso(expires_at)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_execution(self, execution_id: int, now: datetime) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE id = ? AND expires_at > ?",
                (execution_id, _iso(now)),
            ).fetchone()
            return dict(row) if row else None

    def execution_by_reply(self, chat_id: int, reply_message_id: int) -> dict | None:
        """The write whose reply is this Telegram message, expired or not: the caller says
        "too late" for an expired one, rather than treating the reply as a new message."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE chat_id = ? AND reply_message_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_id, reply_message_id),
            ).fetchone()
            return dict(row) if row else None

    def latest_execution(self, chat_id: int, now: datetime,
                         user_id: int | None = None) -> dict | None:
        """The chat's newest write that can still be undone — with `user_id`, the newest of
        that sender's (a group chat's /undo)."""
        with self._lock:
            if user_id is None:
                row = self._conn.execute(
                    "SELECT * FROM executions WHERE chat_id = ? AND undone = 0 "
                    "AND expires_at > ? ORDER BY id DESC LIMIT 1",
                    (chat_id, _iso(now)),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT x.* FROM executions x JOIN events e ON e.id = x.event_id "
                    "WHERE x.chat_id = ? AND e.telegram_user_id = ? AND x.undone = 0 "
                    "AND x.expires_at > ? ORDER BY x.id DESC LIMIT 1",
                    (chat_id, user_id, _iso(now)),
                ).fetchone()
            return dict(row) if row else None

    def set_reply_message_id(self, execution_id: int, reply_message_id: int) -> None:
        """Fill in the Telegram message id of the reply that carried the Undo button. It cannot
        be set at insert time — the row is written while the command runs, before the reply
        exists — so the transport (Plan 3b) records it afterwards and uses it to edit that same
        message when the undo window closes. Without this the column could never be filled."""
        with self._lock:
            self._conn.execute(
                "UPDATE executions SET reply_message_id = ? WHERE id = ?",
                (reply_message_id, execution_id),
            )
            self._conn.commit()

    def mark_undone(self, execution_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE executions SET undone = 1 WHERE id = ?", (execution_id,))
            self._conn.commit()

    @staticmethod
    def _check_cols(cols: dict) -> None:
        bad = set(cols) - EVENT_COLUMNS
        if bad:
            raise ValueError(f"unknown event columns: {sorted(bad)}")
