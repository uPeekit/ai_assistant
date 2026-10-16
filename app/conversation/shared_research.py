"""One web search per message, however many branches ask for it.

The Notion side and the Obsidian side read a message independently and each may decide it
needs looking up. A search costs minutes and a few cents, and two searches for the same message
find the same pages. So each turn gets one `SharedResearch`: the first branch to ask starts the
search, and a branch asking for the same thing while it runs (or after) gets the same result —
or the same failure.

"The same thing" is judged on what the search is *for*: the user's message and the kind of
result (text, pictures, both). The query each branch's model wrote is a hint to the search, and
two models word it differently for the same request. A search that returns text and pictures
also answers a request for either alone; the other way round it does not.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

TEXT, IMAGES, BOTH = "text", "images", "text_and_images"


class Researcher(Protocol):
    async def research(self, request: str, query: str, media: str = TEXT) -> str: ...


def _covers(have: str, want: str) -> bool:
    return have == want or have == BOTH


def _key(request: str) -> str:
    return " ".join(request.casefold().split())


@dataclass
class SharedResearch:
    researcher: Researcher
    _running: list[tuple[str, str, asyncio.Task]] = field(default_factory=list)
    searches: int = 0  # how many actually ran, for the log

    async def research(self, request: str, query: str, media: str = TEXT) -> str:
        key = _key(request)
        for have_key, have_media, task in self._running:
            if have_key == key and _covers(have_media, media):
                log.info("web search shared: %s already asked for %s", media, have_media)
                return await asyncio.shield(task)
        task = asyncio.ensure_future(self.researcher.research(request, query, media))
        self._running.append((key, media, task))
        self.searches += 1
        return await asyncio.shield(task)
