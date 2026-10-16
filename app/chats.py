"""The chats the bot answers in besides its owners' private chats, each with its own scope.

An owner (TELEGRAM_ALLOWED_USER_IDS) has full access in their private chat and is not listed
here. A group is reached only through its own entry: the scope belongs to the chat, whoever
writes in it — the owner included. A group the bot sees that is not listed is remembered as
"seen", so the admin page can offer it instead of the user hunting for its id.

`data/chats.json`, edited on the admin page:

    {"chats": [{"chat_id": -100123, "title": "Book club", "enabled": true, "full": false,
                "rights": ["ask", "add", "tasks"], "tag": "club", "folder": "Club",
                "instruction": "..."}],
     "seen": [{"chat_id": -100456, "title": "...", "at": "2026-10-15T20:00:00"}]}

Read per message (by mtime), like the switches: a change applies to the next message."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ask:    questions about what is in scope
# add:    new notes in the home folder, and appending to notes in scope
# tasks:  new tasks (always carrying the chat's tag)
# tick:   marking tasks done
# change: properties and rewrites of notes in scope
# move:   moving lines between notes in scope
# remind: set, list and cancel reminders (app/reminders/)
RIGHTS = ("ask", "add", "tasks", "tick", "change", "move", "remind")
MAX_SEEN = 20
MAX_TEXT = 2000


@dataclass(frozen=True)
class ChatScope:
    chat_id: int
    title: str = ""
    enabled: bool = True
    # Everything an owner's private chat can do, over the whole vault: no tag, all rights.
    full: bool = False
    rights: frozenset[str] = field(default_factory=frozenset)
    tag: str = ""
    folder: str = ""
    # What the chat's readers are told instead of the owner's guide (_bot.md).
    instruction: str = ""

    def may(self, right: str) -> bool:
        return self.full or right in self.rights


@dataclass(frozen=True)
class ChatRef:
    """Who a turn is from, for what a turn keeps per chat (reminders)."""

    chat_id: int
    user_id: int
    event_id: int | None = None
    # An allowed user (TELEGRAM_ALLOWED_USER_IDS): may cancel anyone's reminder in a group.
    owner: bool = True


def _scope(raw: dict) -> ChatScope | None:
    try:
        chat_id = int(raw["chat_id"])
    except (KeyError, TypeError, ValueError):
        return None
    rights = raw.get("rights") or []
    return ChatScope(
        chat_id=chat_id,
        title=str(raw.get("title", "")).strip()[:200],
        enabled=bool(raw.get("enabled", True)),
        full=bool(raw.get("full", False)),
        rights=frozenset(r for r in rights if r in RIGHTS) if isinstance(rights, list)
        else frozenset(),
        tag=str(raw.get("tag", "")).strip().lstrip("#"),
        folder=str(raw.get("folder", "")).strip().strip("/"),
        instruction=str(raw.get("instruction", ""))[:MAX_TEXT],
    )


def _dump(scope: ChatScope) -> dict:
    return {"chat_id": scope.chat_id, "title": scope.title, "enabled": scope.enabled,
            "full": scope.full, "rights": [r for r in RIGHTS if r in scope.rights],
            "tag": scope.tag, "folder": scope.folder, "instruction": scope.instruction}


class Chats:
    def __init__(self, path: Path, *, now=datetime.now) -> None:
        self._path = path
        self._now = now
        self._chats: dict[int, ChatScope] = {}
        self._seen: list[dict] = []
        self._mtime: float | None = None
        self._lock = threading.Lock()  # the admin page saves from its own thread
        self.reload()

    def reload(self) -> None:
        """Re-read the file if it changed. Unreadable or nonsense: no listed chats — only the
        owners' private chats work — said once."""
        with self._lock:
            try:
                stat = self._path.stat()
            except OSError:
                self._chats, self._seen, self._mtime = {}, [], None
                return
            if self._mtime == stat.st_mtime:
                return
            self._mtime = stat.st_mtime
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError) as e:
                log.warning("could not read %s (%s); no group chats until it is fixed",
                            self._path, e)
                self._chats, self._seen = {}, []
                return
            scopes = [_scope(c) for c in raw.get("chats") or [] if isinstance(c, dict)]
            self._chats = {s.chat_id: s for s in scopes if s is not None}
            self._seen = [s for s in raw.get("seen") or []
                          if isinstance(s, dict) and isinstance(s.get("chat_id"), int)]

    def get(self, chat_id: int) -> ChatScope | None:
        """The chat's scope when it is listed and switched on."""
        self.reload()
        scope = self._chats.get(chat_id)
        if scope is None or not scope.enabled:
            return None
        # A group just added on the page has no tag yet: until it has one, it is not answered —
        # an empty tag would write "#" tasks into the owner's list and match stray "#" lines.
        return scope if scope.full or scope.tag else None

    def all(self) -> dict:
        self.reload()
        return {"chats": [_dump(s) for s in self._chats.values()],
                "seen": [s for s in self._seen if s["chat_id"] not in self._chats]}

    def _write(self, chats: dict[int, ChatScope], seen: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({"chats": [_dump(s) for s in chats.values()], "seen": seen},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path)
        self._chats, self._seen = chats, seen
        self._mtime = self._path.stat().st_mtime

    def save(self, payload: object) -> int:
        """The chats the page posted (a list of objects, each a whole chat); returns how many
        changed. Raises ValueError for a payload that is not a list of chats."""
        if not isinstance(payload, list) or not all(isinstance(c, dict) for c in payload):
            raise ValueError("'chats' must be a list of objects")
        scopes = [_scope(c) for c in payload]
        if any(s is None for s in scopes):
            raise ValueError("every chat needs a numeric chat_id")
        self.reload()
        wanted = {s.chat_id: s for s in scopes}
        changed = sum(1 for k in wanted.keys() | self._chats.keys()
                      if wanted.get(k) != self._chats.get(k))
        if changed:
            with self._lock:
                self._write(wanted, [s for s in self._seen if s["chat_id"] not in wanted])
            log.info("chats: %d listed (%d changed)", len(wanted), changed)
        return changed

    def saw(self, chat_id: int, title: str) -> None:
        """A group the bot is in but nobody listed: remembered for the admin page, newest
        first. Written only when it is new, so a busy group does not rewrite the file."""
        self.reload()
        if chat_id in self._chats or any(s["chat_id"] == chat_id for s in self._seen):
            return
        entry = {"chat_id": chat_id, "title": title.strip()[:200],
                 "at": self._now().isoformat(timespec="seconds")}
        with self._lock:
            self._write(dict(self._chats), [entry, *self._seen][:MAX_SEEN])
        log.info("an unlisted group was seen: chat_id=%s", chat_id)
