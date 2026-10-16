"""The last messages of a listed group that were not addressed to the bot, in memory only.

When someone in the group writes "@bot save this" meaning a message a member sent a minute
ago, the bot has to know that message — and Telegram gives a bot no way to fetch a chat's
history. So for each listed group the bot keeps the last few messages it saw go by: the sender's
first name and the text, for two hours. Never written to disk, never logged, gone on restart;
handed to a model only when a message points back at something said before."""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

from app.pointing import points_back  # noqa: F401 (re-exported)

MAX_KEPT = 20
WINDOW = timedelta(hours=2)
MAX_TEXT = 1000


class RecentMessages:
    def __init__(self, now=datetime.now) -> None:
        self._now = now
        self._chats: dict[int, deque] = {}
        self._lock = threading.Lock()

    def add(self, chat_id: int, sender: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            ring = self._chats.setdefault(chat_id, deque(maxlen=MAX_KEPT))
            ring.append((self._now(), sender.strip() or "?", text[:MAX_TEXT]))

    def recent(self, chat_id: int) -> list[tuple[str, str]]:
        """(sender, text) of the chat's kept messages within the window, oldest first."""
        since = self._now() - WINDOW
        with self._lock:
            ring = self._chats.get(chat_id)
            if ring is None:
                return []
            while ring and ring[0][0] < since:
                ring.popleft()
            return [(sender, text) for _, sender, text in ring]
