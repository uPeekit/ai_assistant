"""The allowlist gate: checked before anything else touches an update — before the text is read,
before transcription, before any audit row exists (documentation/ERRORS.md AUTH_DENIED,
FLOWS.md F16). An unauthorised user gets no reply at all, only a WARNING log carrying ids and
nothing else — never message text, never a token."""

from __future__ import annotations

import logging
from collections.abc import Callable

from telegram import MessageEntity
from telegram.ext import filters

from app import address
from app.chats import Chats
from app.config import Settings
from app.media import instagram
from app.telegram.recent import RecentMessages

log = logging.getLogger(__name__)


def is_allowed(user_id: int | None, allowed: frozenset[int]) -> bool:
    """None (channel posts, edited service messages with no sender) is never allowed."""
    return user_id is not None and user_id in allowed


def deny(user_id: int | None, chat_id: int | None) -> None:
    """Logs the denial at WARNING with the ids only — no message text, no token."""
    log.warning("AUTH_DENIED user_id=%s chat_id=%s", user_id, chat_id)


class _AllowedUserFilter(filters.MessageFilter):
    """The declarative gate: a plain filters.User would reject a denied sender silently (its
    membership test just returns False), leaving text messages, voice notes and commands denied
    with no trace in the log at all — only app.telegram.handlers._guard_callback (the one
    handler PTB cannot gate this way) would ever call deny(). This wraps the same membership
    test and calls deny() on the way to returning False, so every declaratively-gated handler
    logs exactly like the callback path does, without moving the check into a handler body."""

    def __init__(self, allowed: frozenset[int]) -> None:
        super().__init__()
        self._allowed = allowed

    def filter(self, message) -> bool:
        user = message.from_user
        user_id = user.id if user else None
        if is_allowed(user_id, self._allowed):
            return True
        deny(user_id, getattr(message, "chat_id", None))
        return False


def allowed_filter(settings: Settings) -> filters.BaseFilter:
    """A python-telegram-bot filter built from Settings.allowed_user_ids, so an unauthorised
    update never reaches a handler callback. An empty allowlist denies everyone (is_allowed(_,
    frozenset()) is always False)."""
    return _AllowedUserFilter(settings.allowed_user_ids)


GROUPS = ("group", "supergroup")


def bot_mention(message) -> str:
    """"@botname" as this bot is called, or "" before the bot knows its own name."""
    username = getattr(message.get_bot(), "username", None) or ""
    return f"@{username}" if username else ""


def addressed(message, names: tuple[str, ...]) -> bool:
    """Is this group message meant for the bot? An @mention of it, a reply to one of its
    messages, a command, or its name at the start or the end. Voice has no words to look at:
    only a reply counts."""
    bot = message.get_bot()
    replied = message.reply_to_message
    if replied is not None and replied.from_user is not None and \
            replied.from_user.id == getattr(bot, "id", None):
        return True
    text = message.text or ""
    if not text:
        return False
    if any(e.type == MessageEntity.BOT_COMMAND and e.offset == 0
           for e in message.entities or ()):
        return True
    mention = bot_mention(message).casefold()
    for entity, value in message.parse_entities([MessageEntity.MENTION,
                                                 MessageEntity.TEXT_MENTION]).items():
        if mention and value.casefold() == mention:
            return True
        if entity.user is not None and entity.user.id == getattr(bot, "id", None):
            return True
    return address.strip(text, names).called


class _ChatGate(filters.MessageFilter):
    """Who the bot answers, and where. A private chat: an allowed user, as always. A group:
    only a group listed (and switched on) in data/chats.json, and only a message addressed to
    the bot — the rest of the conversation costs nothing and leaves no trace. An allowed user
    in an unlisted group is not let in: a group is reached only through its own entry, with
    its own scope. An unlisted group is remembered for the admin page and gets no reply."""

    def __init__(self, allowed: frozenset[int], chats: Chats | None,
                 names: Callable[[], tuple[str, ...]],
                 recent: RecentMessages | None = None) -> None:
        super().__init__()
        self._allowed = allowed
        self._chats = chats
        self._names = names
        self._recent = recent

    def filter(self, message) -> bool:
        user = message.from_user
        user_id = user.id if user else None
        chat = message.chat
        if chat is not None and chat.type in GROUPS:
            if message.sender_chat is not None:
                # An anonymous admin or a member posting as a channel: Telegram gives them all
                # one shared user id, so "only who asked may undo it" could not hold.
                return False
            scope = self._chats.get(chat.id) if self._chats is not None else None
            if scope is None:
                if self._chats is not None:
                    self._chats.saw(chat.id, chat.title or "")
                log.debug("group %s is not listed", chat.id)
                return False
            if user_id is not None and addressed(message, self._names()):
                return True
            if self._recent is not None and message.text and user_id is not None:
                # Not for the bot, but a later «save this» may mean it (app/telegram/recent.py).
                self._recent.add(chat.id, message.from_user.first_name or "", message.text)
            return False
        if chat is not None and chat.type != "private":
            return False  # channels: never
        if is_allowed(user_id, self._allowed):
            return True
        deny(user_id, getattr(message, "chat_id", None))
        return False


def chat_filter(settings: Settings, chats: Chats | None,
                names: Callable[[], tuple[str, ...]] = lambda: (),
                recent: RecentMessages | None = None) -> filters.BaseFilter:
    """allowed_filter, widened to the groups listed in data/chats.json."""
    return _ChatGate(settings.allowed_user_ids, chats, names, recent)


class _MediaGate(filters.MessageFilter):
    """An Instagram link in a listed group with the "media" right, from a member, not meant
    for the bot: the post goes back as a reply (app/telegram/media.py). A message addressed to
    the bot is a request, and its link is the request's business."""

    def __init__(self, chats: Chats | None, names: Callable[[], tuple[str, ...]]) -> None:
        super().__init__()
        self._chats = chats
        self._names = names

    def filter(self, message) -> bool:
        chat = message.chat
        if chat is None or chat.type not in GROUPS or self._chats is None:
            return False
        if message.from_user is None or message.sender_chat is not None:
            return False
        if not instagram.codes(message.text or ""):
            return False
        scope = self._chats.get(chat.id)
        if scope is None or not scope.may("media"):
            return False
        return not addressed(message, self._names())


def media_filter(chats: Chats | None,
                 names: Callable[[], tuple[str, ...]] = lambda: ()) -> filters.BaseFilter:
    return _MediaGate(chats, names)


class _BareLinks(filters.MessageFilter):
    """An allowed user's private message of nothing but Instagram links: "show me", not a
    request — the post goes back and the conversation never sees it. A stranger's falls
    through to the gate, which denies it as it denies anything else."""

    def __init__(self, allowed: frozenset[int]) -> None:
        super().__init__()
        self._allowed = allowed

    def filter(self, message) -> bool:
        chat = message.chat
        if chat is None or chat.type != "private" or message.from_user is None:
            return False
        return is_allowed(message.from_user.id, self._allowed) and \
            instagram.bare(message.text or "")


def private_links(settings: Settings) -> filters.BaseFilter:
    return _BareLinks(settings.allowed_user_ids)


def strip_mention(message) -> str:
    """The message's text without the bot's @mention, so the reader sees only the request."""
    text = message.text or ""
    mention = bot_mention(message)
    if mention and mention.casefold() in text.casefold():
        start = text.casefold().index(mention.casefold())
        text = (text[:start] + text[start + len(mention):]).strip(" ,:")
    return text.strip()
