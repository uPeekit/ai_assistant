"""The Obsidian side's interpreter, in stages: intent, then one target, then that target's details.

`Filer` asks one question about everything at once — what is this, where does it go, what does
it say — and the light model answers that unreliably: given a dictated message that names a
list as its destination with the preposition swallowed, it made a note called after the list.
Asked the same things one at a time it does not, because each question leaves no room for the
mistake:

1. *intent* — add / done / change / move / ask. One word.
2. *target* — exactly one key from a list this code builds from the vault: the task file, the
   grocery page, the diary, every folder, the few notes whose names match. One key for the
   whole message: letting the model pick a place per item, or several places, is what failed.
3. *details* — a small schema that belongs to that target. Asked for meetings, the model
   cannot answer with a book.

Everything the model returns is then checked against the message (`_Gate`): a date counts only
when the message names a time, a property value only when its words are in the message or in
the user's guide. The answer comes back as the same raw actions `Filer` produces, so
`filer.check`, the writer and the undo are untouched, and the two readers can be compared on
the same cases (tools/benchmark_filer.py).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic

from app import texts
from app.llm import staged_prompts as P
from app.llm.health import Health, describe
from app.vault.filer import GROCERY_LIST, GUIDE_LINK, FilerError, VaultContext
from app.vault.index import related

log = logging.getLogger(__name__)

# (system prompt, JSON schema, user content) -> (answer, prompt tokens, output tokens)
Ask = Callable[[str, dict, str], Awaitable[tuple[dict, int, int]]]

INTENTS = ("add", "done", "change", "move", "ask", "unclear")
ASK_KINDS = ("day", "overdue", "now", "groceries", "search")
MAX_TOKENS = 2000
MAX_NOTES_SHOWN = 12
MAX_GUIDE_VALUE = 80
TASKS, GROCERIES, DIARY, INBOX, NONE = "t", "g", "d", "x", "none"
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}


_WORD = re.compile(r"[^\W\d_]+")


def tokens(text: str) -> set[str]:
    """Every word of `text`, cut to where Russian endings begin. Unlike `index.stems` this
    keeps short words: a gate that cannot see a three-letter product or month name would
    refuse the user's own words."""
    out = set()
    for raw in _WORD.findall(text.casefold()):
        word = "".join(texts.VAULT_SORT_FOLD.get(c, c) for c in raw)
        out.add(word[:4] if len(word) >= 5 else word[:3])
    return out


_TASK_TAIL = re.compile(r"[\U0001F4C5\U0001F501\u2705].*$")


def _task_words(line: str) -> set[str]:
    """The words of a task line itself: no tags, no due date, no repeat rule."""
    bare = _TASK_TAIL.sub("", line)
    return tokens(" ".join(w for w in bare.split() if not w.startswith("#")))


def _names(message: str, name: str) -> bool:
    """Does the message name this note? Every word of the name has to be there, as the same
    word in some form — not merely a word that starts alike."""
    said = _WORD.findall(message.casefold())
    wanted = _WORD.findall(name.casefold())
    return bool(wanted) and all(any(related(w, word) for word in said) for w in wanted)


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props),
            "properties": props}


def _enum(values) -> dict:
    return {"enum": list(values)}


_PROPS = {"type": "array", "items": _obj({"name": _STRING, "value": _STRING})}


def claude_ask(client: anthropic.AsyncAnthropic, model: str,
               health: Health | None = None) -> Ask:
    """The stages' question, put to Claude with structured output."""
    health = health or Health()

    async def ask(system: str, schema: dict, content: str) -> tuple[dict, int, int]:
        try:
            resp = await client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"format": {"type": "json_schema", "schema": schema}})
        except anthropic.APIError as e:
            raise FilerError(describe(e), health.record(e)) from None
        health.ok()
        if resp.stop_reason == "max_tokens":
            raise FilerError("answer cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise FilerError("not JSON") from None
        if not isinstance(data, dict):
            raise FilerError("not an object")
        usage = getattr(resp, "usage", None)
        return (data, getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0)

    return ask


@dataclass
class _Gate:
    """What a model's answer may carry, judged against the message it was given.

    A model asked for a field tends to supply one. Asked for an author it names one from
    memory — a local model credited Pasternak's novel to Bulgakov — and asked for a due date it
    picks today. Nothing here judges meaning; it only refuses values that have no source."""

    message: str
    guide: str

    def __post_init__(self) -> None:
        self._tokens = tokens(self.message)
        # Whole words that *begin* with a cue: as a substring, the cue for "morning" is
        # inside the word for "inside".
        said = _WORD.findall(self.message.casefold())
        self._timed = any(c.isdigit() for c in self.message) or any(
            word.startswith(cue) for word in said for cue in texts.VAULT_DATE_CUES)
        self._guide = self.guide.casefold()

    def date(self, value: object) -> str:
        text = str(value or "").strip()
        return text if _ISO.match(text) and self._timed else ""

    def said(self, value: str) -> bool:
        """Every word of `value` is in the message (in any form)."""
        wanted = tokens(value)
        return bool(wanted) and wanted <= self._tokens

    def mentions(self, value: str) -> bool:
        """At least one word of `value` is in the message."""
        return bool(tokens(value) & self._tokens)

    def prop(self, value: object) -> str | None:
        """The value to write, "" to leave the property empty, None when it has no source."""
        text = str(value or "").strip()
        if not text:
            return ""
        if text.casefold() in ("true", "false"):
            return text.casefold()
        if _ISO.match(text):
            return text if self._timed else None
        if self.said(text) or (len(text) <= MAX_GUIDE_VALUE and text.casefold() in self._guide):
            return text
        return None

    def heading(self, value: object) -> str:
        """A heading for the task file, only when the user's guide knows it: the writer creates
        a heading that is missing, so an invented one would grow the file a new section."""
        text = str(value or "").strip().strip("#").strip()
        return text if text and text.casefold() in self._guide else ""


class StagedFiler:
    """Same contract as `Filer`: `file(message, ctx)` -> raw actions and the tokens they cost."""

    def __init__(self, ask: Ask, model: str,
                 close: Callable[[], Awaitable[None]] | None = None) -> None:
        self.model = model
        self._ask_model = ask
        self._close = close

    @classmethod
    def claude(cls, api_key: str, model: str, *, timeout_s: float = 30.0,
               client: anthropic.AsyncAnthropic | None = None,
               health: Health | None = None) -> StagedFiler:
        sdk = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s,
                                                 max_retries=1)
        return cls(claude_ask(sdk, model, health), model, sdk.close)

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()

    async def file(self, message: str, ctx: VaultContext) -> tuple[list[dict], int, int]:
        run = _Run(self._ask_model, message, ctx)
        actions = await run.read()
        log.info("staged %s: %s | %d call(s)", self.model, " > ".join(run.trail) or "-", run.calls)
        return actions, run.prompt_tokens, run.output_tokens


class _Run:
    """One message going through the stages."""

    def __init__(self, ask: Ask, message: str, ctx: VaultContext) -> None:
        self._ask_model = ask
        self.message = message
        self.ctx = ctx
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.trail: list[str] = []  # what each stage answered, for the log

    async def _ask(self, system: str, schema: dict, content: str) -> dict:
        data, prompt_tokens, output_tokens = await self._ask_model(system, schema, content)
        self.prompt_tokens += prompt_tokens
        self.output_tokens += output_tokens
        self.calls += 1
        return data

    # ---- context blocks ------------------------------------------------------------------

    def _guide(self) -> str:
        return P.section(P.H_GUIDE, self.ctx.guide)

    def _today(self) -> str:
        return P.section(P.H_TODAY, f"{self.ctx.today} ({self.ctx.weekday})")

    def _notes(self) -> dict[str, str]:
        """The notes a message may be about: the hubs the user's guide links to, and any other
        note the message actually names.

        `known_notes` is a wide net — every note sharing a four-letter word start with the
        message — and offering all of it is how a film to watch was appended to a note that
        merely began with the same letters. A note outside the guide has to be named."""
        ctx = self.ctx
        # Pages with a job of their own are never somewhere to append a thought: the task
        # file and the grocery page have their own stages, the home page is made of queries.
        own = {ctx.tasks_note, texts.VAULT_GROCERIES_NOTE, texts.VAULT_HOME_NOTE,
               texts.VAULT_INBOX_NOTE}
        hubs = [name for name in dict.fromkeys(GUIDE_LINK.findall(ctx.guide))
                if name not in own]
        named = [name for name in ctx.known_notes
                 if name not in hubs and name not in own and _names(self.message, name)]
        return {f"n{i}": name
                for i, name in enumerate([*hubs, *named][:MAX_NOTES_SHOWN], start=1)}

    def _tasks(self) -> dict[str, str]:
        return {f"o{i}": line for i, line in enumerate(self.ctx.open_tasks, start=1)}

    # ---- stage 1 -------------------------------------------------------------------------

    async def read(self) -> list[dict]:
        answer = await self._ask(P.INTENT_PROMPT, _obj({"intent": _enum(INTENTS)}),
                                 P.message(text=self.message))
        intent = str(answer.get("intent", ""))
        self.trail.append(intent or "?")
        if intent == "unclear":
            return [self._inbox(self.message)]
        handler = {"add": self._add, "done": self._done, "change": self._change,
                   "move": self._move, "ask": self._question}.get(intent, self._add)
        actions = await handler(self.message)
        return actions or [self._inbox(self.message)]

    @staticmethod
    def _inbox(text: str) -> dict:
        return {"action": "inbox", "text": text}

    # ---- add: target, then details -------------------------------------------------------

    def _places(self) -> dict[str, str]:
        ctx = self.ctx
        known = (P.PLACE_GROCERIES_KNOWN.format(names=", ".join(ctx.groceries[:20]))
                 if ctx.groceries else "")
        places = {
            TASKS: P.PLACE_TASKS.format(name=ctx.tasks_note),
            GROCERIES: P.PLACE_GROCERIES.format(name=texts.VAULT_GROCERIES_NOTE, known=known),
            DIARY: P.PLACE_DIARY,
        }
        for i, folder in enumerate(ctx.folders, start=1):
            props = ctx.folder_props.get(folder)
            places[f"f{i}"] = P.PLACE_FOLDER.format(
                name=folder,
                props=(P.PLACE_FOLDER_PROPS.format(name=folder, names=", ".join(props))
                       if props else ""))
        for key, name in self._notes().items():
            places[key] = P.PLACE_NOTE.format(name=name)
        places[INBOX] = P.PLACE_INBOX
        return places

    async def _add(self, text: str) -> list[dict]:
        """One target for the whole of `text`, then its details.

        One, always. Three ways of letting a message go to more than one place were tried —
        a target per item, a "several" key here, a leftover handed back by the details
        stage — and each was used on plain messages far more often than on mixed ones: a
        list of chores scattered over three places, a task returned whole as its own
        leftover. A message that really is two notes for two places lands in the first."""
        places = self._places()
        answer = await self._ask(
            P.TARGET_PROMPT, _obj({"target": _enum(places)}),
            P.message(self._guide(), P.section(P.H_PLACES, P.keyed(places)), text=text))
        key = str(answer.get("target", ""))
        self.trail.append(key or "?")
        if key == TASKS:
            return await self._add_tasks(text)
        if key == GROCERIES:
            return await self._groceries(text, done=False)
        if key == DIARY:
            return [{"action": "log", "text": text}]
        if key.startswith("f") and key[1:].isdigit() and int(key[1:]) <= len(self.ctx.folders):
            return await self._add_notes(text, self.ctx.folders[int(key[1:]) - 1])
        note = self._notes().get(key)
        if note is not None:
            return await self._append(text, note)
        return [self._inbox(text)]

    async def _add_tasks(self, text: str) -> list[dict]:
        ctx = self.ctx
        schema = _obj({"items": {"type": "array", "items": _obj({
            "text": _STRING, "due": _STRING, "repeat": _STRING, "heading": _STRING,
            "tag": _STRING, "countdown": {"type": "boolean"}})}})
        answer = await self._ask(
            P.TASKS_PROMPT, schema,
            P.message(self._guide(), self._today(),
                      P.section(P.H_AREAS, ", ".join(ctx.tags)), text=text))
        gate = _Gate(text, ctx.guide)
        known_tags = {t.casefold() for t in ctx.tags}
        out = []
        for item in answer.get("items") or []:
            if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                continue
            wanted = _task_words(str(item["text"]))
            twin = next((line for line in ctx.open_tasks
                         if wanted and _task_words(line) == wanted), None)
            if twin is not None:
                # Already on the list, word for word: point at that line rather than add
                # a second one under it.
                out.append({"action": "update", "note": ctx.tasks_note, "task": twin})
                continue
            tag = str(item.get("tag", "")).strip().lstrip("#")
            repeat = str(item.get("repeat", "")).strip()
            out.append({
                "action": "task", "text": str(item["text"]).strip(),
                "due": gate.date(item.get("due")),
                "repeat": repeat if repeat.lower().startswith("every") else "",
                "heading": gate.heading(item.get("heading")),
                "tags": [tag] if tag.casefold() in known_tags else [],
                "countdown": bool(item.get("countdown")),
            })
        return out

    async def _groceries(self, text: str, *, done: bool) -> list[dict]:
        answer = await self._ask(P.GROCERY_PROMPT, _obj({"names": _STRINGS}),
                                 P.message(text=text))
        gate = _Gate(text, "")
        names = [n.strip() for n in answer.get("names") or []
                 if isinstance(n, str) and n.strip() and gate.said(n)]
        # No names left: filer.check takes the product from the message itself.
        return [{"action": "grocery", "body": names, "done": done}]

    async def _add_notes(self, text: str, folder: str) -> list[dict]:
        ctx = self.ctx
        schema = _obj({"items": {"type": "array", "items": _obj({
            "title": _STRING, "props": _PROPS, "body": _STRINGS, "tags": _STRINGS})}})
        answer = await self._ask(
            P.FOLDER_PROMPT, schema,
            P.message(self._guide(), self._today(), P.section(P.H_FOLDER, folder),
                      P.section(P.H_PROPS, ", ".join(ctx.folder_props.get(folder, []))),
                      P.section(P.H_TAGS, ", ".join(ctx.tags)), text=text))
        gate = _Gate(text, ctx.guide)
        known_tags = {t.casefold() for t in ctx.tags}
        out = []
        for item in answer.get("items") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            # A title none of whose words the user said is a note about something else.
            if not title or not gate.mentions(title):
                continue
            props = []
            for prop in item.get("props") or []:
                name = str((prop or {}).get("name", "")).strip()
                value = gate.prop((prop or {}).get("value"))
                if name and value:
                    props.append({"name": name, "value": value})
            out.append({
                "action": "note", "folder": folder, "title": title, "props": props,
                "body": [str(line) for line in item.get("body") or []],
                "tags": [t for t in (str(x).strip().lstrip("#") for x in item.get("tags") or [])
                         if t.casefold() in known_tags],
            })
        if out:
            return out
        # The folder was chosen and nothing was listed for it: asked for every field at once,
        # the model sometimes takes the one thing named for the folder's own name. One
        # narrower question — what to call the note — before giving the message up to the inbox.
        answer = await self._ask(P.TITLE_PROMPT, _obj({"title": _STRING}),
                                 P.message(P.section(P.H_FOLDER, folder), text=text))
        title = str(answer.get("title", "")).strip()
        if title and gate.said(title):
            self.trail.append("title")
            return [{"action": "note", "folder": folder, "title": title, "props": [],
                     "body": [], "tags": []}]
        return []

    async def _append(self, text: str, note: str) -> list[dict]:
        headings = self.ctx.note_headings.get(note, [])
        answer = await self._ask(
            P.APPEND_PROMPT, _obj({"heading": _STRING, "body": _STRINGS}),
            P.message(P.section(P.H_NOTE, note),
                      P.section(P.H_HEADINGS, "\n".join(headings)), text=text))
        heading = str(answer.get("heading", "")).strip()
        body = [str(line).strip() for line in answer.get("body") or [] if str(line).strip()]
        return [{"action": "append", "note": note,
                 "heading": heading if heading in headings else "",
                 "body": body or [text]}]

    # ---- done ----------------------------------------------------------------------------

    async def _done(self, text: str) -> list[dict]:
        tasks = self._tasks()
        choices = {**{k: P.TASK_LINE.format(line=v) for k, v in tasks.items()},
                   GROCERIES: P.KEY_GROCERIES, NONE: P.KEY_NONE}
        answer = await self._ask(
            P.DONE_PROMPT, _obj({"target": _enum(choices)}),
            P.message(P.section(P.H_CHOICES, P.keyed(choices)), text=text))
        key = str(answer.get("target", ""))
        self.trail.append(key or "?")
        if key in tasks:
            return [{"action": "update", "note": self.ctx.tasks_note, "task": tasks[key],
                     "done": True}]
        if key == GROCERIES:
            return await self._groceries(text, done=True)
        # Done, and not on any list: it is a line about the day.
        return [{"action": "log", "text": text}]

    # ---- change --------------------------------------------------------------------------

    async def _change(self, text: str) -> list[dict]:
        tasks, notes = self._tasks(), self._notes()
        choices = {**{k: P.TASK_LINE.format(line=v) for k, v in tasks.items()},
                   **{k: P.NOTE_LINE.format(name=v) for k, v in notes.items()},
                   NONE: P.KEY_NONE}
        answer = await self._ask(
            P.CHANGE_PROMPT, _obj({"target": _enum(choices)}),
            P.message(P.section(P.H_CHOICES, P.keyed(choices)), text=text))
        key = str(answer.get("target", ""))
        self.trail.append(key or "?")
        if key in tasks:
            return await self._change_task(text, tasks[key])
        if key in notes:
            return await self._change_note(text, notes[key])
        return [self._inbox(text)]

    async def _change_task(self, text: str, line: str) -> list[dict]:
        answer = await self._ask(
            P.CHANGE_TASK_PROMPT, _obj({"due": _STRING, "done": {"type": "boolean"}}),
            P.message(self._today(), P.section(P.H_TASK, line), text=text))
        due = _Gate(text, "").date(answer.get("due"))
        done = answer.get("done") is True
        if not (due or done):
            return [self._inbox(text)]
        action: dict = {"action": "update", "note": self.ctx.tasks_note, "task": line}
        if due:
            action["due"] = due
        if done:
            action["done"] = True
        return [action]

    async def _change_note(self, text: str, note: str) -> list[dict]:
        ctx = self.ctx
        headings = ctx.note_headings.get(note, [])
        answer = await self._ask(
            P.CHANGE_NOTE_PROMPT,
            _obj({"kind": _enum(("props", "rewrite")), "props": _PROPS, "heading": _STRING}),
            P.message(self._guide(), self._today(), P.section(P.H_NOTE, note),
                      P.section(P.H_NOTE_PROPS, ", ".join(ctx.note_props.get(note, []))),
                      P.section(P.H_HEADINGS, "\n".join(headings)), text=text))
        if answer.get("kind") == "props":
            gate = _Gate(text, ctx.guide)
            props = []
            for prop in answer.get("props") or []:
                name = str((prop or {}).get("name", "")).strip()
                value = gate.prop((prop or {}).get("value"))
                if name and value is not None:  # "" is a request to clear the property
                    props.append({"name": name, "value": value})
            if props:
                return [{"action": "update", "note": note, "props": props}]
        heading = str(answer.get("heading", "")).strip()
        return [{"action": "rewrite", "note": note, "text": text,
                 "heading": heading if heading in headings else ""}]

    # ---- move ----------------------------------------------------------------------------

    async def _move(self, text: str) -> list[dict]:
        notes = self._notes()
        if not notes:
            return [self._inbox(text)]
        answer = await self._ask(
            P.MOVE_PROMPT, _obj({"source": _enum(notes), "to": _STRING, "what": _STRING}),
            P.message(P.section(P.H_NOTES, P.keyed(notes)), text=text))
        source = notes.get(str(answer.get("source", "")))
        to = str(answer.get("to", "")).strip()
        # The destination is named by the user, in the message; a name from nowhere is not one.
        if source is None or not to or not _Gate(text, "").mentions(to):
            return [self._inbox(text)]
        return [{"action": "move", "note": source, "to": to,
                 "text": str(answer.get("what", "")).strip() or text}]

    # ---- ask -----------------------------------------------------------------------------

    async def _question(self, text: str) -> list[dict]:
        ctx = self.ctx
        schema = _obj({"kind": _enum(ASK_KINDS), "due_from": _STRING, "due_to": _STRING,
                       "text": _STRING, "folder": _enum(["", *ctx.folders]), "tag": _STRING,
                       "props": _PROPS})
        answer = await self._ask(
            P.ASK_PROMPT, schema,
            P.message(self._guide(), self._today(),
                      P.section(P.H_FOLDERS, ", ".join(ctx.folders)),
                      P.section(P.H_TAGS, ", ".join(ctx.tags)), text=text))
        kind = str(answer.get("kind", ""))
        self.trail.append(kind or "?")
        if kind == "day":
            gate = _Gate(text, "")
            return [{"action": "agenda", "scope": "day",
                     "due_from": gate.date(answer.get("due_from")),
                     "due_to": gate.date(answer.get("due_to"))}]
        if kind in ("overdue", "now"):
            return [{"action": "agenda", "scope": kind}]
        if kind == "groceries":
            return [{"action": "grocery", "scope": GROCERY_LIST, "body": []}]
        tag = str(answer.get("tag", "")).strip().lstrip("#")
        return [{"action": "search", "text": str(answer.get("text", "")).strip() or text,
                 "folder": str(answer.get("folder", "")),
                 "tags": [tag] if tag.casefold() in {t.casefold() for t in ctx.tags} else [],
                 "props": [p for p in answer.get("props") or []
                           if isinstance(p, dict) and str(p.get("name", "")).strip()]}]
