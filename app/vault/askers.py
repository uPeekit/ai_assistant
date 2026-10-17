"""Who is who in the owner's records: the name a person goes by there, as they told the bot.

A question about oneself ("what did I rate it") is answered from records that name people — a
ratings table, form responses — where the asker may be under a short form of their name or a
nickname — and not always the same one: a quiz may be under a joke name. When the answerer
cannot tell which entry is theirs, the bot asks, and the same person's next short message in
that chat is the answer. Every name they gave is kept per Telegram user in
`data/askers.json`, so each is asked for once, and the question they asked first is answered
then.

    {"431847137": ["Sam", "Captain Ahab"]}
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

WAIT_S = 600.0  # how long a question back waits for its answer
MAX_REPLY_WORDS = 4  # a longer message is a new message, not a name
MAX_NAME = 80
MAX_NAMES = 10


class Askers:
    def __init__(self, path: Path | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._names: dict[str, list[str]] = {}
        # (chat, user) -> (the question asked, when the bot asked back)
        self._waiting: dict[tuple[int, int], tuple[str, float]] = {}
        if path is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    names = [str(n).strip() for n in (v if isinstance(v, list) else [v])]
                    if any(names):
                        self._names[str(k)] = [n for n in names if n][:MAX_NAMES]
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError) as e:
                log.warning("could not read %s (%s); asking names again", path, e)

    def known(self, user_id: int) -> tuple[str, ...]:
        """The names this person said they go by in the records, the latest last."""
        return tuple(self._names.get(str(user_id), ()))

    def asked(self, chat_id: int, user_id: int, question: str) -> None:
        """The bot asked this person who they are in the records, about `question`."""
        self._waiting[(chat_id, user_id)] = (question, self._clock())

    def reply(self, chat_id: int, user_id: int, message: str) -> str | None:
        """The question asked first, when `message` answers the bot's question back — then the
        name is kept. None for any other message, and the question back is dropped."""
        waiting = self._waiting.pop((chat_id, user_id), None)
        if waiting is None:
            return None
        question, at = waiting
        name = message.strip().strip(".!«»\"'").strip()
        if (self._clock() - at > WAIT_S or not name or "?" in name
                or len(name.split()) > MAX_REPLY_WORDS):
            return None
        self._remember(user_id, name[:MAX_NAME])
        return question

    def _remember(self, user_id: int, name: str) -> None:
        with self._lock:
            names = [n for n in self._names.get(str(user_id), [])
                     if n.casefold() != name.casefold()]
            self._names[str(user_id)] = [*names, name][-MAX_NAMES:]
            if self._path is None:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_text(json.dumps(self._names, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                tmp.replace(self._path)
            except OSError as e:
                log.warning("could not keep a name in %s: %s", self._path, e)
        log.info("an asker's name in the records was kept (user %s)", user_id)
