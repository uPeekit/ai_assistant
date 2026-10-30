"""The conversation orchestrator: the one entry point the Telegram handlers call.

It joins the vault pipeline, the audit log, the chat's memory and the link reader into the
three things a user can do: send a message, press a button, undo. Everything it hands back is
an app.conversation.reply.Reply; nothing here imports python-telegram-bot, and no Russian
literal lives here (app/texts.py owns every string the user reads).

Turns of one chat are serialised by a per-chat asyncio.Lock taken at every entry point: a
double-tapped button delivers two callbacks into two concurrent handlers, and both would pass a
read-then-write guard (`executions.undone` before an undo) that is only safe against itself.
Different chats stay fully concurrent.

Two rules hold on every path through this module:

  * **One audit row per handled message.** `_open` inserts the `events` row, every stage writes
    its columns onto the in-memory `_Turn`, and `_finish` flushes them once — including on the
    error exits, so no path can leave a half-written row behind.
  * **It never raises to its caller.** Every failure becomes a Reply carrying a message from
    `texts.ERRORS` plus an audited error code; `_turn` catches whatever nobody expected.

Callback payloads are `u:<execution_id>` (undo) and `f:<execution_id>` (fix part of a write).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from app import address, texts
from app.audit.store import AuditStore
from app.chats import ChatRef, ChatScope
from app.config import Settings
from app.conversation.memory import ANSWER, BOT, USER, ChatMemory
from app.conversation.reply import Button, Reply
from app.conversation.undo import UndoRecord
from app.llm.answer import LinkAnswerer
from app.llm.health import Health
from app.llm.research import WebResearcher
from app.logging_setup import bind_event
from app.switches import Switches
from app.tuning import Tuning
from app.vault.pipeline import VaultPipeline, VaultTurn
from app.vault.writer import VaultUndo
from app.web.links import LinkReader

log = logging.getLogger(__name__)

GENERIC_ERROR = "INTERNAL"
# Decision placeholder written when an events row is opened; every exit overwrites it.
OPEN = "OPEN"
# /undo arrives without the sender's id in the owners' private chats (see Orchestrator.undo);
# the events row still needs a non-null user column.
NO_USER = 0
MAX_ANSWERS = 3  # recent answers a «save this» may mean


def now_utc() -> datetime:
    return datetime.now(UTC)


def _kind(kind: str) -> str:
    return json.dumps({"kind": kind})


def _models(calls: list[dict]) -> str:
    """The models a message used, for the row's one model column: "claude-haiku-4-5,
    claude-sonnet-5 x2"."""
    counts: dict[str, int] = {}
    for c in calls:
        counts[c["model"]] = counts.get(c["model"], 0) + 1
    return ", ".join(m if n == 1 else f"{m} x{n}" for m, n in counts.items())


def _error(code: str, **fmt: Any) -> str:
    """ERRORS[code] filled in, degrading to the generic message when the code is unknown or the
    caller could not supply every placeholder."""
    try:
        return texts.ERRORS[code].format(**fmt)
    except (KeyError, IndexError):
        return texts.ERRORS[GENERIC_ERROR]


def _vault_buttons(execution_id: int) -> list[list[Button]]:
    """Undo, and Fix: a write can be changed in part (app/vault/fix.py)."""
    return [[Button(f"u:{execution_id}", texts.BTN_UNDO),
             Button(f"f:{execution_id}", texts.BTN_FIX)]]


@dataclass
class _Turn:
    """One handled message: its open `events` row, the columns to write back when it closes, and
    the single clock reading every expiry inside the turn is measured against."""

    event_id: int
    chat_id: int
    now: datetime
    # Who sent it: in a group, the one who may undo or fix what it wrote, and their name in
    # Telegram.
    user_id: int = 0
    name: str = ""
    # The chat's recent exchanges as the readers are shown them, the bot's last answers, and
    # what the message may point back at: (key, text) — the quoted message, recent group talk.
    history: str = ""
    answers: tuple = ()
    pointed: tuple = ()
    started: float = field(default_factory=monotonic)
    cols: dict[str, Any] = field(default_factory=dict)
    # The pages behind the links in this turn's message (app.web.links.LinkPage), read once.
    links: list = field(default_factory=list)
    # Sends an intermediate message (a progress line) before the turn's own reply.
    progress: Callable[[Reply], Awaitable[None]] | None = None
    # Every model call this message made, in order (see `call`).
    calls: list[dict] = field(default_factory=list)
    # The vault's work on this message, and the execution row it wrote (see _finish_vault).
    vault: asyncio.Task | None = None
    execution_id: int | None = None

    def audit(self, **cols: Any) -> None:
        self.cols.update(cols)

    def call(self, model: str, kind: str, **detail: Any) -> None:
        self.calls.append({"model": model, "kind": kind,
                           **{k: v for k, v in detail.items() if v is not None}})


class Orchestrator:
    def __init__(
        self, settings: Settings, store: AuditStore, *, vault: VaultPipeline | None = None,
        clock: Callable[[], datetime] = now_utc, researcher: WebResearcher | None = None,
        switches: Switches | None = None, tuning: Tuning | None = None,
        health: Health | None = None, links: LinkReader | None = None,
        answerer: LinkAnswerer | None = None,
        scopes: Callable[[int], ChatScope | None] | None = None,
        memory: ChatMemory | None = None,
    ) -> None:
        self._s = settings
        self._store = store
        self._vault = vault
        self._clock = clock
        self._researcher = researcher
        self._switches = switches
        self._tuning = tuning
        self._health = health or Health()
        # Reads the links in a fresh message once, before the vault reads the message.
        self._links = links
        # Answers a question about a linked page; None: such a question is not answered.
        self._answerer = answerer
        # A listed group chat's scope (app/chats.py); None for the owners' private chats.
        self._scopes = scopes or (lambda _chat_id: None)
        # Each chat's recent exchanges (app/conversation/memory.py); None: no memory.
        self._memory = memory
        # The bot's own name, if it has one: recognised deterministically (app/address.py).
        # Read per message, so renaming it on the admin page needs no restart.
        self._fallback_names = address.names(settings.bot_name)
        # One lock per chat with a turn in flight, dropped again when the last one leaves, so a
        # long-lived process does not accumulate one per chat it has ever seen.
        self._locks: dict[int, asyncio.Lock] = {}
        self._waiting: dict[int, int] = {}
        # Chats whose next message is a fix, after the Fix button: (chat, presser) ->
        # (execution id, when the button was pressed). In memory on purpose: a restart forgets
        # it, and pressing the button again costs nothing. In a group only the presser's next
        # message is the fix: another member's message is their own.
        self._fixing: dict[tuple[int, int], tuple[int, datetime]] = {}

    @asynccontextmanager
    async def _chat_lock(self, chat_id: int) -> AsyncIterator[None]:
        """Serialise one chat's turns: Telegram delivers a double-tapped button as two
        callbacks ~100 ms apart into two concurrent handlers, where both reads of
        `executions.undone` see the state from before either write. Other chats are
        unaffected: the lock is per chat_id."""
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = self._locks[chat_id] = asyncio.Lock()
        self._waiting[chat_id] = self._waiting.get(chat_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._waiting[chat_id] - 1
            if remaining:
                self._waiting[chat_id] = remaining
            else:
                del self._waiting[chat_id]
                del self._locks[chat_id]

    # ---- entry points ------------------------------------------------------------------------

    async def handle_text(
        self, chat_id: int, user_id: int, text: str, *, kind: str = "text",
        transcript: str | None = None, progress: Callable[[Reply], Awaitable[None]] | None = None,
        reply_to: int | None = None, pointed: tuple = (), name: str = "",
    ) -> Reply:
        """`reply_to` is the id of the bot's message this one replies to in Telegram, if any:
        a reply to a write's message is a fix of that write. `pointed` is what the message may
        point back at — (key, text): the message it quotes, recent group messages."""
        def message(t: _Turn) -> Awaitable[Reply]:
            t.name = name
            return self._message(t, text, reply_to, pointed)

        async with self._chat_lock(chat_id):
            reply = await self._turn(chat_id, user_id, kind, message,
                                     progress=progress, raw_input=text,
                                     transcription=transcript)
            self._remember(chat_id, user_id, text, reply)
            return reply

    async def handle_callback(
        self, chat_id: int, user_id: int, data: str, *,
        progress: Callable[[Reply], Awaitable[None]] | None = None,
    ) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, user_id, "callback",
                                    lambda t: self._callback(t, data), progress=progress,
                                    raw_input=data)

    async def undo(self, chat_id: int, execution_id: int | None = None,
                   user_id: int = NO_USER) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, user_id, "undo",
                                    lambda t: self._undo(t, execution_id))

    # ---- memory ------------------------------------------------------------------------------

    def _remember(self, chat_id: int, user_id: int, text: str, reply: Reply) -> None:
        if self._memory is None:
            return
        now = self._clock()
        try:
            self._memory.add(chat_id, user_id, USER, text, now)
            self._memory.add(chat_id, user_id, BOT, reply.text, now)
            if reply.answer:
                self._memory.add(chat_id, user_id, ANSWER, reply.answer, now)
        except Exception:  # memory is a help, never a reason to fail a reply
            log.warning("could not remember the exchange in chat %s", chat_id)

    def _recall(self, turn: _Turn, pointed: tuple) -> None:
        turn.pointed = tuple(pointed)
        if self._memory is None:
            return
        try:
            said = self._memory.recent(turn.chat_id, self._clock())
        except Exception:
            log.warning("could not read the memory of chat %s", turn.chat_id)
            return
        labels = {USER: texts.MEMORY_USER, BOT: texts.MEMORY_BOT}
        turn.history = "\n".join(f"{labels[role]}: {text}" for role, text in said
                                 if role in labels)
        # Answers to questions, recorded as such: never a refusal or a hit list that happens
        # to start with the same mark.
        turn.answers = tuple(text for role, text in reversed(said) if role == ANSWER)[
            :MAX_ANSWERS]

    # ---- messages ----------------------------------------------------------------------------

    @property
    def _names(self) -> tuple[str, ...]:
        return (address.names(self._tuning.bot_name) if self._tuning is not None
                else self._fallback_names)

    def _vault_on(self) -> bool:
        return self._vault is not None and (
            self._switches.get("obsidian") if self._switches is not None else True)

    async def _message(self, turn: _Turn, text: str, reply_to: int | None,
                       pointed: tuple = ()) -> Reply:
        """A fix, when the message replies to a write's reply (the reply wins: it is the more
        specific, more recent gesture) or the Fix button was pressed and its write can still be
        fixed; otherwise an ordinary message. A pending fix whose write's window has closed is
        too late while the press itself is younger than the window — the message is meant as a
        correction, and filing it would turn "to groceries" into a grocery line; an older press
        is forgotten."""
        pending = self._fixing.pop((turn.chat_id, turn.user_id), None)
        self._recall(turn, pointed)
        scope = self._scopes(turn.chat_id)
        if turn.chat_id < 0 and scope is None:
            # A group the gate let in but that is no longer listed: fail closed, never as an
            # owner's private chat.
            turn.audit(decision=_kind("NOT_LISTED"))
            return Reply("")
        if reply_to is not None:
            row = self._store.execution_by_reply(turn.chat_id, reply_to)
            if row is not None and UndoRecord.model_validate_json(row["undo"]).kind == "vault":
                # In a group, replying to the bot is how a member talks to it: a reply to
                # someone else's write, or to one that can no longer be fixed, is a message.
                mine = self._may_touch(turn, row) and self._fixable(turn, row["id"]) is not None
                if scope is None or scope.full or mine:
                    return await self._fix(turn, text, row["id"])
        if pending is not None:
            execution_id, pressed_at = pending
            if self._fixable(turn, execution_id) is not None:
                return await self._fix(turn, text, execution_id)
            if turn.now - pressed_at < timedelta(seconds=self._s.undo_window_s):
                return self._fix_too_late(turn)
        if not self._vault_on():
            return self._plain(turn, "NOTHING_ENABLED")
        if scope is not None and not scope.full:
            return self._scoped(turn, text, scope)
        return await self._text(turn, text)

    def _scoped(self, turn: _Turn, text: str, scope: ChatScope) -> Reply:
        """A message in a group with its own scope: the vault narrowed to the chat's notes and
        rights (app/vault/scope.py)."""
        assert self._vault is not None
        called = address.strip(text, self._names)
        if called.only_name:
            turn.audit(decision=_kind("ABOUT"))
            return Reply(texts.CALLED)
        text = called.text if called.called else text
        turn.audit(decision=_kind("VAULT"))
        turn.vault = asyncio.create_task(
            self._vault.scoped(scope).handle(text, research=self._research,
                                             answer=self._answer, **self._context(turn)))
        return Reply("")

    async def _text(self, turn: _Turn, text: str) -> Reply:
        """An owner's message. Its reply is whatever _finish_vault makes of the vault's turn."""
        assert self._vault is not None
        called = address.strip(text, self._names)
        if called.only_name or (called.called and address.about_question(called.text)):
            # Being called by name with nothing else, or asked what it is: answered from
            # texts.py, with no model call and nothing written anywhere.
            turn.audit(decision=_kind("ABOUT"))
            return Reply(texts.ABOUT.format(name=self._names[0],
                                            digest_at=self._s.daily_digest_at)
                         if not called.only_name else texts.CALLED)
        text = called.text or text
        if self._links is not None:
            turn.links = await self._links.read(text)
        turn.audit(decision=_kind("VAULT"))
        turn.vault = asyncio.create_task(
            self._vault.handle(text, research=self._research, links=tuple(turn.links),
                               answer=self._answer, **self._context(turn)))
        return Reply("")

    @property
    def _research(self):
        return self._researcher.research if self._researcher is not None else None

    @property
    def _answer(self):
        return self._answerer.answer if self._answerer is not None else None

    def _context(self, turn: _Turn) -> dict:
        """What the vault side is told about this chat's conversation."""
        async def progress(text: str) -> None:
            await self._progress(turn, Reply(text))
        return {"chat": self._chat_ref(turn), "history": turn.history,
                "answers": turn.answers, "pointed": turn.pointed, "progress": progress}

    def _chat_ref(self, turn: _Turn) -> ChatRef:
        return ChatRef(turn.chat_id, turn.user_id, turn.event_id,
                       owner=turn.user_id in self._s.allowed_user_ids, name=turn.name)

    def _vault_for(self, turn: _Turn) -> VaultPipeline:
        assert self._vault is not None
        scope = self._scopes(turn.chat_id)
        return self._vault.scoped(scope) if scope is not None else self._vault

    def _may_touch(self, turn: _Turn, row: dict) -> bool:
        """May this sender undo or fix that write? In the owners' private chats, always. In a
        group: an owner, or the member whose message made it."""
        if turn.user_id in self._s.allowed_user_ids:
            return True
        if turn.chat_id > 0 and self._scopes(turn.chat_id) is None:
            return True  # an owner's private chat: only owners reach it
        event = self._store.get_event(row["event_id"]) or {}
        return bool(turn.user_id) and event.get("telegram_user_id") == turn.user_id

    @staticmethod
    async def _progress(turn: _Turn, reply: Reply) -> None:
        if turn.progress is None:
            return
        try:
            await turn.progress(reply)
        except Exception:  # a lost progress line must not stop the turn
            log.exception("could not send a progress message")

    # ---- callbacks ---------------------------------------------------------------------------

    async def _callback(self, turn: _Turn, data: str) -> Reply:
        prefix, _, rest = data.partition(":")
        # Any button other than Fix means the chat has moved on from a fix it asked for.
        self._forget_fix(turn)
        if prefix == "f" and rest.isdigit():
            return self._fix_ask(turn, int(rest))
        if prefix == "u" and rest.isdigit():
            return await self._undo(turn, int(rest))
        # A button from an older version of the bot (a Notion question, its inbox offer).
        log.warning("unknown callback payload in event %s", turn.event_id)
        return self._plain(turn, "BUTTON_EXPIRED")

    # ---- fixing part of a write --------------------------------------------------------------

    def _fixable(self, turn: _Turn, execution_id: int) -> tuple[dict, UndoRecord] | None:
        """The row and its record, when that write can still be fixed: this chat's, within the
        undo window, not undone, and every write knowing what it did."""
        row = self._store.get_execution(execution_id, turn.now)
        if row is None or row["undone"] or row["chat_id"] != turn.chat_id:
            return None
        record = UndoRecord.model_validate_json(row["undo"])
        if (record.kind != "vault" or not record.vault
                or any(u.action is None for u in record.vault) or not self._vault_on()):
            return None
        return row, record

    def _fix_too_late(self, turn: _Turn) -> Reply:
        turn.audit(decision=_kind("FIX"), error="FIX_EXPIRED")
        return Reply(texts.FIX_EXPIRED.format(minutes=max(1, self._s.undo_window_s // 60)))

    def _fix_ask(self, turn: _Turn, execution_id: int) -> Reply:
        found = self._fixable(turn, execution_id)
        if found is None:
            return self._fix_too_late(turn)
        if not self._may_touch(turn, found[0]):
            return self._plain_text(turn, texts.SCOPE_NOT_YOURS)
        self._fixing[(turn.chat_id, turn.user_id)] = (execution_id, turn.now)
        turn.audit(decision=_kind("FIX_ASK"))
        return Reply(texts.FIX_ASK)

    async def _fix(self, turn: _Turn, text: str, execution_id: int) -> Reply:
        """Change part of what a turn wrote. The old row is done with once anything changed;
        the new one carries the untouched writes and the new ones, so its Undo takes the whole
        thing back to before the first message."""
        found = self._fixable(turn, execution_id)
        if found is None:
            return self._fix_too_late(turn)
        row, record = found
        if not self._may_touch(turn, row):
            return self._plain_text(turn, texts.SCOPE_NOT_YOURS)
        turn.audit(decision=_kind("FIX"))
        event = self._store.get_event(row["event_id"]) or {}
        original = (event.get("raw_input") or event.get("transcription") or "").strip()
        result = await self._vault_for(turn).fix(original, text, record.vault)
        if result.model:
            turn.call(result.model, "fix", fixed=execution_id, writes=len(result.writes) or None,
                      error=result.error or None, prompt_tokens=result.prompt_tokens,
                      output_tokens=result.output_tokens)
        line = result.reply_line()
        if not result.applied:
            # The row stays valid and the user can try again; pressing Fix took the
            # buttons off the write's message, so they come back here. No undo_id: the write's
            # own message stays the one its row points to.
            return Reply(line, _vault_buttons(execution_id))
        self._store.mark_undone(execution_id)
        undos = result.undos
        if record.reminders:
            # A fix changes files, not the reminder: it stays as it was set, and Undo of the
            # fixed turn still cancels it.
            line = f"{line}\n{texts.FIX_REMINDER_KEPT}"
        if not undos and not record.reminders:  # everything dropped: nothing to take back
            return Reply(line)
        execution_id = self._add_execution(turn, undos, record.reminders)
        return Reply(line, _vault_buttons(execution_id), undo_id=execution_id)

    # ---- undo --------------------------------------------------------------------------------

    async def _undo(self, turn: _Turn, execution_id: int | None) -> Reply:
        self._forget_fix(turn)
        minutes = max(1, self._s.undo_window_s // 60)
        # In a group, /undo means the sender's own latest write, not whoever wrote last.
        mine = turn.user_id if self._scopes(turn.chat_id) is not None else None
        row = (self._store.get_execution(execution_id, turn.now) if execution_id is not None
               else self._store.latest_execution(turn.chat_id, turn.now, user_id=mine))
        if row is None or row["undone"] or row["chat_id"] != turn.chat_id:
            return self._plain(turn, "UNDO_EXPIRED", minutes=minutes)
        if not self._may_touch(turn, row):
            return self._plain_text(turn, texts.SCOPE_NOT_YOURS)
        record = UndoRecord.model_validate_json(row["undo"])
        vault_undos = record.vault_undos()
        if self._vault is None or not (vault_undos or record.reminders):
            # Nothing this bot can take back (an older version's Notion write).
            return self._plain(turn, "UNDO_EXPIRED", minutes=minutes)
        if record.reminders:
            # Cancelled whatever happens to the files: a reminder nobody wants must not fire.
            self._vault.cancel_reminders(record.reminders)
        left: list[str] = []
        if vault_undos:
            try:
                left = await self._vault.undo(vault_undos)
            except Exception as e:
                log.exception("undoing the vault side failed")
                return self._plain(turn, "UNDO_FAILED", message=type(e).__name__)
        # A write whose every line was left alone was not undone at all: the row stays, so the
        # user can revert their hand edit and press Undo again.
        if not (left and len(left) == len(vault_undos)):
            self._store.mark_undone(row["id"])
        turn.audit(decision=_kind("UNDO"))
        if not left:
            return Reply(texts.UNDONE)
        # A note somebody changed since, in the lines this turn wrote, is left as it is.
        notes = ", ".join(f"«{name}»" for name in dict.fromkeys(left))
        kept = texts.VAULT_UNDO_LEFT.format(notes=notes)
        return Reply(f"{texts.UNDONE}\n{kept}" if len(left) < len(vault_undos) else kept)

    # ---- the turn ----------------------------------------------------------------------------

    async def _turn(
        self, chat_id: int, user_id: int, kind: str,
        work: Callable[[_Turn], Awaitable[Reply]], *,
        progress: Callable[[Reply], Awaitable[None]] | None = None, **cols: Any,
    ) -> Reply:
        """Open the events row, run `work`, close the row — with all three inside the guard. The
        audit store is the one dependency that cannot report its own failure through the audit
        log, so a locked or unwritable database degrades to a logged INTERNAL reply instead of
        reaching the transport as an exception."""
        try:
            turn = self._open(chat_id, user_id, kind, **cols)
        except Exception:
            log.exception("could not open an audit event for chat %s", chat_id)
            return Reply(_error(GENERIC_ERROR))
        turn.progress = progress
        # Every log record produced while this turn is in flight — here, and in every module the
        # turn calls into — carries this event's id, so a log line can be traced back to its
        # `events` row.
        with bind_event(turn.event_id):
            try:
                reply = await work(turn)
            except Exception:  # the caller is a chat handler: it gets a Reply, always
                log.exception("unhandled failure in event %s", turn.event_id)
                reply = self._plain(turn, GENERIC_ERROR)
            try:
                reply = await self._finish_vault(turn, reply)
            except Exception:
                log.exception("obsidian side failed in event %s", turn.event_id)
                reply = self._plain(turn, GENERIC_ERROR)
            # Last line of every reply, at most once an hour: why the answers got worse.
            warning = await self._health.checked_note()
            if warning:
                reply = replace(reply, text=f"{reply.text}\n\n{warning}".strip())
            try:
                self._finish(turn)
            except Exception:  # the answer is already earned; losing the row must not eat it
                log.exception("could not close audit event %s", turn.event_id)
            decision = json.loads(turn.cols.get("decision") or "{}").get("kind", "-")
            log.info("done: %s in %.1fs", decision.lower(), monotonic() - turn.started)
        return reply

    async def _finish_vault(self, turn: _Turn, reply: Reply) -> Reply:
        """Wait for the vault's turn on this message, say in one line what it did, and give its
        write a row of its own with the Undo and Fix buttons."""
        if turn.vault is None:
            return reply
        result: VaultTurn = await turn.vault
        if result.said:
            reply = replace(reply, answer=result.said)
        if result.model and (result.writes or result.error):
            turn.call(result.model, "vault", writes=len(result.writes) or None,
                      error=result.error or None,
                      prompt_tokens=result.prompt_tokens, output_tokens=result.output_tokens)
        if result.undos or result.reminders:
            execution_id = self._add_execution(turn, result.undos, result.reminders)
            reply = replace(reply, buttons=_vault_buttons(execution_id), undo_id=execution_id)
        line = result.reply_line()
        return replace(reply, text=f"{reply.text}\n{line}".strip()) if line else reply

    def _add_execution(self, turn: _Turn, undos: list[VaultUndo],
                       reminders: list[int] | None) -> int:
        """A row for Undo and Fix to find. Its window is measured from the write, not from when
        the message arrived: a web search can spend minutes before anything is written."""
        turn.execution_id = self._store.add_execution(
            turn.event_id, turn.chat_id, None,
            UndoRecord(vault=undos, reminders=list(reminders or [])).model_dump_json(),
            self._clock() + timedelta(seconds=self._s.undo_window_s),
        )
        return turn.execution_id

    def _open(self, chat_id: int, user_id: int, kind: str, **cols: Any) -> _Turn:
        event_id = self._store.new_event(
            telegram_user_id=user_id, chat_id=chat_id, kind=kind,
            **{k: v for k, v in cols.items() if v is not None},
        )
        # A placeholder no exit ever writes, so the column is never null and a row that was
        # closed without a decision is visible as exactly that.
        return _Turn(event_id, chat_id, self._clock(), user_id=user_id,
                     cols={"decision": _kind(OPEN)})

    def _finish(self, turn: _Turn) -> None:
        if turn.links:
            # What the models were shown of the message's links: after a misfiling, the answer
            # alone does not say why.
            seen = json.loads(turn.cols.get("llm_context") or "{}")
            seen["links"] = [{"url": p.url, "title": p.title, "text": p.text, "error": p.error}
                             for p in turn.links]
            turn.audit(llm_context=json.dumps(seen, ensure_ascii=False, separators=(",", ":")))
        if turn.calls:
            turn.audit(llm_model=_models(turn.calls),
                       llm_response=json.dumps(turn.calls, ensure_ascii=False))
        self._store.update_event(
            turn.event_id, duration_ms=int((monotonic() - turn.started) * 1000), **turn.cols)

    def _plain(self, turn: _Turn, code: str, **fmt: Any) -> Reply:
        turn.audit(error=code, decision=json.dumps({"kind": "ERROR", "code": code}))
        return Reply(_error(code, **fmt))

    def _plain_text(self, turn: _Turn, text: str) -> Reply:
        """A refusal that is not an error of the bot's: another member's write in a group.
        Shown to the presser alone; the write's message keeps its buttons."""
        turn.audit(decision=_kind("NOT_YOURS"))
        return Reply(text, notice=True)

    def _forget_fix(self, turn: _Turn) -> None:
        """The pending Fix of whoever acted — every member's, for an /undo from no one in
        particular (an owner's private chat)."""
        if turn.user_id:
            self._fixing.pop((turn.chat_id, turn.user_id), None)
        else:
            for key in [k for k in self._fixing if k[0] == turn.chat_id]:
                del self._fixing[key]
