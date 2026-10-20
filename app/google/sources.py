"""Which Google files the bot reads for whom, their local copies, and finding what a question
needs in them.

`data/sources.json` (the admin page's sources section): each source is a doc, sheet, form or
folder, the chats that may use it, how a doc in it is cut up (`split`, the owner's choice), and
the owner's instruction about it — how to read it, in their own words. None of that is decided
here: this module only carries the owner's configuration to the model. The owner's private chat
uses every source, and may also search the rest of their Drive; a group uses only the sources
that list it.

Copies live in `data/sources/<file id>.json` as sections (app/google/sections.py). A file is
read again only when Drive says it changed, and Drive is asked at most every CHECK_EVERY; a
folder's listing likewise, and only the files in it that changed are read. A question waits at
most WAIT_S for Google: past that it is answered from the copies there are, and the reading goes
on behind it, so the next question has them."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app import texts
from app.google.client import KIND_OF, GoogleClient
from app.google.errors import GoogleError, SignInNeeded
from app.google.sections import Section, doc_sections, form_sections, sheet_sections
from app.vault.index import overlap, words

log = logging.getLogger(__name__)

KINDS = ("doc", "sheet", "form", "folder")
# How a doc is cut into sections — the owner picks one per source on the admin page.
SPLITS = ("pieces", "headings")
CHECK_EVERY_S = 600.0
WAIT_S = 10.0
MAX_SECTION = 5_000
MAX_RANKED = 400  # sections handed on per question; the answerer reads within its budget
# The shape of a kept copy. A copy of another shape is read again from Google: a change in how
# sections are cut reaches files that have not changed since.
COPY_FORMAT = 4
TITLE_WEIGHT = 3
PARALLEL = 6
SEARCH_RESULTS = 3
NOTICE_EVERY_S = 24 * 3600.0
_LINKS = (
    (re.compile(r"docs\.google\.com/document/d/([\w-]+)"), "doc"),
    (re.compile(r"docs\.google\.com/spreadsheets/d/([\w-]+)"), "sheet"),
    (re.compile(r"docs\.google\.com/forms/d/(?!e/)([\w-]+)"), "form"),
    (re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)"), "folder"),
)


# Each kind's own link, made back from the file id: what the page shows and lets the owner edit.
_OPEN = {"doc": "https://docs.google.com/document/d/{}/edit",
         "sheet": "https://docs.google.com/spreadsheets/d/{}/edit",
         "form": "https://docs.google.com/forms/d/{}/edit",
         "folder": "https://drive.google.com/drive/folders/{}"}


def link_of(file_id: str, kind: str) -> str:
    return _OPEN[kind].format(file_id) if kind in _OPEN else ""


def parse_link(link: str) -> tuple[str, str] | None:
    """(file id, kind) from a Google link. A form's public "/forms/d/e/…" link carries another
    id than the form's own: the editor's link is the one to paste."""
    for pattern, kind in _LINKS:
        m = pattern.search(link)
        if m:
            return m.group(1), kind
    return None


@dataclass(frozen=True)
class Source:
    id: str
    file_id: str
    kind: str
    title: str = ""
    chats: frozenset[int] = frozenset()
    instruction: str = ""
    split: str = "pieces"
    # A sheet's tabs to read; none: every tab. Tabs that repeat the main one (by year, sorted)
    # would otherwise crowd it out of what a question reads.
    tabs: tuple[str, ...] = ()


def _source(raw: dict) -> Source:
    file_id, kind = str(raw.get("file_id", "")).strip(), str(raw.get("kind", "")).strip()
    link = str(raw.get("link", "")).strip()
    if link:
        parsed = parse_link(link)
        if parsed is None:  # a link the owner typed is never quietly ignored
            raise ValueError(f"not a Google doc, sheet, form or folder link: {link[:200]}")
        file_id, kind = parsed
    if not file_id or kind not in KINDS:
        raise ValueError("a source needs a Google doc, sheet, form or folder link")
    chats = raw.get("chats") or []
    if not isinstance(chats, list) or not all(isinstance(c, int) for c in chats):
        raise ValueError("'chats' must be a list of chat ids")
    return Source(id=str(raw.get("id") or file_id), file_id=file_id, kind=kind,
                  title=str(raw.get("title", "")).strip()[:200], chats=frozenset(chats),
                  instruction=str(raw.get("instruction", ""))[:2000],
                  split=str(raw.get("split")) if raw.get("split") in SPLITS else "pieces",
                  tabs=_tabs(raw.get("tabs")))


def _tabs(value: object) -> tuple[str, ...]:
    """Tab names from the page: a comma-separated line or a list."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    return tuple(dict.fromkeys(str(t).strip() for t in items if str(t).strip()))


def _dump(source: Source) -> dict:
    return {"id": source.id, "file_id": source.file_id, "kind": source.kind,
            "title": source.title, "chats": sorted(source.chats),
            "instruction": source.instruction, "split": source.split,
            "tabs": list(source.tabs)}


class Sources:
    """data/sources.json, re-read by mtime. Unreadable: no sources, said once."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sources: list[Source] = []
        self._mtime: float | None = None
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            try:
                stat = self._path.stat()
            except OSError:
                self._sources, self._mtime = [], None
                return
            if self._mtime == stat.st_mtime:
                return
            self._mtime = stat.st_mtime
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError("not a list")
                self._sources = [_source(r) for r in raw if isinstance(r, dict)]
            except (OSError, ValueError) as e:
                log.warning("could not read %s (%s); no Google sources", self._path, e)
                self._sources = []

    def all(self) -> list[Source]:
        self.reload()
        return list(self._sources)

    def for_chat(self, chat_id: int, owner: bool) -> list[Source]:
        return [s for s in self.all() if owner or chat_id in s.chats]

    def dump(self) -> list[dict]:
        """For the page: each source with its link, to check and to edit."""
        return [{**_dump(s), "link": link_of(s.file_id, s.kind)} for s in self.all()]

    def save(self, payload: object) -> int:
        if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
            raise ValueError("'sources' must be a list of objects")
        wanted = [_source(r) for r in payload]
        old = {s.id: s for s in self.all()}
        changed = sum(1 for s in wanted if old.get(s.id) != s) + len(
            set(old) - {s.id for s in wanted})
        if changed:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_text(json.dumps([_dump(s) for s in wanted], ensure_ascii=False,
                                          indent=1), encoding="utf-8")
                tmp.replace(self._path)
                self._sources, self._mtime = wanted, self._path.stat().st_mtime
            log.info("google sources: %d (%d changed)", len(wanted), changed)
        return changed


@dataclass(frozen=True)
class Found:
    """A section of a source, as the answerer is shown it."""

    source: str  # the document's name
    title: str  # the section's
    text: str
    instruction: str = ""
    # How well it matched the question; 0: offered by name only (app/vault/answer.py).
    score: float = 0.0
    # The table the section belongs to (a sheet's tab) and its place among the sections: a
    # plan's read step hands on a whole tab, in its own order.
    group: str = ""
    order: int = 0

    @property
    def name(self) -> str:
        return self.source if self.title in ("", self.source) else f"{self.source} — {self.title}"


@dataclass
class _Copy:
    title: str
    modified: str
    checked: float
    split: str = ""  # the cut its sections were made with: another one means reading again
    sections: list[Section] = field(default_factory=list)
    children: list[dict] = field(default_factory=list)  # a folder's files
    error: str = ""


class GoogleSources:
    def __init__(self, client: GoogleClient, sources: Sources, cache_dir: Path, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._sources = sources
        self._dir = cache_dir
        self._clock = clock
        self._copies: dict[str, _Copy] = {}
        self._reading: dict[str, asyncio.Task] = {}
        self._gate = asyncio.Semaphore(PARALLEL)
        self.sign_in_needed = False
        self._flagged_at: datetime | None = None
        self._noticed_at: float | None = None
        self.last_error = ""

    async def aclose(self) -> None:
        for task in self._reading.values():
            task.cancel()
        await self._client.aclose()

    # ---- copies ------------------------------------------------------------------------------

    def _file(self, file_id: str) -> Path:
        return self._dir / f"{re.sub(r'[^\w-]', '_', file_id)}.json"

    def _load(self, file_id: str) -> _Copy | None:
        copy = self._copies.get(file_id)
        if copy is not None:
            return copy
        try:
            raw = json.loads(self._file(file_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("format") != COPY_FORMAT:
            return None
        copy = _Copy(title=raw.get("title", ""), modified=raw.get("modified", ""), checked=0.0,
                     split=raw.get("split", ""),
                     sections=[Section(s["title"], s["text"], bool(s.get("summary")),
                                       str(s.get("group", "")))
                               for s in raw.get("sections", [])],
                     children=raw.get("children", []), error=raw.get("error", ""))
        self._copies[file_id] = copy
        return copy

    def _store(self, file_id: str, copy: _Copy) -> None:
        self._copies[file_id] = copy
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data = {"format": COPY_FORMAT, "title": copy.title, "modified": copy.modified,
                    "split": copy.split,
                    "read_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "sections": [s.as_dict() for s in copy.sections],
                    "children": copy.children, "error": copy.error}
            tmp = self._file(file_id).with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._file(file_id))
        except OSError as e:
            log.warning("could not keep a copy of a Google file (%s)", type(e).__name__)

    async def _read(self, file_id: str, kind: str, title: str, split: str) -> list[Section]:
        if kind == "doc":
            return doc_sections(await self._client.document(file_id),
                                by_headings=split == "headings")
        if kind == "sheet":
            name, tabs = await self._client.sheet_values(file_id)
            return sheet_sections(name or title, tabs)
        form, responses = await self._client.form(file_id)
        return form_sections(form, responses)

    async def _fresh(self, file_id: str, kind: str, *, known: dict | None = None,
                     split: str = "pieces") -> _Copy | None:
        """The file's copy, read again when it changed or the owner chose another cut.
        `known` is a folder listing's entry, which already says when the file changed: no
        need to ask Drive again."""
        copy = self._load(file_id)
        if copy is not None and kind == "doc" and copy.split != split:
            copy.modified = ""  # cut another way now: read again
        now = self._clock()
        if copy is not None and known is None and copy.modified and \
                now - copy.checked < CHECK_EVERY_S:
            return copy
        async with self._gate:
            try:
                meta = known or await self._client.file(file_id)
                if meta.get("trashed"):
                    raise GoogleError("the file is in the trash", 404)
            except GoogleError as e:
                if e.gone and copy is not None:
                    # Unshared or deleted: its old copy must not be quoted any more.
                    copy.error = str(e)
                    self._store(file_id, copy)
                raise
            modified = meta.get("modifiedTime", "")
            if copy is not None and copy.modified == modified and kind != "folder":
                copy.checked = now
                return copy
            title = meta.get("name", "")
            if kind == "folder":
                children = await self._client.children(file_id)
                fresh = _Copy(title=title, modified=modified, checked=now,
                              children=[{"id": c["id"], "kind": KIND_OF.get(c["mimeType"]),
                                         "name": c.get("name", ""),
                                         "modifiedTime": c.get("modifiedTime", "")}
                                        for c in children])
            else:
                fresh = _Copy(title=title, modified=modified, checked=now, split=split,
                              sections=await self._read(file_id, kind, title, split))
        self._store(file_id, fresh)
        if kind == "folder":
            await asyncio.gather(*(self._child(c, split) for c in fresh.children))
        return fresh

    async def _child(self, child: dict, split: str) -> None:
        copy = self._load(child["id"])
        if copy is not None and copy.modified == child.get("modifiedTime") and (
                child["kind"] != "doc" or copy.split == split):
            copy.checked = self._clock()
            return
        try:
            await self._fresh(child["id"], child["kind"], split=split, known={
                "name": child.get("name", ""), "modifiedTime": child.get("modifiedTime", "")})
        except GoogleError as e:  # one file of the folder: the others are still read
            log.info("a file in a Google folder could not be read (%s)", e)

    def _refresh(self, source: Source) -> asyncio.Task:
        """One reading of a source at a time; the task outlives a question that stops waiting."""
        task = self._reading.get(source.file_id)
        if task is None or task.done():
            task = asyncio.create_task(self._fresh(source.file_id, source.kind,
                                                   split=source.split))
            task.add_done_callback(self._settled)
            self._reading[source.file_id] = task
        return task

    def _settled(self, task: asyncio.Task) -> None:
        """How a reading ended, whoever was still waiting for it: a failure is noted (and its
        exception retrieved) even when the question stopped waiting long before."""
        if task.cancelled():
            return
        error = task.exception()
        if isinstance(error, SignInNeeded):
            if not self.sign_in_needed:
                self._flagged_at = datetime.now(UTC)
            self.sign_in_needed = True
            self.last_error = str(error)
        elif error is not None:
            self.last_error = str(error)
            log.info("google source not refreshed (%s)", type(error).__name__)

    def _signed_in_again(self) -> None:
        """The warning ends only once Google has really given a token since it began — a
        question answered from fresh-enough copies proves nothing."""
        ok = self._client.auth.last_ok
        if self.sign_in_needed and ok is not None and self._flagged_at is not None \
                and ok > self._flagged_at:
            self.sign_in_needed = False
            self.last_error = ""

    def _sections_of(self, source: Source) -> list[tuple[str, Section]]:
        """(document name, section) of a source's copy as it stands — a folder's files too."""
        copy = self._load(source.file_id)
        if copy is None or copy.error:
            return []
        if source.kind != "folder":
            wanted = {t.casefold() for t in source.tabs}
            return [(source.title or copy.title, s) for s in copy.sections
                    if not wanted or source.kind != "sheet" or s.group.casefold() in wanted]
        out = []
        for child in copy.children:
            inner = self._load(child["id"])
            if inner is not None and not inner.error:
                out += [(child.get("name") or inner.title, s) for s in inner.sections]
        return out

    async def warm(self) -> None:
        """Read every source once, behind the bot's start: the first question finds them."""
        for source in self._sources.all():
            try:
                await self._refresh(source)
            except Exception as e:  # noqa: BLE001 — a start-up warm-up never stops the bot
                log.info("google source %s not read yet (%s)", source.id, type(e).__name__)

    # ---- finding ------------------------------------------------------------------------------

    async def find(self, query: str, chat_id: int, *, owner: bool) -> list[Found]:
        """The sections of this chat's sources that best match `query`, within BUDGET. Never
        raises: Google failing leaves the copies there are."""
        sources = self._sources.for_chat(chat_id, owner)
        tasks = [self._refresh(s) for s in sources]
        searched: list[tuple[str, Section]] = []
        if owner:
            search = asyncio.create_task(self._drive_search(query, sources, searched))
            search.add_done_callback(self._settled)
            tasks.append(search)
        if tasks:
            await asyncio.wait(tasks, timeout=WAIT_S)
        self._signed_in_again()
        candidates: list[tuple[str, str, Section]] = []
        for source in sources:
            candidates += [(name, source.instruction, s) for name, s in self._sections_of(source)]
        candidates += [(name, "", s) for name, s in searched]
        return _ranked(query, candidates)

    async def _drive_search(self, query: str, sources: list[Source],
                            into: list[tuple[str, Section]]) -> None:
        """The owner's whole Drive: files whose text holds the question's main words."""
        main = sorted({w for w in re.findall(r"[^\W\d_]{4,}", query.casefold())
                       if w not in texts.SEARCH_STOP_WORDS}, key=len, reverse=True)[:2]
        # A file that is a source already, or in a source's folder, is not found twice — and
        # its copy keeps the cut its source asked for.
        listed = {s.file_id for s in sources}
        for source in sources:
            folder = self._load(source.file_id) if source.kind == "folder" else None
            listed |= {c["id"] for c in (folder.children if folder else [])}
        for meta in await self._client.search(main, SEARCH_RESULTS):
            if meta["id"] in listed:
                continue
            kind = KIND_OF.get(meta.get("mimeType", ""))
            known = self._load(meta["id"])
            copy = await self._fresh(meta["id"], kind, known=meta,
                                     split=known.split if known and known.split else "pieces")
            if copy is not None and not copy.error:
                into += [(copy.title, s) for s in copy.sections]

    def sign_in_notice(self) -> str:
        """What to tell the owner, at most once a day, when the sign-in stopped working."""
        if not self.sign_in_needed:
            return ""
        now = self._clock()
        if self._noticed_at is not None and now - self._noticed_at < NOTICE_EVERY_S:
            return ""
        self._noticed_at = now
        return texts.GOOGLE_SIGN_IN

    def status(self) -> dict:
        """For the admin page: each source's copy as it stands."""
        out = []
        for source in self._sources.all():
            copy = self._load(source.file_id)
            sections = len(self._sections_of(source)) if copy else 0
            out.append({"id": source.id, "title": source.title or (copy.title if copy else ""),
                        "sections": sections, "files": len(copy.children) if copy else 0,
                        "error": copy.error if copy else ""})
        return {"sign_in_needed": self.sign_in_needed, "error": self.last_error,
                "sources": out}


def _ranked(query: str, candidates: list[tuple[str, str, Section]]) -> list[Found]:
    wanted = {w for w in words(query) if w not in texts.SEARCH_STOP_WORDS}
    scored = []
    in_text: dict[int, int] = {}
    for name, instruction, section in candidates:
        # The owner's instruction describes the source in their words ("ratings of the club"):
        # it names what the file is about when its own titles do not.
        about = f"{name} {section.title} {instruction}"
        in_text[id(section)] = len(overlap(wanted, words(section.text)))
        score = TITLE_WEIGHT * len(overlap(wanted, words(about))) + in_text[id(section)]
        if score:
            scored.append((score, name, instruction, section))
    # A table's summary answers "the best", "the most" — what no block of its rows can — and
    # goes next to the best block of its own table, whenever that table counts: above it when
    # the question names nothing in the rows, just below it when it does (a book, a person),
    # since then that block holds the answer and a file's other tabs' summaries must not
    # push it out of what the answerer reads.
    best: dict[tuple[str, str], tuple[float, int]] = {}
    for score, name, _, section in scored:
        key = (name, section.group)
        if not section.summary and score > best.get(key, (0, 0))[0]:
            best[key] = (score, in_text[id(section)])
    for name, instruction, section in candidates:
        if section.summary and (name, section.group) in best and not any(
                s is section for _, _, _, s in scored):
            scored.append((0, name, instruction, section))

    def placed(score: float, name: str, section: Section) -> float:
        top = best.get((name, section.group))
        if not section.summary or top is None:
            return score
        return top[0] - 0.25 if top[1] else top[0] + 0.5

    scored = [(placed(score, name, section), name, instruction, section)
              for score, name, instruction, section in scored]
    scored.sort(key=lambda s: -s[0])
    # Every section, best first: the answerer reads the first ones within its budget and is
    # shown the rest by name (app/vault/answer.py).
    ranked = {id(s) for _, _, _, s in scored}
    rest = [(0, n, i, s) for n, i, s in candidates if id(s) not in ranked]
    place = {id(s): i for i, (_, _, s) in enumerate(candidates)}
    return [Found(source=name, title=section.title, text=_clip(section.text),
                  group=section.group, order=place[id(section)],
                  instruction=instruction, score=score)
            for score, name, instruction, section in [*scored, *rest]][:MAX_RANKED]


def _clip(text: str) -> str:
    """A section as long as one may be, saying so when it was cut."""
    return text if len(text) <= MAX_SECTION else f"{text[:MAX_SECTION]}\n{texts.GOOGLE_CUT}"
