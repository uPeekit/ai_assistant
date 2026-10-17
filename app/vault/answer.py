"""Answering a question from what the vault says, the way an assistant who has read it would.

The word search (app/vault/search.py) finds where the question's words are; this reads those
notes and puts one question to a model: answer from them, and name the notes the answer came
from. The model sees the rest of the vault only as a catalog — one line per note: where it is
and a few of its properties. When the answer is in a note the words did not reach ("which book
did I like most" names no word of a title), it asks for those notes by key and is asked once
more with them. Two calls at most, and the vault never goes to the model whole: a vault a
hundred times larger costs a longer catalog, not a hundred times the text.

The notes are the user's own, but a note can hold what a web search appended to it, so the
prompt treats every note as data, never as instructions."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import combinations

import anthropic

from app.llm import staged_prompts as P
from app.llm.health import Health
from app.vault.filer import FilerError
from app.vault.index import GUIDE_NOTE, Note, VaultIndex
from app.vault.search import Hit
from app.vault.staged import Ask, claude_ask

log = logging.getLogger(__name__)

MAX_NOTE = 8_000  # characters of one note
MAX_READ = 24_000  # characters of notes per call
MAX_FIRST = 8  # notes the search found, read before the model asks for any
MAX_MORE = 6  # notes the model may ask for on top
MAX_CATALOG = 400  # catalog lines; past it the notes the search ranked come first
MAX_PROPS = 3  # properties per catalog line
MAX_PROP = 40  # characters per property value there
MAX_SOURCES = 5
# The folder summary: a property is counted when it has at most this many values in a folder
# (a status, an author) and at least half of the folder's notes carry it.
MAX_VALUES = 8
MIN_FOLDER = 3
MAX_CROSSED = 3  # properties whose values are also counted in pairs
# Characters: a Telegram message the user reads, not a report. Room for a passage an owner's
# instruction asks to be quoted as written rather than retold; sending splits a long one.
MAX_ANSWER = 3_500
MAX_TOKENS = 3_000

_STRING = {"type": "string"}
_KEYS = {"type": "array", "items": _STRING}
# who: the question is about the asker, and the records do not say for sure which of the
# people in them that is; the answer is the question back.
KINDS = ("notes", "general", "web", "none", "who")
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["kind", "options", "answer", "sources", "more", "web"],
          "properties": {"kind": {"enum": list(KINDS)}, "options": _KEYS, "answer": _STRING,
                         "sources": _KEYS, "more": _KEYS, "web": _STRING}}
MAX_OPTIONS = 5
WEB_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
              "properties": {"answer": _STRING}}
MAX_FOUND = 12_000  # characters of web findings an answer is written from
GOOGLE_BUDGET = 12_000  # characters of Google sections read per call
MAX_GOOGLE_LISTED = 300  # names of the other Google sections the model may ask for


@dataclass
class Answer:
    text: str
    sources: list[Note] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    # Where the answer came from: the notes, general knowledge, nowhere ("none"), or — with
    # `web` the query — the web, still to be searched.
    kind: str = "notes"
    web: str = ""
    # The Google documents (app/google/) the answer came from, by name: plain text.
    google: list[str] = field(default_factory=list)
    # kind "who": the names in the records that may be the asker's.
    options: list[str] = field(default_factory=list)


def _value(value: object) -> str:
    """A property as a catalog line shows it: a link by the name it points at, a list joined."""
    if isinstance(value, list):
        return ", ".join(_value(v) for v in value if v not in (None, ""))
    text = str(value).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].split("|")[-1].rsplit("/", 1)[-1]
    return text if len(text) <= MAX_PROP else text[:MAX_PROP - 1] + "…"


def catalog(index: VaultIndex, first: list[Note]) -> dict[str, Note]:
    """Every note under a short key, the ones the search found first. The user's guide to the
    bot is not one of them: it is about the bot, not about anything the user asks."""
    seen = {n.path for n in first}
    rest = sorted((n for n in index.notes if n.path not in seen), key=lambda n: n.path)
    notes = [n for n in [*first, *rest] if n.name != GUIDE_NOTE][:MAX_CATALOG]
    return {f"n{i}": note for i, note in enumerate(notes, start=1)}


def catalog_line(note: Note) -> str:
    where = note.path.removesuffix(".md")
    props = [f"{k}: {_value(v)}" for k, v in note.props.items()
             if k not in ("aliases", "alias", "tags", "tag", "cssclasses")
             and _value(v)][:MAX_PROPS]
    return " · ".join([where, *props])


def folders_summary(notes: list[Note]) -> str:
    """Exact counts per folder: its notes, and how many carry each value of a property. A
    model asked "how many books have I read" counted 21 of 23 down a thirty-line catalog; a
    sum done here is not miscounted."""
    by_folder: dict[str, list[Note]] = {}
    for note in notes:
        if note.folder and note.name != GUIDE_NOTE:
            by_folder.setdefault(note.folder, []).append(note)
    lines = []
    for folder, inside in sorted(by_folder.items()):
        if len(inside) < MIN_FOLDER:
            continue
        counts: dict[str, Counter] = {}
        for note in inside:
            for name, value in note.props.items():
                if name in ("aliases", "alias", "tags", "tag", "cssclasses"):
                    continue
                for one in value if isinstance(value, list) else [value]:
                    if one is None or "://" in str(one):  # an address is not a category
                        continue
                    if (shown := _value(one)):
                        counts.setdefault(name, Counter())[shown] += 1
        parts = [P.SUMMARY_NOTES.format(folder=folder, n=len(inside))]
        # A property worth counting groups notes: few values, most notes carry one, and the
        # values repeat. A meeting's own date is on one note each and says nothing as a count.
        counted = [name for name, counter in counts.items()
                   if len(counter) <= MAX_VALUES and sum(counter.values()) * 2 >= len(inside)
                   and len(counter) * 2 <= sum(counter.values())]
        for name in counted:
            values = ", ".join(f"{v} {n}" for v, n in counts[name].most_common())
            parts.append(f"{name} — {values}")
        lines.append("; ".join(parts))
        # Two properties together ("how many of his books have I read"): the model added a
        # status to an author's books wrongly even with both totals in front of it.
        for one, two in combinations(counted[:MAX_CROSSED], 2):
            pairs = Counter((_value(n.props.get(one, "")), _value(n.props.get(two, "")))
                            for n in inside)
            values = ", ".join(f"{a} + {b} {k}" for (a, b), k in pairs.most_common() if a and b)
            if values:
                lines.append(f"  {one} + {two} — {values}")
    return "\n".join(lines)


def _within(keys: list[str], found: dict, budget: int) -> list[str]:
    """The keys, in order, whose texts fit the budget together."""
    out, left = [], budget
    for key in keys:
        size = len(found[key].text)
        if size > left:
            break
        out.append(key)
        left -= size
    return out


def _google_block(key: str, found) -> str:
    """One Google section as the answerer reads it: its key and name, the owner's instruction
    for that document, then its text."""
    head = f"### {key}: {found.name}"
    if found.instruction.strip():
        head += f"\n{P.GOOGLE_INSTRUCTION}: {found.instruction.strip()}"
    return f"{head}\n{found.text}"


class VaultAnswerer:
    def __init__(self, ask: Ask, model: str,
                 close: Callable[[], Awaitable[None]] | None = None, *,
                 second: VaultAnswerer | None = None) -> None:
        self._ask = ask
        self.model = model
        self._close = close
        # A stronger model, asked only when this one cannot tell who the asker is in the
        # records: Haiku asked people back whose name it had just been told, and offered names
        # nobody had written; Sonnet matched a short form of a name and a quiz nickname.
        self._second = second

    @classmethod
    def claude(cls, api_key: str, model: str, *, timeout_s: float = 60.0,
               health: Health | None = None,
               client: anthropic.AsyncAnthropic | None = None,
               second_model: str = "") -> VaultAnswerer:
        sdk = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s,
                                                 max_retries=1)
        second = (cls(claude_ask(sdk, second_model, health), second_model)
                  if second_model and second_model != model else None)
        return cls(claude_ask(sdk, model, health), model, sdk.close, second=second)

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()

    async def answer(self, question: str, hits: list[Hit], index: VaultIndex,
                     today: str, history: str = "", google: list | tuple = (),
                     asker: str = "") -> Answer:
        """The answer and the notes it came from. Raises FilerError when the model could not
        be asked: the caller then shows what the search found instead."""
        out = await self._answer(question, hits, index, today, history, google, asker)
        if out.kind != "who" or self._second is None:
            return out
        log.info("vault answer: who is asking is unclear to %s; asking %s", self.model,
                 self._second.model)
        try:
            again = await self._second._answer(question, hits, index, today, history, google,
                                               asker)
        except FilerError as e:
            log.warning("the second answer failed (%s); asking the user", e)
            return out
        again.prompt_tokens += out.prompt_tokens
        again.output_tokens += out.output_tokens
        return again

    async def _answer(self, question: str, hits: list[Hit], index: VaultIndex, today: str,
                      history: str, google: list | tuple, asker: str) -> Answer:
        first: list[Note] = []
        for hit in hits:
            note = next((n for n in index.notes if n.path == hit.path), None)
            if note is not None and note not in first:
                first.append(note)
        keyed = catalog(index, first)
        by_path = {n.path: k for k, n in keyed.items()}
        read = [by_path[n.path] for n in first[:MAX_FIRST] if n.path in by_path]
        out = Answer(text="")
        # Every Google section of this chat, best first: the first ones as far as the budget
        # goes are read, the rest are a list of names the model may ask for (`more`), as with
        # the notes — a word count ranks a table's summary below a page that happens to share
        # the question's words, and the name tells the model what it holds.
        found = {f"g{i}": f for i, f in enumerate(google, start=1)}
        # Only what matched the question is read at first; the rest are names to ask for.
        google_read = _within([k for k, f in found.items() if f.score > 0], found,
                              GOOGLE_BUDGET)
        for round_ in range(2):
            texts_, shown = self._texts(index, keyed, read)
            google_text = "\n\n".join(_google_block(k, found[k]) for k in google_read)
            listed = {k: f.name for k, f in found.items() if k not in google_read}
            data, used_in, used_out = await self._ask(
                P.ANSWER_PROMPT, SCHEMA,
                P.message(P.section(P.H_TODAY, today),
                          P.section(P.H_ASKER, asker),
                          P.section(P.H_HISTORY, history),
                          P.section(P.H_SUMMARY, folders_summary(index.notes)),
                          P.section(P.H_CATALOG, P.keyed(
                              {k: catalog_line(n) for k, n in keyed.items()})),
                          P.section(P.H_READ, texts_),
                          P.section(P.H_GOOGLE_LIST, P.keyed(dict(
                              list(listed.items())[:MAX_GOOGLE_LISTED]))),
                          P.section(P.H_GOOGLE, google_text), text=question),
                MAX_TOKENS)
            out.prompt_tokens += used_in
            out.output_tokens += used_out
            asked = list(dict.fromkeys(str(x) for x in data.get("more") or []))
            more = [k for k in asked if k in keyed and k not in shown][:MAX_MORE]
            more_google = [k for k in asked if k in found and k not in google_read][:MAX_MORE]
            if round_ == 0 and (more or more_google):
                # The answer is in what the words did not reach: those first, then what was
                # read already, as far as the budget goes.
                log.info("vault answer: reading %d more note(s), %d google section(s)",
                         len(more), len(more_google))
                read = [*more, *read]
                google_read = _within([*more_google, *google_read], found, GOOGLE_BUDGET)
                continue
            break
        text = str(data.get("answer", "")).strip()
        kind = str(data.get("kind", "notes"))
        out.kind = kind if kind in KINDS else "notes"
        out.web = str(data.get("web", "")).strip() if out.kind == "web" else ""
        if out.kind == "who":
            # The question back is the code's, never the model's words: asked who someone is,
            # a model still wrote the rating of the person it guessed. A name it offers must be
            # one the records it read actually hold — it offered names nobody had written.
            read_text = f"{texts_}\n{google_text}".casefold()
            out.options = [o for o in dict.fromkeys(
                str(o).strip() for o in data.get("options") or [] if str(o).strip())
                if o.casefold() in read_text][:MAX_OPTIONS]
            return out
        if not text and not out.web:
            raise FilerError("empty answer")
        if len(text) > MAX_ANSWER:
            text = text[:MAX_ANSWER - 1].rstrip() + "…"
        out.text = text
        named = list(dict.fromkeys(str(x) for x in data.get("sources") or []))
        out.sources = [keyed[k] for k in named if k in keyed][:MAX_SOURCES]
        out.google = list(dict.fromkeys(found[k].name for k in named if k in found))[
            :MAX_SOURCES]
        return out

    async def from_web(self, question: str, found: str, history: str = "") -> Answer:
        """The answer written from what a web search found. The findings are data."""
        data, used_in, used_out = await self._ask(
            P.WEB_ANSWER_PROMPT, WEB_SCHEMA,
            P.message(P.section(P.H_HISTORY, history),
                      P.section(P.H_FOUND, found[:MAX_FOUND]), text=question), MAX_TOKENS)
        text = str(data.get("answer", "")).strip()
        if not text:
            raise FilerError("empty answer")
        if len(text) > MAX_ANSWER:
            text = text[:MAX_ANSWER - 1].rstrip() + "…"
        return Answer(text=text, kind="web", prompt_tokens=used_in, output_tokens=used_out)

    @staticmethod
    def _texts(index: VaultIndex, keyed: dict[str, Note],
               read: list[str]) -> tuple[str, set[str]]:
        """The notes to show, each under its key, until the budget is spent."""
        blocks: list[str] = []
        shown: set[str] = set()
        left = MAX_READ
        for key in read:
            try:
                text = index.read(keyed[key].path).strip()
            except OSError:
                continue
            if len(text) > MAX_NOTE:
                text = text[:MAX_NOTE] + "\n" + P.NOTE_CUT
            if len(text) > left:
                break
            left -= len(text)
            shown.add(key)
            blocks.append(f"### {key}\n{text}")
        return "\n\n".join(blocks), shown
