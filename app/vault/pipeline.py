"""The Obsidian side of a message, end to end and on its own.

It shares nothing with the Notion pipeline but the message itself: its own interpreter (the
filer), its own check, its own writer, its own undo. Notion can be switched off without
touching any of it.

It never asks the user anything and never fails a message: a model that is down, an action that
does not check out, a note that is gone — all of it ends as a line in the inbox note or as one
line in the reply saying nothing was written."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from app import texts
from app.chats import ChatRef, ChatScope
from app.google.sources import GoogleSources
from app.llm import staged_prompts as P
from app.llm.edits import EditError, Editor, NothingToChange
from app.llm.edits import TooLong as EditTooLong
from app.llm.prompts import MOVE_INSTRUCTION
from app.llm.research import ResearchError, ResearchQuestion
from app.llm.rewrite import RewriteError, Rewriter
from app.llm.rewrite import TooLong as RewriteTooLong
from app.reminders import timing
from app.reminders.store import Reminder, ReminderStore
from app.vault import agenda as agenda_mod
from app.vault import fix as fixing
from app.vault import frontmatter, mdedit, summary
from app.vault.answer import VaultAnswerer
from app.vault.askers import Askers
from app.vault.filer import GROCERY_LIST, Filer, FilerError, check, context, doubtful
from app.vault.index import VaultIndex
from app.vault.linker import Linker
from app.vault.omnisearch import OmniSearch
from app.vault.scope import ScopedIndex, allows_path, gate
from app.vault.search import MAX_HITS, Hit, search, vault_name
from app.vault.writer import VaultAction, VaultUndo, VaultWrite, VaultWriter

log = logging.getLogger(__name__)

# (the user's message, what to search for, text / images / text_and_images) -> markdown
Research = Callable[[str, str, str], Awaitable[str]]

MAX_SUMMARY = 3
# Notes a question's search may hand the answerer: a count needs every one of them.
ANSWER_HITS = 60
# Where a plan step's text goes when the filer picked no note for it. A folder, not the
# inbox note: the inbox keeps one-line reminders, and this is a page of research.
CARRIERS = ("note", "append", "update", "rewrite")
MAX_TITLE = 60


def with_content(actions: list[VaultAction], content: str,
                 message: str) -> list[VaultAction]:
    """Text a plan step has already produced, put into whichever action files it.

    The filer chose the place; the words are not its to rewrite, and it never saw them —
    what a web search brought back is data, and data does not go into a prompt. When the
    filer picked nothing that can hold a body, the text becomes a note of its own rather
    than a truncated line in the inbox."""
    lines = content.splitlines()
    out, used = [], False
    for action in actions:
        if not used and action.action in CARRIERS:
            action = action.model_copy(update={"body": lines})
            used = True
        out.append(action)
    if not used:
        title = (message.strip() or texts.VAULT_INBOX_NOTE)[:MAX_TITLE]
        out.append(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR,
                               title=title, body=lines))
    return out


def _trimmed(lines: list[str]) -> list[str]:
    """Moved lines without the blank ones at either end: the gap between sections belongs to
    neither of them."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@dataclass
class VaultTurn:
    writes: list[VaultWrite] = field(default_factory=list)
    # A question answered from the vault: its hits, and the vault's name for the links.
    hits: list[Hit] = field(default_factory=list)
    asked: bool = False
    # Reminders this turn set (app/reminders/): Undo cancels them.
    reminders: list[int] = field(default_factory=list)
    # The answer to a question, as words alone — what a later «save this» saves.
    said: str = ""
    # An answer built from dates rather than from words: the agenda (see app/vault/agenda.py).
    answer: str = ""
    vault: str = ""
    error: str = ""
    # Why Claude could not be used, when that is what went wrong: a code from
    # app/llm/health.py, so the reply names the cause in the user's own language rather than
    # showing them "claude 400".
    reason: str = ""
    # Said after what was written, when it was written with something missing: a search
    # that found nothing still leaves the note the user asked for.
    remark: str = ""
    model: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def undos(self) -> list[VaultUndo]:
        return [w.undo for w in self.writes if w.undo is not None]

    def reply_line(self) -> str:
        why = (texts.LLM_DOWN_SHORT.get(self.reason) or self.error) if self.error else ""
        if why and not self.writes:
            failed = texts.VAULT_FAILED.format(error=why)
            # A reminder set in the same turn is set: saying only "not written" would have
            # the user send it again and get two.
            return f"{failed}\n{self.answer}" if self.reminders and self.answer else failed
        if self.answer and not self.writes:
            return self.answer
        if self.asked and not self.writes:
            return self._found()
        if not self.writes:
            return ""
        # What was written is said even when something else went wrong: those files are on
        # disk and Undo reaches them, so a bare "not written" would be untrue twice over.
        notes = "; ".join(n for n in (self.remark, why) if n)
        tail = f" — {notes}" if notes else ""
        if len(self.writes) == 1:
            line = texts.VAULT_REPLY.format(what=self.writes[0].what + tail)
        else:
            # Several writes are a list, one per line: a dozen tasks, each with its tags
            # and date, do not fit one line and are the part the user wants to check.
            listed = [texts.VAULT_LIST_ITEM.format(what=w.what)
                      for w in self.writes[:summary.MAX_LISTED]]
            if len(self.writes) > summary.MAX_LISTED:
                listed.append(texts.VAULT_LIST_MORE.format(
                    n=len(self.writes) - summary.MAX_LISTED))
            # What went wrong goes last, on its own line, after the writes it qualifies.
            if notes:
                listed.append(texts.VAULT_LIST_NOTE.format(notes=notes))
            line = "\n".join([texts.VAULT_REPLY_LIST, *listed])
        if self.answer:
            return f"{line}\n{self.answer}"
        return f"{line}\n{self._found()}" if self.asked else line

    def _found(self) -> str:
        """The answer to a question, one line per hit, each a link that opens the note in
        Obsidian — on the phone too."""
        if not self.hits:
            return texts.VAULT_SEARCH_EMPTY
        lines = [texts.VAULT_SEARCH_HEADER]
        for hit in self.hits:
            if hit.kind == "task":
                lines.append(texts.VAULT_SEARCH_TASK.format(line=hit.line))
                continue
            name = f"[{hit.name}]({hit.uri(self.vault)})" if self.vault else hit.name
            lines.append(texts.VAULT_SEARCH_HIT.format(
                name=name, line=f" — {hit.line}" if hit.line else ""))
        return "\n".join(lines)


@dataclass
class FixTurn:
    """What a fix did: the writes it made, the turn's writes it left as they were, and the
    phrases the reply is made of."""

    writes: list[VaultWrite] = field(default_factory=list)
    kept: list[VaultUndo] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    # Something was actually changed on disk: the old row is done with.
    applied: bool = False
    error: str = ""
    reason: str = ""
    left: list[str] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def undos(self) -> list[VaultUndo]:
        return [*self.kept, *(w.undo for w in self.writes if w.undo is not None)]

    def reply_line(self) -> str:
        if self.left:
            notes = ", ".join(f"«{name}»" for name in self.left)
            return texts.VAULT_UNDO_LEFT.format(notes=notes)
        if not self.applied:
            why = texts.LLM_DOWN_SHORT.get(self.reason) or self.error or texts.FIX_UNCLEAR
            return texts.FIX_FAILED.format(error=why)
        what = "; ".join(self.said) or ", ".join(w.what for w in self.writes)
        if self.error:
            what += f" — {self.error}"
        return texts.FIX_DONE.format(what=what)


def _not_here(refused: list[str]) -> str:
    """What a group chat is told when part of its message was not its to ask."""
    # "inbox" is what a message nobody understood becomes, not something the group asked for.
    refused = [k for k in refused if k != "inbox"]
    if not refused:
        return texts.SCOPE_NOTHING
    kinds = ", ".join(dict.fromkeys(texts.SCOPE_KINDS.get(k, k) for k in refused))
    return texts.SCOPE_NOT_HERE.format(kinds=kinds)


class VaultPipeline:
    def __init__(self, index: VaultIndex, writer: VaultWriter, filer: Filer,
                 linker: Linker | None = None, *, now=datetime.now,
                 linking: Callable[[], bool] = lambda: True,
                 rewriter: Rewriter | None = None,
                 editor: Editor | None = None,
                 strong: Filer | None = None,
                 answerer: VaultAnswerer | None = None,
                 omni: OmniSearch | None = None,
                 reminders: ReminderStore | None = None, zone: str = "",
                 google: GoogleSources | None = None,
                 web_words: Callable[[], tuple[str, ...]] | None = None,
                 date_props: Callable[[], tuple[str, ...]] | None = None,
                 askers: Askers | None = None) -> None:
        self._index = index
        self._writer = writer
        # The whole vault and its writer. A group chat's turn (`scoped`) swaps `_index` and
        # `_writer` for its narrowed view; these stay what they are.
        self._base = index
        self._base_writer = writer
        self._scope: ChatScope | None = None
        self._filer = filer
        # The same question put to a stronger model, for an answer the light one visibly
        # got wrong (see filer.doubtful). None: the first answer always stands.
        self._strong = strong
        self._linker = linker
        self._rewriter = rewriter
        self._editor = editor
        # Reads the notes a question's search found and answers from them. None: the reply
        # is the list of what was found.
        self._answerer = answerer
        # Obsidian's own search, while the app runs: what it finds that ours did not is read
        # by the answerer too (app/vault/omnisearch.py).
        self._omni = omni
        self._reminders = reminders
        # Google Docs/Sheets/Forms a question may be answered from (app/google/); None: none.
        self._google = google
        # Who people are in the owner's records, as they told the bot (app/vault/askers.py).
        self._askers = askers or Askers()
        # The admin page's words for a web search and its date properties, read per message.
        self._web_words = web_words or (lambda: ())
        self._date_props = date_props or (lambda: agenda_mod.DATE_PROPS)
        # The user's time zone: `now` gives local wall-clock time, and a reminder is stored as
        # an aware moment.
        self._zone = ZoneInfo(zone) if zone else None
        self._now = now
        self._linking = linking
        self._tasks: set[asyncio.Task] = set()  # linking, running behind the reply

    # ---- reminders (app/reminders/) -----------------------------------------------------

    def _local(self) -> datetime:
        now = self._now()
        if now.tzinfo is None and self._zone is not None:
            return now.replace(tzinfo=self._zone)
        return now if now.tzinfo is not None else now.astimezone()

    def _pending(self, chat: ChatRef | None) -> list[Reminder]:
        if self._reminders is None or chat is None:
            return []
        return self._reminders.pending(chat.chat_id)

    def _listed(self, reminder: Reminder) -> str:
        now = self._local()
        moment = reminder.due_at.astimezone(now.tzinfo)
        return f"{timing.label(moment, reminder.repeat, now.date())}: {reminder.text}"

    def cancel_reminders(self, ids: list[int]) -> None:
        """Undo of a turn that set reminders."""
        if self._reminders is not None:
            for reminder_id in ids:
                self._reminders.cancel(reminder_id)

    def _reminded(self, actions: list[VaultAction], chat: ChatRef | None,
                  pending: list[Reminder], turn: VaultTurn) -> list[VaultAction]:
        """Set, list and cancel what the reader asked for; the rest goes on to the writer.
        A reminder's task gets the date the reminder settled on; a reminder that cannot be set
        takes its task with it — "remind me" never leaves a lone task behind."""
        kinds = ("reminder", "reminder_list", "reminder_cancel")
        asked = [a for a in actions if a.action in kinds]
        if not asked:
            return actions
        rest = [a for a in actions if a.action not in kinds]
        lines: list[str] = []
        now = self._local()
        for action in asked:
            paired = [t for t in rest if t.action == "task" and t.text == action.text]
            if self._reminders is None or chat is None:
                lines.append(texts.REMIND_OFF)
                continue
            if action.action == "reminder_list":
                lines.append("\n".join([texts.REMIND_LIST, *(
                    texts.REMIND_ITEM.format(
                        when=timing.label(r.due_at.astimezone(now.tzinfo), r.repeat,
                                          now.date()), text=r.text) for r in pending)])
                             if pending else texts.REMIND_NONE)
                continue
            if action.action == "reminder_cancel":
                found = pending[action.reminder - 1] if 0 < action.reminder <= len(
                    pending) else None
                if found is None:
                    lines.append(texts.REMIND_NOT_FOUND)
                elif not chat.owner and found.user_id != chat.user_id:
                    lines.append(texts.SCOPE_NOT_YOURS)
                else:
                    self._reminders.cancel(found.id)
                    lines.append(texts.REMIND_CANCELLED.format(text=found.text))
                continue
            if not (action.due or action.time or action.repeat):
                lines.append(texts.REMIND_WHEN)
                rest = [a for a in rest if a not in paired]
                continue
            moment = timing.first_due(action.due, action.time, action.repeat, now)
            if moment is None:
                if action.due == now.date().isoformat() and not action.time:
                    # "today", no time, after 09:00: when today is the question, not "past".
                    lines.append(texts.REMIND_WHEN)
                else:
                    asked = timing.asked_for(action.due, action.time, now)
                    lines.append(texts.REMIND_PAST.format(when=timing.when(asked, now.date())))
                rest = [a for a in rest if a not in paired]
                continue
            reminder_id = self._reminders.add(chat.chat_id, chat.user_id, action.text, moment,
                                              repeat=action.repeat, event_id=chat.event_id)
            turn.reminders.append(reminder_id)
            lines.append(texts.REMIND_SET.format(
                when=timing.label(moment, action.repeat, now.date()), text=action.text))
            rest = [t.model_copy(update={"due": moment.date().isoformat()})
                    if t in paired and not t.due else t for t in rest]
        said = "\n".join(lines)
        turn.answer = f"{said}\n{turn.answer}".strip() if turn.answer else said
        return rest

    @property
    def index(self) -> VaultIndex:
        """The whole vault's index (the admin page lists its folders and tags)."""
        return self._base

    def scoped(self, scope: ChatScope) -> VaultPipeline:
        """This pipeline for one turn of a chat with its own scope (app/vault/scope.py): the
        same models and writer, narrowed to the chat's notes and rights. No linker — a link
        would carry an owner's note name into the chat's note — and no second reading."""
        if scope.full:
            return self
        other = copy.copy(self)
        other._scope = scope
        other._linker = None
        other._strong = None
        return other

    async def _refreshed(self) -> None:
        """The index read again, and — for a scoped turn — the chat's view of it built anew,
        with a writer that refuses everything outside it."""
        await asyncio.to_thread(self._base.refresh)
        if self._scope is not None:
            view = ScopedIndex(self._base, self._scope)
            self._index = view
            self._writer = self._base_writer.guarded(allows_path(view), view)

    def _gated(self, actions: list[VaultAction]) -> tuple[list[VaultAction], list[str]]:
        if self._scope is None:
            return actions, []
        return gate(actions, self._index)

    @property
    def plans_itself(self) -> bool:
        """Does this vault's reader look things up and search the web on its own? Then the
        Notion side hands it nothing: the two branches plan independently."""
        return bool(getattr(self._filer, "plans_itself", False))

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._filer.aclose()
        if self._strong is not None:
            await self._strong.aclose()
        if self._linker is not None:
            await self._linker.aclose()
        if self._rewriter is not None:
            await self._rewriter.aclose()
        if self._editor is not None:
            await self._editor.aclose()
        if self._answerer is not None:
            await self._answerer.aclose()

    async def handle(self, message: str, *, content: str = "",
                     go: Callable[[], Awaitable[bool]] | None = None,
                     research: Research | None = None, links: tuple = (),
                     answer: Callable[[str, list], Awaitable[str]] | None = None,
                     chat: ChatRef | None = None, history: str = "", answers: tuple = (),
                     pointed: tuple = (),
                     progress: Callable[[str], Awaitable[None]] | None = None) -> VaultTurn:
        """Read the message, write the vault, and start the linking behind the reply.

        `content` is text the caller already has (a plan step's research): the filer still
        decides where it goes, but it is written as it stands. `go` is awaited before
        anything is written, so a caller that started this in parallel and then learned it
        was not wanted can stop it without a half-finished write — cancelling the task
        could not, because the writes happen in a thread.

        `research` runs a web search for an action that asks for one (the staged reader
        decides that on its own); the orchestrator hands in one that is shared with the
        Notion side, so the same search for the same message runs once."""
        try:
            # The answer to the bot's «who are you in the records»: their name is kept, and
            # the question they asked first is the one to answer now.
            first = self._askers.reply(chat.chat_id, chat.user_id, message) \
                if chat is not None else None
            message = first or message
            await self._refreshed()
            ctx = replace(context(self._index, message, self._now()), links=tuple(links))
            pending = await asyncio.to_thread(self._pending, chat)
            ctx = replace(ctx, reminders=tuple(self._listed(r) for r in pending),
                          now_time=self._local().strftime("%H:%M"), history=history,
                          answers=tuple(answers), pointed=tuple(pointed),
                          web_words=tuple(self._web_words()))
            raw, prompt_tokens, output_tokens = await self._filer.file(message, ctx)
        except FilerError as e:
            log.warning("filer failed: %s", e)
            return await self._kept(message, e, go)
        except OSError as e:
            log.warning("vault unreadable: %s", e)
            return VaultTurn(error=type(e).__name__)
        model = self._filer.model
        actions = check(raw, self._index, message)
        # Before the gate, not after it: the gate opens when the whole turn is over, and a
        # second model call there would be added to the wait instead of hidden inside it.
        # Text the caller already has fills an empty note, so there is nothing to doubt.
        why = doubtful(actions) if self._strong is not None and not content else ""
        if why:
            try:
                again, more_in, more_out = await self._strong.file(message, ctx)
            except FilerError as e:  # the first answer stands
                log.warning("second reading failed: %s", e)
            else:
                log.info("vault: %s from %s; read again by %s", why, model,
                         self._strong.model)
                actions, model = check(again, self._index, message), self._strong.model
                prompt_tokens += more_in
                output_tokens += more_out
        actions, refused = self._gated(actions)
        if self._scope is not None and not actions:
            # A group chat never falls back to the owner's inbox: it is told what it may not
            # do here, or that the bot did not understand, and nothing is written.
            return VaultTurn(answer=_not_here(refused), model=model,
                             prompt_tokens=prompt_tokens, output_tokens=output_tokens)
        if go is not None and not await go():
            log.info("vault: held back; the text the Notion side produced is written instead")
            return VaultTurn(model=model, prompt_tokens=prompt_tokens,
                             output_tokens=output_tokens)
        if not actions and any(isinstance(r, dict) and r.get("looked_up") for r in raw):
            # Every item of the set the user asked for is already in the vault, and check()
            # leaves those alone. That is an answer, not a message nobody understood.
            log.info("vault: everything looked up is already there")
            return VaultTurn(answer=texts.VAULT_ALL_THERE, model=model,
                             prompt_tokens=prompt_tokens, output_tokens=output_tokens)
        if not actions:  # the model answered nothing usable: keep the words rather than drop them
            actions = [VaultAction(action="inbox", text=message)]
        if content:
            actions = with_content(actions, content, message)
        # A group sees no obsidian:// links: they open only on the owner's devices.
        turn = VaultTurn(model=model, prompt_tokens=prompt_tokens, output_tokens=output_tokens,
                         vault="" if self._scope is not None else vault_name(self._index.root))
        if refused:
            turn.remark = _not_here(refused)
        actions = await asyncio.to_thread(self._reminded, actions, chat, pending, turn)
        if any(a.research for a in actions):
            actions = await self._looked_up(actions, message, research, turn)
        about_links = [a for a in actions if a.action == "link_answer"]
        actions = [a for a in actions if a.action != "link_answer"]
        if about_links:
            await self._answer_links(message, links, answer, turn)
        dated = [a for a in actions if a.action == "agenda"]
        questions = [a for a in actions if a.action == "search"]
        shopping = [a for a in actions
                    if a.action == "grocery" and a.scope == GROCERY_LIST]
        actions = [a for a in actions if a.action not in ("search", "agenda")
                   and a not in shopping]
        for _ in shopping[:1]:  # one answer however many times it was asked for
            answer = await asyncio.to_thread(self._grocery_answer)
            turn.answer = f"{turn.answer}\n{answer}".strip() if turn.answer else answer
        # A rewrite and a move are the actions the filer cannot finish on its own: they need
        # the note's current text, which the filer never saw. Each becomes actions carrying
        # the finished text, or is dropped with a line in the reply.
        prepared: list[VaultAction] = []
        for action in actions:
            if action.action == "rewrite":
                rewritten = await self._rewritten(action, turn)
                prepared += [rewritten] if rewritten is not None else []
            elif action.action == "move":
                prepared += await self._moved(action, turn, prepared)
            else:
                prepared.append(action)
        actions = prepared
        for question in dated:
            answer = await asyncio.to_thread(self._agenda_answer, question)
            turn.answer = f"{turn.answer}\n{answer}".strip() if turn.answer else answer
        for question in questions:
            turn.asked = True
            # More than the list shows: the answer reads the best of them, and a count
            # ("how many books have I read") needs all of them.
            hits = await asyncio.to_thread(
                search, self._index, question.text, folder=question.folder,
                tags=tuple(question.tags), props=question.props,
                limit=ANSWER_HITS if self._answerer is not None else MAX_HITS)
            turn.hits += hits[:MAX_HITS]
            if self._answerer is not None and self._omni is not None:
                hits = hits + await asyncio.to_thread(self._omni_hits, question.text, hits)
            said = await self._answered(message, hits, turn, research=research,
                                        progress=progress, history=history, chat=chat,
                                        words=question.text, replied=first is not None)
            if said:
                turn.answer = f"{turn.answer}\n{said}".strip() if turn.answer else said
        turn.writes, failed = await asyncio.to_thread(self._write_all, actions)
        if failed and not turn.writes:
            turn.error = turn.error or failed[0]
            return turn
        if failed:
            turn.remark = "; ".join(
                r for r in (turn.remark, texts.VAULT_SOME_FAILED.format(n=len(failed))) if r)
        log.info("vault %s: %s", model,
                 ", ".join([*(f"{w.kind}:{w.note}" for w in turn.writes),
                            *([f"search:{len(turn.hits)} hits"] if turn.asked else []),
                            # Without this an answered question looked exactly like a turn
                            # that did nothing at all, which is how the overdue bug hid.
                            *([f"answered:{len(turn.answer.splitlines())} lines"]
                              if turn.answer else [])]) or "-")
        self._link_later(turn.writes)
        return turn

    async def fix(self, original: str, correction: str,
                  undos: list[VaultUndo]) -> FixTurn:
        """Change part of what one turn wrote (app/vault/fix.py). `undos` are that turn's
        writes, each carrying the action it did. Never raises, never asks."""
        turn = FixTurn(model=self._filer.model)
        fix = getattr(self._filer, "fix", None)
        previous = [VaultAction(**u.action) for u in undos if u.action is not None]
        if fix is None or len(previous) != len(undos) or not previous:
            turn.error = texts.FIX_UNCLEAR
            return turn
        try:
            await self._refreshed()
            ctx = context(self._index, f"{original}\n{correction}", self._now())
            plan, turn.prompt_tokens, turn.output_tokens = await fix(
                original, correction, previous, ctx)
        except FilerError as e:
            log.warning("fix failed: %s", e)
            turn.error, turn.reason = str(e), e.reason
            return turn
        except OSError as e:
            log.warning("vault unreadable: %s", e)
            turn.error = type(e).__name__
            return turn
        if plan.unclear:
            if plan.said:  # e.g. the one move that could not be done: name it
                turn.error = "; ".join(plan.said)
            return turn
        # What the fix writes is checked like any answer; a web search is not part of a fix.
        actions = [a.model_copy(update={"research": "", "media": ""})
                   for a in check(plan.write, self._index, correction, regroup=False)]
        actions, refused = self._gated(actions)
        if refused:
            # All or nothing: the plan takes writes back to replace them, and dropping only
            # the replacement would take the group's own line away for nothing.
            turn.error = _not_here(refused)
            return turn
        scratch = VaultTurn()
        prepared: list[VaultAction] = []
        for action in actions:
            if action.action == "rewrite":
                rewritten = await self._rewritten(action, scratch)
                prepared += [rewritten] if rewritten is not None else []
            else:
                prepared.append(action)
        if not prepared and not plan.take_back:
            turn.error = scratch.error
            return turn
        old = [undos[i] for i in plan.take_back]
        try:
            turn.writes, failed, turn.left, stuck = await asyncio.to_thread(
                self._writer.replace_writes, old, prepared)
        except (OSError, ValueError) as e:
            log.warning("vault fix write failed: %s", type(e).__name__)
            turn.error = type(e).__name__
            return turn
        if turn.left:
            return turn
        turn.applied = True
        turn.kept = [u for i, u in enumerate(undos) if i not in set(plan.take_back)]
        turn.said = plan.said
        problems = []
        if failed:
            problems.append(texts.FIX_NOT_WRITTEN.format(
                items=", ".join(fixing.words(prepared[i]) for i in failed)))
        if stuck:
            problems.append(texts.FIX_NOT_TAKEN_BACK.format(
                notes=", ".join(f"«{name}»" for name in stuck)))
        turn.error = "; ".join(problems)
        log.info("vault fix %s: took back %d, wrote %d", turn.model, len(old),
                 len(turn.writes))
        self._link_later(turn.writes)
        return turn

    async def _kept(self, message: str, error: FilerError,
                    go: Callable[[], Awaitable[bool]] | None) -> VaultTurn:
        """The model could not be asked — Claude is down, out of credit, or answered rubbish.
        The words go to the inbox note as they are, which takes no model: with Notion off
        there is no other place that would have kept them. The reply says both things, that
        they are saved and why they are not filed."""
        turn = VaultTurn(error=str(error), reason=error.reason, model=self._filer.model)
        if self._scope is not None:
            return turn  # the inbox is the owner's: a group's words are not kept there
        if not message.strip() or (go is not None and not await go()):
            return turn
        try:
            write = await asyncio.to_thread(
                self._writer.run, VaultAction(action="inbox", text=message))
        except (OSError, ValueError) as e:
            log.warning("could not keep the message in the inbox note: %s", e)
            return turn
        turn.writes = [write.model_copy(update={"kind": "kept"})]
        return turn

    async def _looked_up(self, actions: list[VaultAction], message: str,
                         research: Research | None, turn: VaultTurn) -> list[VaultAction]:
        """Each action that asks for a web search, with what the search found as its body.

        The found text goes into the note as it stands and never into a prompt: what the
        web returned is data. A search that fails still leaves a new note written (the
        user asked for the page), with a word in the reply about why it is empty."""
        out: list[VaultAction] = []
        for action in actions:
            if not action.research:
                out.append(action)
                continue
            query, media = action.research, action.media or "text"
            action = action.model_copy(update={"research": "", "media": ""})
            found = ""
            if research is None:
                turn.remark = turn.remark or texts.VAULT_WEB_OFF
            else:
                try:
                    found = await research(message, query, media)
                except ResearchQuestion:
                    turn.remark = turn.remark or texts.VAULT_WEB_UNCLEAR
                except ResearchError as e:
                    log.warning("vault web search failed: %s", e)
                    turn.remark = turn.remark or texts.VAULT_WEB_FAILED
            if found.strip():
                action = action.model_copy(
                    update={"body": [*action.body, *found.strip().splitlines()]})
            elif action.action == "append" and not action.body:
                continue  # nothing to add to a note that is already there
            out.append(action)
        return out

    def _asker(self, chat: ChatRef | None) -> str:
        """Who asks, as the answerer is told: their Telegram name, and the name they said they
        go by in the records."""
        if chat is None:
            return ""
        known = self._askers.known(chat.user_id)
        return P.ASKER_KNOWN.format(name=chat.name, known=P.names(known)) if known \
            else chat.name

    def _omni_hits(self, query: str, hits: list[Hit]) -> list[Hit]:
        """What Omnisearch finds that our search did not, after ours."""
        seen = {h.path for h in hits}
        found = [p for p in self._omni.find(query, self._index) if p not in seen]
        if found:
            log.info("omnisearch added %d note(s)", len(found))
        return [Hit(PurePosixPath(p).stem, p) for p in found]

    async def _answered(self, question: str, hits: list[Hit], turn: VaultTurn, *,
                        research: Research | None = None,
                        progress: Callable[[str], Awaitable[None]] | None = None,
                        history: str = "", chat: ChatRef | None = None,
                        words: str = "", replied: bool = False) -> str:
        """The answer to a question, in the chat: from the notes (with links to them), from
        general knowledge, or — when it needs fresh facts — from a web search. "" when there
        is no answerer or it failed, and the list of what the search found is the reply.
        `words` are the question's search words, the earlier question folded in when this
        message only completes it. `replied`: this question comes back after the asker told
        the bot their name, so it is not asked a second time."""
        if self._answerer is None:
            return ""
        now = self._now()
        today = f"{now:%Y-%m-%d}, {texts.VAULT_WEEKDAYS_SHORT[now.weekday()]}"
        google = []
        if self._google is not None and chat is not None:
            # A private chat of an owner sees every source and the Drive; a group only its own.
            query = f"{question} {words}" if words and words not in question else question
            google = await self._google.find(query, chat.chat_id,
                                             owner=chat.owner and chat.chat_id > 0)
        try:
            known = self._askers.known(chat.user_id) if chat is not None else ()
            # Said inside the question, not only beside it: told who the asker is in a context
            # line, a model still asked them again.
            asked = P.ASKER_IN_QUESTION.format(question=question, known=P.names(known)) \
                if known else question
            found = await self._answerer.answer(asked, hits, self._index, today,
                                                history=history, google=google,
                                                asker=self._asker(chat))
        except FilerError as e:
            log.warning("vault answer failed (%s); listing what was found", e)
            return ""
        turn.prompt_tokens += found.prompt_tokens
        turn.output_tokens += found.output_tokens
        log.info("vault answer %s: %s, %d source(s), %d+%d tokens", self._answerer.model,
                 found.kind, len(found.sources), found.prompt_tokens, found.output_tokens)
        if found.kind == "web" and found.web:
            return await self._from_web(question, found, turn, research, progress, history)
        if found.kind == "general":
            turn.said = found.text
            return texts.ANSWER_GENERAL.format(answer=found.text)
        if found.kind == "who":
            # A question back, not an answer: nothing to save, no sources. The person's next
            # short message is their name (Askers.reply), and this question is answered then.
            if chat is None:
                return texts.ANSWER_WHO.format(lead="")
            if replied:
                # Told and still not found: asking again would go round in circles.
                return texts.ANSWER_WHO_NOT_FOUND.format(name=known[-1])
            # Names they gave before are not in these records (a quiz under a joke name): ask
            # once more, saying so.
            lead = texts.WHO_NOT_HERE.format(names=", ".join(f"«{n}»" for n in known)) \
                if known else ""
            self._askers.asked(chat.chat_id, chat.user_id, question)
            return (texts.ANSWER_WHO_OPTIONS.format(
                lead=lead, options=", ".join(f"«{o}»" for o in found.options))
                if found.options else texts.ANSWER_WHO.format(lead=lead))
        if found.kind == "notes":
            turn.said = found.text
        lines = [texts.VAULT_ANSWER.format(answer=found.text)]
        if self._scope is not None:
            named = [note.name for note in found.sources]
        else:
            vault = vault_name(self._index.root)
            named = [f"[{note.name}]({Hit(note.name, note.path).uri(vault)})"
                     for note in found.sources]
        if found.google:
            # Which Google parts an answer came from is for the log: in the reply it was a
            # line of document and section names nobody could open.
            log.info("vault answer from google: %s", "; ".join(found.google))
        if named:
            lines.append(texts.VAULT_ANSWER_SOURCES.format(links=", ".join(named)))
        notice = self._google.sign_in_notice() if self._google and chat and chat.owner \
            and chat.chat_id > 0 else ""
        if notice:
            lines.append(notice)
        return "\n".join(lines)

    async def _from_web(self, question: str, found, turn: VaultTurn,
                        research: Research | None,
                        progress: Callable[[str], Awaitable[None]] | None,
                        history: str) -> str:
        """A question that needs fresh facts: the shared web search, then one answer from
        what it found. Without a search, what the model knew is the answer, if anything."""
        if research is None:
            return texts.ANSWER_GENERAL.format(answer=found.text) if found.text else ""
        if progress is not None:
            try:
                await progress(texts.ANSWER_SEARCHING)
            except Exception:  # a lost progress line costs nothing
                log.debug("progress line not sent")
        try:
            findings = await research(question, found.web, "text")
            web = await self._answerer.from_web(question, findings, history)
        except ResearchQuestion as q:
            # The search needs to know more ("which date?"): asked back, nothing answered.
            return texts.ANSWER_WEB_ASK.format(question=q.question)
        except (ResearchError, FilerError) as e:
            log.warning("web answer failed: %s", e)
            return texts.ANSWER_WEB_FAILED.format(error=e)
        turn.prompt_tokens += web.prompt_tokens
        turn.output_tokens += web.output_tokens
        turn.said = web.text
        return texts.ANSWER_WEB.format(answer=web.text)

    async def _rewritten(self, action: VaultAction, turn: VaultTurn) -> VaultAction | None:
        """The same action with `body` filled in, or None when nothing could be changed and
        saying so beats writing something.

        One model call decides whether this is a few edits or a whole new text; the note (or the
        named section) is read once, and the edits are applied to exactly the lines that were
        numbered. A heading that is no longer there widens the change to the whole note rather
        than failing: the user asked for the note to change, and the section was only how they
        pointed.

        Undo needs nothing special here. The writer keeps the file's whole previous text, so
        putting a note back is exact — including its pictures, whose files are never deleted."""
        note = self._index.by_name(action.note)
        if note is None:
            return None
        if self._editor is None and self._rewriter is None:
            turn.error = turn.error or texts.VAULT_REWRITE_OFF
            return None
        try:
            text = await asyncio.to_thread(self._index.read, note.path)
        except OSError as e:
            log.warning("could not read %s to change it: %s", note.path, e)
            return None
        # The note without its frontmatter: the writer puts the properties back, so a line
        # number the model is given has to mean the same line the writer will change.
        _, note_body = frontmatter.split(text)
        current, heading = note_body, action.heading
        if heading:
            lines = note_body.split("\n")
            section = mdedit.find_section(lines, heading)
            if section is None:
                heading = ""
            else:
                current = "\n".join(lines[section.start:section.end])
        body = await self._new_body(note.name, current, action.text, turn)
        if body is None:
            return None
        return action.model_copy(update={"heading": heading, "body": body})

    async def _new_body(self, name: str, current: str, instruction: str,
                        turn: VaultTurn) -> list[str] | None:
        """The note's (or section's) new lines: a few edits applied, or a whole new text."""
        lines = current.split("\n")
        if self._editor is not None:
            try:
                plan, prompt_tokens, output_tokens = await self._editor.plan(
                    mdedit.numbered(lines), instruction)
            except NothingToChange:
                # Not a failure: the model read the note and the instruction does not apply
                # to anything in it. The "not written" wording made it look like a breakage.
                turn.error = turn.error or texts.VAULT_NOTHING_TO_CHANGE
                return None
            except EditTooLong:
                turn.error = turn.error or texts.VAULT_TOO_LONG
                return None
            except EditError as e:
                return self._failed(name, e, turn)
            turn.prompt_tokens += prompt_tokens
            turn.output_tokens += output_tokens
            if plan.edits:
                # A note is the user's own text file and the previous version is kept whole, so
                # the only rule worth enforcing is that an edit may not point outside what the
                # model was shown; `apply_edits` ignores anything that does.
                return mdedit.apply_edits(lines, plan.edits)
            if plan.full.strip():
                return plan.full.splitlines()
            return None
        assert self._rewriter is not None
        try:
            new_text, prompt_tokens, output_tokens = await self._rewriter.rewrite(
                current, instruction)
        except RewriteTooLong:
            turn.error = turn.error or texts.VAULT_TOO_LONG
            return None
        except RewriteError as e:
            return self._failed(name, e, turn)
        turn.prompt_tokens += prompt_tokens
        turn.output_tokens += output_tokens
        return new_text.splitlines()

    async def _moved(self, action: VaultAction, turn: VaultTurn,
                     earlier: list[VaultAction]) -> list[VaultAction]:
        """Part of one note taken, word for word, to another: the writes that do it, the
        destination first — so a failure halfway leaves a copy, never a loss.

        The model only points at lines; it never writes the moved text. Its answer counts only
        when every operation is a delete: those are the lines that move, copied by this code as
        they stand. Anything else — a rewrite, an insertion, nothing at all — moves nothing and
        cuts nothing."""
        note = self._index.by_name(action.note)
        if note is None:
            return []
        if self._editor is None:
            turn.error = turn.error or texts.VAULT_REWRITE_OFF
            return []
        try:
            text = await asyncio.to_thread(self._index.read, note.path)
        except OSError as e:
            log.warning("could not read %s to move from it: %s", note.path, e)
            return []
        # Numbered without the frontmatter, as for a rewrite: the writer keeps the properties.
        _, body = frontmatter.split(text)
        lines = body.split("\n")
        try:
            plan, prompt_tokens, output_tokens = await self._editor.plan(
                mdedit.numbered(lines), MOVE_INSTRUCTION.format(to=action.to, what=action.text))
        except NothingToChange:
            turn.error = turn.error or texts.VAULT_NOTHING_TO_MOVE
            return []
        except EditTooLong:
            turn.error = turn.error or texts.VAULT_TOO_LONG
            return []
        except EditError as e:
            self._failed(note.name, e, turn)
            return []
        turn.prompt_tokens += prompt_tokens
        turn.output_tokens += output_tokens
        taken = sorted({n for e in plan.edits if e.op == "delete"
                        for n in e.span if 1 <= n <= len(lines)})
        moved = _trimmed([lines[n - 1] for n in taken])
        if plan.full.strip() or any(e.op != "delete" for e in plan.edits) or not moved:
            log.info("move from %s refused: %s", note.name,
                     "whole text" if plan.full.strip() else
                     "not only deletes" if plan.edits else "nothing marked")
            turn.error = turn.error or texts.VAULT_MOVE_UNCLEAR
            return []
        gone = set(taken)
        rest = [line for n, line in enumerate(lines, start=1) if n not in gone]
        cut = VaultAction(action="rewrite", note=note.name, text=action.text,
                          body=rest or [""])
        wanted = action.to.casefold()
        made = next((a for a in earlier if a.action == "note"
                     and (a.title or a.text).strip().casefold() == wanted), None)
        if made is not None:
            # "create X and move … there": the note is being written in this same message.
            made.body = [*made.body, *moved]
            return [cut]
        target = self._index.by_name(action.to)
        if target is not None:
            put = VaultAction(action="append", note=target.name, body=moved)
        else:
            folder = str(PurePosixPath(note.path).parent)
            put = VaultAction(action="note", folder="" if folder == "." else folder,
                              title=action.to, body=moved)
        return [put, cut]

    @staticmethod
    def _failed(name: str, error: Exception, turn: VaultTurn) -> None:
        log.warning("changing %s failed: %s", name, error)
        turn.error = turn.error or str(error)
        turn.reason = turn.reason or getattr(error, "reason", "")
        return None

    def _grocery_answer(self) -> str:
        """What still has to be bought, or that nothing does."""
        from app.vault import groceries

        want = groceries.needed(self._index.groceries())
        if not want:
            return texts.VAULT_GROCERIES_EMPTY
        return texts.VAULT_GROCERIES_LIST.format(items=", ".join(want))

    @staticmethod
    async def _answer_links(message: str, links: tuple,
                            answer: Callable[[str, list], Awaitable[str]] | None,
                            turn: VaultTurn) -> None:
        """A question about a page the user sent, answered from the page; nothing written.
        Only asked when the vault is on its own: with Notion on, its interpreter decides
        answers, so the two sides never both answer one message."""
        pages = [page for page in links if not page.error]
        if answer is None or not pages:
            return
        try:
            reply = await answer(message, pages)
        except Exception as e:  # the answerer's own error, or Claude being down
            log.warning("vault: link answer failed (%s)", type(e).__name__)
            reply = texts.ERRORS["LINK_ANSWER_FAILED"]
        turn.answer = f"{turn.answer}\n{reply}".strip() if turn.answer else reply

    def _agenda_answer(self, action: VaultAction) -> str:
        """A question about dates, answered from the vault: a day, a range, or "what now"."""
        today = self._now().date()
        if action.scope == "overdue":
            # Everything late, oldest first — the question asks for the list, not for a
            # suggestion of what to start with.
            return agenda_mod.listing(agenda_mod.build(self._index, today,
                                                       self._date_props()).overdue,
                                      texts.VAULT_OVERDUE)
        if action.scope == "now":
            return agenda_mod.listing(agenda_mod.suggest(self._index, today), texts.VAULT_NOW)
        start = _day(action.due_from) or _day(action.due_to) or today
        end = _day(action.due_to) or start
        if end < start:
            start, end = end, start
        items = agenda_mod.on_day(self._index, start, end, self._date_props())
        header = (texts.VAULT_ON_DAY.format(date=start.strftime("%d.%m")) if start == end
                  else texts.VAULT_ON_RANGE.format(start=start.strftime("%d.%m"),
                                                   end=end.strftime("%d.%m")))
        return agenda_mod.listing(items, header)

    def digest(self, today: date | None = None) -> str:
        """The morning message: overdue, today, tomorrow, and the meetings around them. An
        empty string when there is nothing to say — nobody wants "you have 0 tasks"."""
        self._index.refresh()
        day = today or self._now().date()
        text = agenda_mod.digest(agenda_mod.build(self._index, day, self._date_props()), day)
        shopping = agenda_mod.groceries_line(self._index)
        if not shopping:
            return text
        return f"{text}\n\n{shopping}" if text else shopping

    def _write_all(self, actions: list[VaultAction]) -> tuple[list[VaultWrite], list[str]]:
        """What was written, and the error of each action that could not be. One file that
        cannot be written must not hide the ones already on disk, or cost them their Undo."""
        writes: list[VaultWrite] = []
        failed: list[str] = []
        for action in actions:
            try:
                writes.append(self._writer.run(action))
            except (OSError, ValueError) as e:
                log.warning("vault write failed (%s): %s", action.action, e)
                failed.append(type(e).__name__)
        return writes, failed

    def _link_later(self, writes: list[VaultWrite]) -> None:
        if self._linker is None or not writes or not self._linking():
            return
        task = asyncio.create_task(self._link(writes))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _link(self, writes: list[VaultWrite]) -> None:
        for write in writes:
            try:
                await self._linker.link(write)
            except Exception as e:  # never reaches the user: the note is already written
                log.warning("linking %s failed: %s", write.note, type(e).__name__)

    async def undo(self, undos: list[VaultUndo]) -> list[str]:
        """Take every write of one turn back, newest first. Returns the notes that have
        changed since in the lines the turn wrote: those are left as they are."""
        left: list[str] = []
        for undo in reversed(undos):
            if not await asyncio.to_thread(self._writer.undo, undo):
                left.append(PurePosixPath(undo.path).stem)
        return left
