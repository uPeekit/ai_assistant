"""The links in a message, read and trimmed before the message is interpreted.

A link is data the user sent: a flat to put in a comparison table, a book whose real title
belongs in a list, an article to summarise, a page to answer a question about. Both interpreters
get what is on the page, and each uses it for the action it chose. A link on the never-open list
(a document, a bank) is not read at all: it would not open anyway, and the reader is a third
party. A link that cannot be read stays a plain link; nothing about it is an error.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from app.web.reader import Page, PageUnreadable
from app.web.trim import page_facts

log = logging.getLogger(__name__)

MAX_LINKS = 3
# All the pages of one message together, split evenly: what research gives one page, because
# this goes into every interpretation of the message. One link gets it all, three get a third.
LINKS_CHARS = 6000
DEADLINE_S = 12.0  # a slow page never holds the message up for long
URL = re.compile(r"https?://[^\s<>«»\"'()\[\]]+", re.I)
TRAILING = ".,;:!?"

# Domains whose pages are private or need a login: a document, a workspace, a bank. Editable on
# the admin page (Tuning.never_open); this is what it starts from.
NEVER_OPEN_DEFAULT = (
    "docs.google.com", "drive.google.com", "mail.google.com", "notion.so", "notion.site",
    "swedbank.ee", "seb.ee", "lhv.ee", "luminor.ee", "cooppank.ee", "bigbank.ee",
)


@dataclass(frozen=True)
class LinkPage:
    url: str
    title: str = ""
    text: str = ""  # its share of LINKS_CHARS; empty when the page could not be read
    error: str = ""  # why not: "private", "timeout", or the reader's reason


class _Reader(Protocol):
    async def read(self, url: str) -> Page: ...


def find_links(text: str) -> list[str]:
    """The http(s) addresses in the text, in order, each once, without the punctuation that
    ends the sentence they sit in."""
    out: list[str] = []
    for match in URL.findall(text):
        url = match.rstrip(TRAILING)
        if url not in out:
            out.append(url)
    return out


def is_private(url: str, never_open: Iterable[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    for domain in never_open:
        domain = domain.strip().lower().lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


class LinkReader:
    def __init__(self, reader: _Reader, never_open: Callable[[], Iterable[str]], *,
                 deadline_s: float = DEADLINE_S) -> None:
        self._reader = reader
        self._never_open = never_open
        self._deadline = deadline_s

    async def read(self, message: str) -> list[LinkPage]:
        urls = find_links(message)[:MAX_LINKS]
        if not urls:
            return []
        start = time.monotonic()
        never = list(self._never_open())
        pages: dict[str, LinkPage] = {u: LinkPage(u, error="private")
                                      for u in urls if is_private(u, never)}
        readable = [u for u in urls if u not in pages]
        limit = LINKS_CHARS // max(len(readable), 1)
        tasks = {asyncio.ensure_future(self._one(u, message, limit)): u for u in readable}
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._deadline)
            for task in pending:
                task.cancel()
                pages[tasks[task]] = LinkPage(tasks[task], error="timeout")
            for task in done:
                pages[tasks[task]] = task.result()
        result = [pages[u] for u in urls]
        log.info("links: %d found, %d read, %d private, %d not read, %.1f s", len(result),
                 sum(1 for p in result if not p.error),
                 sum(1 for p in result if p.error == "private"),
                 sum(1 for p in result if p.error and p.error != "private"),
                 time.monotonic() - start)
        return result

    async def _one(self, url: str, message: str, limit: int) -> LinkPage:
        try:
            page = await self._reader.read(url)
        except PageUnreadable as e:
            return LinkPage(url, error=str(e))
        except Exception as e:  # one link must never cost the message
            log.warning("links: a page read failed unexpectedly (%s)", type(e).__name__)
            return LinkPage(url, error=type(e).__name__)
        return LinkPage(url, page.title, page_facts(page.text, message, limit=limit))
