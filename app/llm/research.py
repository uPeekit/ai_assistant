"""Web research: Claude with three tools — Anthropic's web search, a known shop's or
real-estate site's own search (app/web/sites.py), and reading a page (app/web/reader.py,
trimmed by app/web/trim.py) — in a loop this module drives, with a budget it enforces. The
interpreter only decides *that* something is to be looked up (Candidate.web_query); this turns
the query into Markdown with sources, which then goes through the normal write path as the
candidate's content."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any

import anthropic

from app.llm.health import Health, describe
from app.llm.image_search import ImageSearch
from app.llm.prompts import (
    ALREADY_READ,
    FORCE_ANSWER,
    IMAGE_FILTER_PROMPT,
    IMAGE_QUERIES_PROMPT,
    IMAGES_HEADING,
    READ_FAILED,
    READ_REFUSED,
    READ_TOOL,
    READS_LEFT,
    RESEARCH_QUESTION,
    SOURCES_HEADING,
    TOOL_INPUT_ERROR,
    image_filter_message,
    research_loop_prompt,
    research_message,
    site_search_tool,
)
from app.web.reader import PageUnreadable, Reader
from app.web.sites import RENT, SALE, SITES, search_url
from app.web.trim import relevant

log = logging.getLogger(__name__)

# The backstop behind the soft deadline: the loop answers by itself once its budget or
# RESEARCH_SOFT_DEADLINE_S is spent, so this only fires when a single call hangs.
DEADLINE_S = 180.0
MAX_RESULT_CHARS = 20_000
PREAMBLE_CHARS = 300  # how far into the answer a lead-in before the first heading may run
_FIRST_HEADING = re.compile(r"(?<![\w#])(#{1,3} )")  # "C# " is not a heading
MAX_TURNS = 12  # model calls in one lookup, pause_turn resumes included; the budget ends it sooner
MAX_TOKENS = 8192  # thinking and the answer together; the answer is capped at MAX_RESULT_CHARS
_EFFORT_PREFIXES = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-mythos-5",
                    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")
# Dollars per million tokens (input, output), for the log line's estimate; a search is $0.01.
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-5": (2.0, 10.0),
          "claude-sonnet-4-6": (3.0, 15.0), "claude-opus-5": (5.0, 25.0)}
SEARCH_PRICE = 0.01


MAX_IMAGES = 6
MAX_PHRASES = 3
MAX_SOURCE_PAGES = 4
MAX_CANDIDATES = 16  # pictures shown to the relevance check
KEEP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["keep"],
               "properties": {"keep": {"type": "array", "items": {"type": "integer"}}}}
# "Letter signed Sara ... (page 5).jpg" and "(page 2)" are one document: compared without the
# page numbers and other digits, its pages collapse into one picture.
_PAGE_MARK = re.compile(r"\(?page\s*\d+\)?|\d+", re.I)
_LINK = re.compile(r"\((https?://[^\s)]+)\)")


class ResearchError(Exception):
    """`reason` is a code from app/llm/health.py when the call failed because Claude
    could not be used at all, and "" for every other failure."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class ResearchTimeout(ResearchError):
    """The search ran past its deadline. Different from "found nothing": the user is told that
    it was cut short, not that the web is empty."""


class ResearchQuestion(Exception):
    """The request cannot be looked up as it stands; `question` is for the user."""

    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


def _with_extra(prompt: str, extra: str) -> str:
    """The user's extra instructions, appended to a system prompt. They add to it; they never
    replace it, so the rules the code depends on still hold."""
    return f"{prompt}\n\n{extra.strip()}" if extra.strip() else prompt


def final_text(blocks: list[Any]) -> str:
    """The answer proper: the text after the last tool call or result. Text before it is the
    model narrating its search ("let me look that up"), which does not belong in the note."""
    last_tool = max((i for i, b in enumerate(blocks)
                     if b.type.endswith("_tool_result")
                     or b.type in ("tool_use", "server_tool_use")), default=-1)
    text = "".join(b.text for b in blocks[last_tool + 1:] if b.type == "text").strip()
    return _drop_preamble(text)


def _drop_preamble(text: str) -> str:
    """A chatty lead-in ("Great, I found...") before the first heading, despite the prompt. Text
    blocks are joined as they come (a citation can split a sentence), so the lead-in may even
    share the heading's line."""
    m = _FIRST_HEADING.search(text[:PREAMBLE_CHARS])
    return text[m.start(1):].strip() if m and m.start(1) > 0 else text


def _question(text: str) -> str | None:
    """The question in an answer that is nothing but RESEARCH_QUESTION and a question; None
    for a real result."""
    text = text.strip()
    if not text.startswith(RESEARCH_QUESTION):
        return None
    return text[len(RESEARCH_QUESTION):].strip() or None


def _document_key(caption: str) -> str:
    return " ".join(_PAGE_MARK.sub(" ", caption.lower()).split())


def _md_caption(caption: str) -> str:
    """A caption safe inside ![...]: no brackets, one line, not too long."""
    return " ".join(caption.replace("[", "(").replace("]", ")").split())[:120]


def merge(text: str, images: list[str]) -> str:
    """Text and the image lines as one note: the images under their own heading, in front of
    the sources (which close the note) when there are any."""
    if not images:
        return text
    block = "\n\n".join([IMAGES_HEADING, *images])
    if not text:
        return block
    head, sep, tail = text.partition(SOURCES_HEADING)
    if not sep:
        return f"{text}\n\n{block}"
    return f"{head.rstrip()}\n\n{block}\n\n{sep}{tail}"


def estimate_cost(model: str, *, input_tokens: int, output_tokens: int, cache_read: int = 0,
                  cache_write: int = 0, searches: int = 0) -> float:
    """Dollars, roughly, for the log and the benchmark. Unknown models are priced as Sonnet 5."""
    pin, pout = PRICES.get(model, PRICES["claude-sonnet-5"])
    tokens = (input_tokens * pin + cache_write * pin * 1.25 + cache_read * pin * 0.1
              + output_tokens * pout)
    return tokens / 1_000_000 + searches * SEARCH_PRICE


def loop_tools(max_searches: int) -> list[dict]:
    """web_search is Anthropic's, the basic version: it runs on their side and has no code
    sandbox — the newer version's sandbox is where a model once slept and retried for minutes.
    site_search and read run here, so this code decides how many there are."""
    return [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches},
        {"name": "site_search", "description": site_search_tool(SITES),
         "input_schema": {"type": "object", "additionalProperties": False,
                          "required": ["site", "query"],
                          "properties": {"site": {"enum": [s.name for s in SITES]},
                                         "query": {"type": "string"},
                                         "deal": {"enum": [SALE, RENT]}}}},
        {"name": "read", "description": READ_TOOL,
         "input_schema": {"type": "object", "additionalProperties": False,
                          "required": ["url"],
                          "properties": {"url": {"type": "string"},
                                         "look_for": {"type": "string"}}}},
    ]


@dataclass
class Lookup:
    """One lookup's budget, the pages it read, and what it cost."""

    reads_left: int
    pages: dict[str, str] = field(default_factory=dict)
    sites: list[str] = field(default_factory=list)
    site_reads: int = 0
    page_reads: int = 0
    fallbacks: int = 0
    unreadable: int = 0
    chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    searches: int = 0

    @property
    def spent(self) -> bool:
        return self.reads_left <= 0

    def count(self, usage: Any) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        tools = getattr(usage, "server_tool_use", None)
        self.searches += getattr(tools, "web_search_requests", 0) or 0


class WebResearcher:
    def __init__(
        self, api_key: str, model: str, *, max_searches: int = 4, timeout_s: float = 600.0,
        client: anthropic.AsyncAnthropic | None = None,
        is_image: Callable[[str], Awaitable[bool]] | None = None,
        search: ImageSearch | None = None,
        extra: Callable[[], str] | None = None, deadline_s: float = DEADLINE_S,
        health: Health | None = None, reader: Reader | None = None, max_reads: int = 8,
        soft_deadline_s: float = 90.0, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._health = health or Health()
        self._deadline = deadline_s
        # The user's own additions to the research prompt, from the admin page.
        self._extra = extra or (lambda: "")
        self._max_searches = max_searches
        self._max_reads = max_reads
        self._soft_deadline = soft_deadline_s
        self._clock = clock
        self._loop_tools = loop_tools(max_searches)
        self._reader = reader if reader is not None else Reader()
        self.last: Lookup | None = None
        self._client = client or anthropic.AsyncAnthropic(
            # No retry: a search that ran long is slow, not broken, and trying again only
            # doubles the wait. DEADLINE_S is what actually bounds it.
            api_key=api_key, timeout=timeout_s, max_retries=0)
        # Checks an image link before it is kept (ImageHost.is_image); without one, every
        # link the model found is kept and the executor sorts them out at write time.
        self._is_image = is_image
        self._search = search  # where pictures come from; None: text only

    async def aclose(self) -> None:
        await self._client.close()
        await self._reader.aclose()

    async def research(self, request: str, query: str, media: str = "text") -> str:
        """Markdown ready to write: text, pictures, or both, per `media` (the interpreter's
        web_media). The text is Claude's web research; pictures come from Wikimedia Commons
        (Claude only proposes the search phrases) and from the pages the text cites, because
        image links a model writes itself are mostly invented. Raises ResearchQuestion when the
        request needs the user first, ResearchError when nothing usable came back."""
        try:
            async with asyncio.timeout(self._deadline):
                return await self._research(request, query, media)
        except TimeoutError:
            raise ResearchTimeout(f"gave up after {self._deadline:.0f}s") from None

    async def _research(self, request: str, query: str, media: str) -> str:
        want_text, want_images = media != "images", media != "text"
        text_call = (self._text(request, query) if want_text
                     else asyncio.sleep(0, result=""))
        commons_call = (self._commons(request, query) if want_images
                        else asyncio.sleep(0, result=[]))
        text, commons = await asyncio.gather(text_call, commons_call, return_exceptions=True)
        if isinstance(text, ResearchQuestion):
            raise text
        if isinstance(commons, BaseException):
            log.warning("image search failed: %s", commons)
            commons = []
        text_failed = isinstance(text, BaseException)
        images: list[str] = []
        if want_images:
            sources = [] if text_failed else await self._source_images(text)
            images = await self._keep([*commons, *sources], request, query)
        if text_failed:
            if not images:
                raise text
            text = ""  # the pictures alone are still worth writing
        if not text and not images:
            raise ResearchError("no images found" if want_images else "no answer")
        return merge(text, images)

    async def _phrases(self, request: str, query: str) -> list[str]:
        """English search phrases for Commons: one cheap call, no tools."""
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=200, system=IMAGE_QUERIES_PROMPT,
                messages=[{"role": "user", "content": research_message(request, query)}],
            )
        except anthropic.APIError as e:
            self._health.record(e)
            log.info("image phrases failed (%s)", getattr(e, "status_code", type(e).__name__))
            return []
        text = "".join(b.text for b in resp.content if b.type == "text")
        lines = [line.strip(" -•\t\"'") for line in text.splitlines()]
        return [line for line in lines if line][:MAX_PHRASES]

    async def _commons(self, request: str, query: str) -> list[tuple[str, str]]:
        if self._search is None:
            return []
        phrases = await self._phrases(request, query)
        found = await asyncio.gather(*(self._commons_one(p) for p in phrases))
        # Interleaved, so each phrase contributes its best picture before any gives a second.
        return [hit for row in zip_longest(*found) for hit in row if hit is not None]

    async def _commons_one(self, phrase: str) -> list[tuple[str, str]]:
        """Commons hits for `phrase`, dropping its last word until something matches: every
        word must match, and one abstract word ("family") empties the whole search."""
        assert self._search is not None
        words = phrase.split()
        while words:
            hits = await self._search.commons(" ".join(words))
            if hits:
                return hits
            words = words[:-1]
        return []

    async def _source_images(self, text: str) -> list[tuple[str, str]]:
        """og:image of the pages the text cites (its sources section)."""
        if self._search is None or SOURCES_HEADING not in text:
            return []
        links = _LINK.findall(text.split(SOURCES_HEADING, 1)[1])[:MAX_SOURCE_PAGES]
        found = await asyncio.gather(*(self._search.og_image(u) for u in links))
        return [hit for hit in found if hit is not None]

    async def _relevant(
        self, request: str, query: str, hits: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """The hits whose captions fit the request, by one cheap call. Commons search also
        matches file descriptions, so "Helsinki Stockholm" once returned six pages of a scanned
        1923 letter. On any failure the hits are kept as they are."""
        if not hits:
            return hits
        listing = "\n".join(f"{i}. {cap or url.rsplit('/', 1)[-1]}"
                            for i, (url, cap) in enumerate(hits, start=1))
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=300, system=IMAGE_FILTER_PROMPT,
                messages=[{"role": "user", "content": image_filter_message(
                    request, query, listing)}],
                output_config={"format": {"type": "json_schema", "schema": KEEP_SCHEMA}},
            )
            text = next((b.text for b in resp.content if b.type == "text"), "")
            keep = json.loads(text).get("keep", [])
        except (anthropic.APIError, ValueError, AttributeError) as e:
            log.info("image relevance check skipped (%s)", type(e).__name__)
            return hits
        chosen = {n for n in keep if isinstance(n, int)}
        return [hit for i, hit in enumerate(hits, start=1) if i in chosen]

    async def _keep(
        self, hits: list[tuple[str, str]], request: str = "", query: str = ""
    ) -> list[str]:
        """Image lines for the hits that fit the request and really are images."""
        hits = list({url: (url, cap) for url, cap in hits}.values())  # one line per picture
        hits = list({_document_key(cap) or url: (url, cap)
                     for url, cap in reversed(hits)}.values())[::-1]  # one page per document
        hits = await self._relevant(request, query, hits[:MAX_CANDIDATES])
        if self._is_image is not None:
            ok = await asyncio.gather(*(self._is_image(url) for url, _ in hits))
            hits = [hit for hit, good in zip(hits, ok, strict=True) if good]
        return [f"![{_md_caption(cap)}]({url})" for url, cap in hits[:MAX_IMAGES]]

    def _text(self, request: str, query: str) -> Awaitable[str]:
        prompt = research_loop_prompt(self._max_searches, self._max_reads)
        return self._run(_with_extra(prompt, self._extra()), request, query)

    async def _run(self, system: str, request: str, query: str) -> str:
        """Claude asks for lookups, this runs them, until it answers or the budget ends it. When
        the reads are spent or the soft deadline passes, the next request has tools switched
        off: the model cannot call one again, so it cannot spin — it answers."""
        lookup = Lookup(reads_left=self._max_reads)
        self.last = lookup
        start = self._clock()
        history: list[dict] = [{"role": "user", "content": research_message(request, query)}]
        turn: list[Any] = []  # the assistant turn being written, across pause_turn resumes
        force = False
        resp = None
        for _ in range(MAX_TURNS):
            messages = [*history, {"role": "assistant", "content": turn}] if turn else history
            resp = await self._create(system, messages, force)
            lookup.count(resp.usage)
            turn = [*turn, *resp.content]
            if resp.stop_reason == "pause_turn":
                continue
            calls = [b for b in resp.content if b.type == "tool_use"]
            if force or resp.stop_reason != "tool_use" or not calls:
                break
            results = await self._run_tools(lookup, calls, request)
            force = lookup.spent or self._clock() - start >= self._soft_deadline
            if force:
                results.append({"type": "text", "text": FORCE_ANSWER})
            history += [{"role": "assistant", "content": turn},
                        {"role": "user", "content": results}]
            turn = []
        self._log(lookup, start)
        if resp is not None and resp.stop_reason == "refusal":
            raise ResearchError("claude declined")
        text = final_text(turn)
        question = _question(text)
        if question:
            raise ResearchQuestion(question)
        if not text:
            stop = resp.stop_reason if resp is not None else "none"
            raise ResearchError(f"no answer (stop_reason={stop})")
        return text[:MAX_RESULT_CHARS]

    async def _create(self, system: str, messages: list[dict], force: bool) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": MAX_TOKENS, "system": system,
            "messages": messages, "tools": self._loop_tools,
            # Every turn re-sends the conversation so far; cached, the repeat costs a tenth.
            "cache_control": {"type": "ephemeral"},
        }
        if force:
            kwargs["tool_choice"] = {"type": "none"}
        if self.model.startswith(_EFFORT_PREFIXES):
            kwargs["output_config"] = {"effort": "medium"}
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise ResearchError(describe(e), self._health.record(e)) from None
        self._health.ok()
        return resp

    async def _run_tools(self, lookup: Lookup, calls: list[Any], request: str) -> list[dict]:
        """Every client tool call of one turn, answered in one message. The budget is handed out
        in the order the calls came, before any runs; the reads then run side by side."""
        seen: set[str] = set()
        plans = [self._allocate(lookup, call, seen) for call in calls]
        answers = await asyncio.gather(*(self._answer(lookup, plan, request) for plan in plans))
        note = READS_LEFT.format(n=lookup.reads_left)
        return [{"type": "tool_result", "tool_use_id": call.id, "content": body + note,
                 "is_error": failed}
                for call, (body, failed) in zip(calls, answers, strict=True)]

    @staticmethod
    def _allocate(lookup: Lookup, call: Any, seen: set[str]) -> tuple:
        """What to do with one call, decided before anything runs: ("error", why),
        ("refused",), ("cached", url), ("dup",) or ("read", url, look_for, site)."""
        args = call.input if isinstance(call.input, dict) else {}
        try:
            if call.name == "site_search":
                site = str(args.get("site", ""))
                look_for = str(args.get("query", ""))
                url = search_url(site, look_for, str(args.get("deal") or SALE))
            elif call.name == "read":
                site = ""
                look_for = str(args.get("look_for", ""))
                url = str(args.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    raise ValueError("read needs a full http or https address")
            else:
                raise ValueError(f"unknown tool {call.name!r}")
        except ValueError as e:  # SiteError is a ValueError too
            return ("error", TOOL_INPUT_ERROR.format(error=e))
        if url in lookup.pages:
            return ("cached", url)
        if url in seen:
            return ("dup",)
        if lookup.spent:
            return ("refused",)
        lookup.reads_left -= 1
        seen.add(url)
        return ("read", url, look_for, site)

    async def _answer(self, lookup: Lookup, plan: tuple, request: str) -> tuple[str, bool]:
        kind = plan[0]
        if kind == "error":
            return plan[1], True
        if kind == "refused":
            return READ_REFUSED, True
        if kind == "dup":
            return ALREADY_READ, False
        if kind == "cached":
            return lookup.pages[plan[1]], False
        _, url, look_for, site = plan
        if site:
            lookup.sites.append(site)
            lookup.site_reads += 1
        else:
            lookup.page_reads += 1
        try:
            page = await self._reader.read(url)
        except PageUnreadable as e:
            lookup.unreadable += 1
            return READ_FAILED.format(reason=e), True
        lookup.fallbacks += page.via != "jina"
        log.debug("research read %s via %s", url, page.via)
        body = f"{page.title}\n{page.url}\n\n{relevant(page.text, f'{look_for} {request}')}"
        lookup.pages[url] = body
        lookup.chars += len(body)
        return body, False

    def _log(self, lookup: Lookup, start: float) -> None:
        cost = estimate_cost(self.model, input_tokens=lookup.input_tokens,
                             output_tokens=lookup.output_tokens, cache_read=lookup.cache_read,
                             cache_write=lookup.cache_write, searches=lookup.searches)
        sites = f" ({', '.join(dict.fromkeys(lookup.sites))})" if lookup.sites else ""
        log.info("research: %d web search(es), %d site search(es)%s, %d page read(s) "
                 "(%d via fallback, %d unreadable), %.1fk chars, %.0f s, %dk+%dk tok, ~$%.3f",
                 lookup.searches, lookup.site_reads, sites, lookup.page_reads,
                 lookup.fallbacks, lookup.unreadable, lookup.chars / 1000,
                 self._clock() - start,
                 (lookup.input_tokens + lookup.cache_read + lookup.cache_write) // 1000,
                 lookup.output_tokens // 1000, cost)
