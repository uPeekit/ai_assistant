"""What the bot does with a message, end to end: the interpreter (the filer), its check, the
writer, and undo.

It never asks the user anything and never fails a message: a model that is down, an action that
does not check out, a note that is gone — all of it ends as a line in the inbox note or as one
line in the reply saying nothing was written."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
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
from app.vault.planner import MAX_RESEARCH, Plan, Step, VaultPlanner
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
# A plan step's inputs, each: what the steps it needs produced, cut so one big search cannot
# crowd the rest out of the prompts.
MAX_INPUT = 6_000
# All of a write or ask step's inputs together: they are shown in every stage prompt.
MAX_STEP_INPUTS = 12_000
# A read step's result (a whole table) and a derive step's inputs together: the strong model
# reads them once.
MAX_READ = 40_000
MAX_READ_NOTES = 10_000  # of a read: the room kept for the notes it finds
MAX_DERIVE_INPUTS = 60_000
MAX_PLAN_GUIDE = 3_000
MAX_TITLE = 60


def with_content(actions: list[VaultAction], content: str, message: str, *,
                 carriers: tuple[str, ...] = CARRIERS, extra: bool = True) -> list[VaultAction]:
    """Text a plan step has already produced, put into whichever action files it.

    The filer chose the place; the words are not its to rewrite, and it never saw them —
    what a web search brought back is data, and data does not go into a prompt. When the
    filer picked nothing that can hold a body, the text becomes a note of its own rather
    than a truncated line in the inbox."""
    lines = content.splitlines()
    out, used = [], False
    for action in actions:
        if not used and action.action in carriers:
            action = action.model_copy(update={"body": lines})
            used = True
        out.append(action)
    if not used and extra:
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
    # A message run as a plan: its answer is the reply, without a line per write.
    planned: bool = False
    # A plan's step whose answerer has to ask who the user is: the question, for the plan.
    who: str = ""
    # What the answerer drew on (notes, general, web …): only notes count as a source later.
    answered: str = ""

    @property
    def undos(self) -> list[VaultUndo]:
        return [w.undo for w in self.writes if w.undo is not None]

    def reply_line(self) -> str:
        if self.planned and self.answer and not self.writes:
            return self.answer
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
                 askers: Askers | None = None,
                 planner: VaultPlanner | None = None) -> None:
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
        # Messages of several steps (app/vault/planner.py); None: every message is one step.
        self._planner = planner
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
                     research: Research | None = None, links: tuple = (),
                     answer: Callable[[str, list], Awaitable[str]] | None = None,
                     chat: ChatRef | None = None, history: str = "", answers: tuple = (),
                     pointed: tuple = (),
                     progress: Callable[[str], Awaitable[None]] | None = None,
                     plain: bool = False, inputs: str = "", source: str = "",
                     facts: str = "") -> VaultTurn:
        """Read the message, write the vault, and start the linking behind the reply.

        `content` is text the caller already has (a plan step's research): the filer still
        decides where it goes, but it is written as it stands.

        `research` runs a web search for an action that asks for one (the staged reader
        decides that on its own)."""
        try:
            # The answer to the bot's «who are you in the records»: their name is kept, and
            # the question they asked first is the one to answer now.
            # A plan's step never: the plan asks, with the whole message (_plan_turn).
            first = self._askers.reply(chat.chat_id, chat.user_id, message) \
                if chat is not None and not plain else None
            message = first or message
            if not plain:  # a plan refreshes once per round, not each step on its own
                await self._refreshed()
            tally = [0, 0]
            plan_ctx = filtering = None
            if (self._planner is not None and not plain and not content
                    and message.strip()
                    and (self._scope is None or self._scope.may("ask"))):
                # The quick filter runs beside the reader: a one-step message waits for
                # neither, and the reader's answer is dropped when a plan comes.
                plan_ctx = self._plan_context(history)
                filtering = asyncio.create_task(self._planner.several(message, plan_ctx, tally))
                filtering.add_done_callback(_retrieved)
            ctx = replace(context(self._index, message, self._now()), links=tuple(links))
            pending = await asyncio.to_thread(self._pending, chat)
            ctx = replace(ctx, inputs=inputs, source=source, facts=facts,
                          reminders=tuple(self._listed(r) for r in pending),
                          now_time=self._local().strftime("%H:%M"), history=history,
                          answers=tuple(answers), pointed=tuple(pointed),
                          web_words=tuple(self._web_words()))
            filing = asyncio.create_task(self._filer.file(message, ctx))
            filing.add_done_callback(_retrieved)
            if filtering is not None:
                assert self._planner is not None and plan_ctx is not None
                try:
                    plan = await self._planner.plan(message, plan_ctx, tally) \
                        if await filtering else None
                except BaseException:
                    filing.cancel()  # nothing of this message goes on unattended
                    raise
                if plan is not None:
                    filing.cancel()
                    # Waited for, not awaited: the reader's own cancellation stays its own,
                    # and this message's (at shutdown) still reaches here.
                    await asyncio.wait({filing})
                    if not filing.cancelled() and filing.exception() is None:
                        _, used_in, used_out = filing.result()  # spent, so counted
                        tally[0] += used_in
                        tally[1] += used_out
                    return await self._run_plan(
                        message, plan, tally, research=research, links=links, chat=chat,
                        history=history, progress=progress, answers=answers, pointed=pointed,
                        answer=answer)
            raw, prompt_tokens, output_tokens = await filing
            prompt_tokens += tally[0]
            output_tokens += tally[1]
        except FilerError as e:
            log.warning("filer failed: %s", e)
            if plain:  # a plan's step: its failure is reported by the plan, nothing kept here
                return VaultTurn(error=str(e), reason=e.reason, model=self._filer.model)
            return await self._kept(message, e)
        except OSError as e:
            log.warning("vault unreadable: %s", e)
            return VaultTurn(error=type(e).__name__)
        model = self._filer.model
        actions = check(raw, self._index, message)
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
        if plain:
            # A step the stages could not place: the planner's words never go to the inbox.
            if actions and all(a.action == "inbox" for a in actions):
                return VaultTurn(error=texts.PLAN_STEP_UNCLEAR, model=model)
            actions = [a for a in actions if a.action != "inbox"]
        actions, refused = self._gated(actions)
        if self._scope is not None and not actions:
            # A group chat never falls back to the owner's inbox: it is told what it may not
            # do here, or that the bot did not understand, and nothing is written.
            return VaultTurn(answer=_not_here(refused), model=model,
                             prompt_tokens=prompt_tokens, output_tokens=output_tokens)
        if not actions and any(isinstance(r, dict) and r.get("looked_up") for r in raw):
            # Every item of the set the user asked for is already in the vault, and check()
            # leaves those alone. That is an answer, not a message nobody understood.
            log.info("vault: everything looked up is already there")
            return VaultTurn(answer=texts.VAULT_ALL_THERE, model=model,
                             prompt_tokens=prompt_tokens, output_tokens=output_tokens)
        if not actions:  # the model answered nothing usable: keep the words rather than drop them
            if plain:
                return VaultTurn(error=texts.PLAN_STEP_UNCLEAR, model=model)
            actions = [VaultAction(action="inbox", text=message)]
        if content:
            # A plan's step puts found text only where a body belongs, and never makes a note
            # of its own from the step's words.
            actions = (with_content(actions, content, message, carriers=("note", "append"),
                                    extra=False) if plain
                       else with_content(actions, content, message))
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
                                        words=question.text, replied=first is not None,
                                        inputs=inputs, plain=plain,
                                        about_records=bool(question.folder or question.tags))
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

    # ---- plans: one message, several steps ----------------------------------------------------

    def _plan_context(self, history: str) -> str:
        """What the filter and the planner see besides the message: the guide, the folders,
        the recent conversation."""
        guide = self._index.guide()
        return "\n\n".join(p for p in (
            P.section(P.H_GUIDE, guide[:MAX_PLAN_GUIDE]),
            P.section(P.H_FOLDERS, ", ".join(self._index.folders())),
            P.section(P.H_HISTORY, history)) if p)

    async def _run_plan(self, message: str, plan: Plan, tally: list[int], *,
                        research: Research | None, links: tuple, chat: ChatRef | None,
                        history: str, progress: Callable[[str], Awaitable[None]] | None,
                        answers: tuple = (), pointed: tuple = (),
                        answer: Callable[[str, list], Awaitable[str]] | None = None,
                        ) -> VaultTurn:
        """The message as a plan of linked steps. Steps run in rounds — each round only needs
        earlier ones, its steps together — and everything they write and say is one turn, so
        the reply and the Undo cover the whole message."""
        log.info("plan: %d steps (%s)", len(plan.steps), ", ".join(s.kind for s in plan.steps))
        merged = VaultTurn(planned=True, model=self._filer.model)
        if research is not None:
            # One budget of searches for the whole message, steps' own lookups included.
            left = [MAX_RESEARCH]
            asked: set[tuple[str, str]] = set()
            outer = research

            async def research(request: str, query: str, media: str = "text") -> str:
                # The same search again is shared, not run: it takes no second slot.
                if (request, media) not in asked:
                    if left[0] <= 0:
                        raise ResearchError(texts.PLAN_SEARCHES_SPENT)
                    left[0] -= 1
                    asked.add((request, media))
                return await outer(request, query, media)

        results: dict[int, str] = {}
        facts: set[int] = set()  # the results a later write may take values from
        failed: list[tuple[Step, str]] = []
        noted: dict[int, str] = {}  # what a write step said besides its writes (a reminder set)
        who = ""
        kinds = {s.id: s.kind for s in plan.steps}
        for n, layer in enumerate(plan.layers()):
            if n:  # what the round before wrote is in the index for this one
                await self._refreshed()
            outs = await asyncio.gather(*(
                self._plan_step(step, results, facts, {s.id for s, _ in failed}, message,
                                kinds, research=research, links=links, chat=chat,
                                history=history, progress=progress, answers=answers,
                                pointed=pointed, answer=answer, tally=tally)
                for step in layer), return_exceptions=True)
            for step, out in zip(layer, outs, strict=True):
                if isinstance(out, BaseException):
                    # Unexpected: the step is a failed part; what other steps wrote stays,
                    # with its Undo.
                    log.error("plan step %d (%s) raised", step.id, step.kind, exc_info=out)
                    failed.append((step, ""))
                    continue
                text, sub_turn, why, fact = out
                if sub_turn is not None:
                    merged.writes += sub_turn.writes
                    merged.reminders += sub_turn.reminders
                    merged.prompt_tokens += sub_turn.prompt_tokens
                    merged.output_tokens += sub_turn.output_tokens
                    who = who or sub_turn.who
                    if step.kind == "write" and sub_turn.answer and text is not None:
                        noted[step.id] = sub_turn.answer
                if text is None:
                    failed.append((step, why))
                else:
                    results[step.id] = text
                    if fact:
                        facts.add(step.id)
        if who and chat is not None:
            # The answerer needs to know who asks: asked with the whole message, so the name
            # the user gives next replays all of it.
            self._askers.asked(chat.chat_id, chat.user_id, message)
        # The writes are listed by the reply itself, each with its link: a write step's own
        # "written: …" line as the reply said one thing of several written.
        # A question no later step takes on is the user's too, marked as the reply or not: two
        # questions planned apart, only the second marked, lost the first answer.
        taken = {n for s in plan.steps for n in s.needs}
        said = [results[s.id] for s in plan.steps if s.id in results and (
                (s.reply and s.kind != "write") or (s.kind == "ask" and s.id not in taken))]
        last = plan.steps[-1]
        if not any(s.reply for s in plan.steps) and last.id in results \
                and last.kind in ("derive", "ask"):
            said = [results[last.id]]  # no step marked: the last one's answer is the reply
        lines = [*([who] if who else []), *said,
                 *(noted[s.id] for s in plan.steps if s.id in noted),
                 *(texts.PLAN_PART_FAILED_WHY.format(what=s.text, why=why) if why
                   else texts.PLAN_PART_FAILED.format(what=s.text) for s, why in failed
                   if not (who and s.kind == "ask"))]
        if any(s.kind == "write" for s, _ in failed):
            # A part that should have written did not: the owner's own words are kept, once.
            kept = await self._kept(message, FilerError("a plan step failed"))
            merged.writes += kept.writes
            if kept.writes:
                lines.append(texts.PLAN_KEPT)
        merged.answer = "\n\n".join(lines)
        merged.said = "\n\n".join(said)
        merged.prompt_tokens += tally[0]
        merged.output_tokens += tally[1]
        return merged

    async def _plan_step(self, step: Step, results: dict[int, str], facts: set[int],
                         failed: set[int], message: str, kinds: dict[int, str], *,
                         research: Research | None, links: tuple, chat: ChatRef | None,
                         history: str, progress: Callable[[str], Awaitable[None]] | None,
                         answers: tuple, pointed: tuple,
                         answer: Callable[[str, list], Awaitable[str]] | None,
                         tally: list[int],
                         ) -> tuple[str | None, VaultTurn | None, str, bool]:
        """(what the step produced — None when it failed —, the turn it wrote in, why it
        failed, whether its result may be a source of values for later writes)."""
        if any(n in failed for n in step.needs):
            return None, None, "", False
        if step.kind == "derive":
            inputs = _handed(results, step.needs, kinds, each=MAX_INPUT,
                             total=MAX_DERIVE_INPUTS)
        else:
            inputs = _handed(results, step.needs, kinds, each=MAX_INPUT,
                             total=MAX_STEP_INPUTS)
        handed_facts = _handed(results, tuple(n for n in step.needs if n in facts), kinds,
                               each=MAX_INPUT, total=MAX_STEP_INPUTS)
        try:
            if step.kind in ("write", "ask"):
                # Found and derived text a write is handed is written as it stands, as on the
                # one-step path: a model retelling a search into note lines loses most of it.
                content = "\n\n".join(results[n] for n in step.needs
                                       if kinds.get(n) in ("research", "derive")) \
                    if step.kind == "write" else ""
                sub_turn = await self.handle(
                    step.text, plain=True, inputs=inputs, source=message, facts=handed_facts,
                    content=content, research=research, links=links, chat=chat,
                    history=history, progress=progress, answers=answers, pointed=pointed,
                    answer=answer)
                why = texts.LLM_DOWN_SHORT.get(sub_turn.reason) or sub_turn.error
                if step.kind == "write":
                    # Done when it wrote, set a reminder, or answered (a reminder listed,
                    # everything already there) without an error.
                    done = sub_turn.writes or sub_turn.reminders or (
                        sub_turn.answer and not sub_turn.error)
                    text = ("\n".join(texts.PLAN_STEP_WROTE.format(what=w.what)
                                      for w in sub_turn.writes) or sub_turn.answer) \
                        if done else None
                    return text or None, sub_turn, why or sub_turn.answer, True
                if sub_turn.who:
                    return None, sub_turn, "", False
                text = sub_turn.said or sub_turn.answer or None
                # Only what the notes and documents say is a source; the model's own
                # knowledge or a web answer is data to read, not a value to write.
                return text, sub_turn, why, sub_turn.answered == "notes"
            if step.kind == "read":
                found = await self._read(step.text, chat)
                return found or None, None, "", True
            if step.kind == "research":
                if research is None:
                    return None, None, "", False
                # The step's own words as the request: the shared research keys on it, and
                # every step asked with the whole message would have been one search.
                found = await research(step.text, step.text, "text")
                return found or None, None, "", True
            assert self._planner is not None
            # The reply of a plan that writes is short: the details are on the page.
            text = (f"{step.text}\n{P.PLAN_REPLY_SHORT}"
                    if step.reply and "write" in kinds.values() else step.text)
            return await self._planner.derive(text, inputs, tally), None, "", True
        except (FilerError, ResearchError, ResearchQuestion) as e:
            log.warning("plan step %d (%s) failed: %s", step.id, step.kind, e)
            reason = getattr(e, "reason", "")
            if str(e) == texts.PLAN_SEARCHES_SPENT:
                return None, None, texts.PLAN_SEARCHES_SPENT, False
            return None, None, texts.LLM_DOWN_SHORT.get(reason, "") if reason else "", False

    async def _read(self, text: str, chat: ChatRef | None) -> str:
        """A plan's read step: the owner's records that match `text`, whole, as data. The best
        matching table is handed on tab by tab in its own order, a document by its best parts,
        then the notes the vault search finds — within MAX_READ."""
        parts: list[str] = []
        notes = await asyncio.to_thread(self._read_notes, text)
        # A share is kept for the notes: a table matched on one common word would otherwise
        # fill the whole read and push out the notes that were meant.
        room = MAX_READ - min(len("\n\n".join(notes)), MAX_READ_NOTES)
        if self._google is not None and chat is not None:
            found = await self._google.find(text, chat.chat_id,
                                            owner=chat.owner and chat.chat_id > 0)
            top = next((f for f in found if f.score > 0), None)
            if top is not None:
                if top.group:
                    picked = sorted((f for f in found if f.source == top.source
                                     and f.group == top.group), key=lambda f: f.order)
                else:
                    picked = [f for f in found if f.source == top.source and f.score > 0]
                parts.append(_cut("\n\n".join(f"### {f.name}\n{f.text}" for f in picked),
                                  room))
        return _cut("\n\n".join([*parts, *notes]), MAX_READ)

    def _read_notes(self, text: str) -> list[str]:
        out = []
        for hit in search(self._index, text, limit=MAX_HITS):
            note = self._index.get(hit.path)
            if note is not None:
                with suppress(OSError):
                    out.append(f"### {note.name}\n{self._index.read(note.path)}")
        return out

    async def _kept(self, message: str, error: FilerError) -> VaultTurn:
        """The model could not be asked — Claude is down, out of credit, or answered rubbish.
        The words go to the inbox note as they are, which takes no model. The reply says both
        things, that they are saved and why they are not filed."""
        turn = VaultTurn(error=str(error), reason=error.reason, model=self._filer.model)
        if self._scope is not None:
            return turn  # the inbox is the owner's: a group's words are not kept there
        if not message.strip():
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
                        words: str = "", replied: bool = False, inputs: str = "",
                        plain: bool = False, about_records: bool = False) -> str:
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
        known = self._askers.known(chat.user_id) if chat is not None else ()
        if self._google is not None and chat is not None:
            # A private chat of an owner sees every source and the Drive; a group only its own.
            query = f"{question} {words}" if words and words not in question else question
            # The names the asker goes by in the records, when the question is about them: «my
            # highest rating» finds their own column of a sheet by the name, which the
            # question does not carry — and only then: on any other question their column
            # took the room the rows asked about needed.
            said = set(re.findall(r"[^\W\d_]+", question.casefold()))
            if said & set(texts.ASKER_SELF_WORDS):
                query = " ".join([query, *(n for n in known if n not in query)])
            google = await self._google.find(query, chat.chat_id,
                                             owner=chat.owner and chat.chat_id > 0)
        try:
            # Said inside the question, not only beside it: told who the asker is in a context
            # line, a model still asked them again.
            asked = P.ASKER_IN_QUESTION.format(question=question, known=P.names(known)) \
                if known else question
            found = await self._answerer.answer(asked, hits, self._index, today,
                                                history=history, google=google,
                                                asker=self._asker(chat), inputs=inputs,
                                                about_records=about_records)
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
            if plain:
                # A plan's step: the plan asks, with the whole message, so the name the user
                # gives replays all of it (_plan_turn).
                turn.who = (texts.ANSWER_WHO_OPTIONS.format(
                    lead="", options=", ".join(f"«{o}»" for o in found.options))
                    if found.options else texts.ANSWER_WHO.format(lead=""))
                return ""
            if replied:
                # Told and still not found: asking again would go round in circles.
                return texts.ANSWER_WHO_NOT_FOUND.format(
                    names=", ".join(f"«{n}»" for n in self._askers.given(chat.user_id)))
            # Names they gave before are not in these records (a quiz under a joke name): ask
            # once more, saying so.
            lead = texts.WHO_NOT_HERE.format(names=", ".join(f"«{n}»" for n in known)) \
                if known else ""
            self._askers.asked(chat.chat_id, chat.user_id, question)
            return (texts.ANSWER_WHO_OPTIONS.format(
                lead=lead, options=", ".join(f"«{o}»" for o in found.options))
                if found.options else texts.ANSWER_WHO.format(lead=lead))
        turn.answered = found.kind
        if found.kind == "notes":
            turn.said = found.text
        lines = [texts.VAULT_ANSWER.format(answer=found.text)]
        # Which notes and Google parts an answer came from is for the log: links in a reply
        # are for what was made, and a line of the notes read told the owner little.
        if found.sources:
            log.info("vault answer from notes: %s",
                     "; ".join(note.name for note in found.sources))
        if found.google:
            log.info("vault answer from google: %s", "; ".join(found.google))
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
        """A question about a page the user sent, answered from the page; nothing written."""
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


def _cut(text: str, limit: int = MAX_INPUT) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _handed(results: dict[int, str], needs: tuple[int, ...], kinds: dict[int, str], *,
            each: int, total: int) -> str:
    """The results a step needs, as one block within its caps; a read result may be long."""
    parts = [f"#{n}: {_cut(results[n], MAX_READ if kinds.get(n) == 'read' else each)}"
             for n in needs]
    return _cut("\n\n".join(parts), total)


def _retrieved(task: asyncio.Task) -> None:
    """A task this message may drop: its failure is retrieved, never logged as forgotten."""
    if not task.cancelled():
        task.exception()
