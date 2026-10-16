"""Both branches may ask to search the web for the same message; it runs once."""

from __future__ import annotations

import asyncio

import pytest

from app.conversation.shared_research import SharedResearch
from app.llm.research import ResearchQuestion


class Slow:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    async def research(self, request: str, query: str, media: str = "text") -> str:
        self.calls.append((request, query, media))
        await asyncio.sleep(0.01)
        if self.fail:
            raise self.fail
        return f"found for {query} ({media})"


async def test_two_branches_asking_for_the_same_message_share_one_search():
    researcher = Slow()
    shared = SharedResearch(researcher)

    notion, vault = await asyncio.gather(
        shared.research("Найди про ворота тории", "виды ворот тории фото", "text_and_images"),
        shared.research("найди про  ворота Тории", "ворота тории", "text"))

    assert len(researcher.calls) == 1 and shared.searches == 1
    assert notion == vault  # whatever the first query found


async def test_a_different_message_or_a_kind_the_first_search_does_not_cover_searches_again():
    researcher = Slow()
    shared = SharedResearch(researcher)

    await shared.research("найди рецепт борща", "борщ", "text")
    await shared.research("найди рецепт борща", "борщ фото", "images")  # text covers no pictures
    await shared.research("найди рецепт щей", "щи", "text")

    assert len(researcher.calls) == 3


async def test_a_failure_reaches_every_branch_that_waited_for_it():
    shared = SharedResearch(Slow(fail=ResearchQuestion("какой именно?")))

    results = await asyncio.gather(shared.research("найди", "q", "text"),
                                   shared.research("найди", "q", "text"),
                                   return_exceptions=True)

    assert all(isinstance(r, ResearchQuestion) for r in results)


async def test_one_branch_giving_up_does_not_cancel_the_search_for_the_other():
    researcher = Slow()
    shared = SharedResearch(researcher)
    first = asyncio.ensure_future(shared.research("найди", "q", "text"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert await shared.research("найди", "q", "text") == "found for q (text)"
    assert len(researcher.calls) == 1
