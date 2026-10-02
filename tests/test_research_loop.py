import asyncio
import json
import logging

import anthropic
import httpx2
import pytest

from app.llm.prompts import FORCE_ANSWER
from app.llm.research import ResearchError, ResearchQuestion, ResearchTimeout, WebResearcher
from app.web.reader import Page, PageUnreadable

KEY = "sk-ant-test-key"


def message(content, stop_reason="end_turn"):
    return httpx2.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 2000, "output_tokens": 300},
    })


def tool_use(id_, name, **inp):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


class FakeReader:
    def __init__(self, fail=(), delay=0.0, on_read=None):
        self.calls: list[str] = []
        self.fail, self.delay, self.on_read = set(fail), delay, on_read

    async def read(self, url):
        self.calls.append(url)
        if self.on_read:
            self.on_read()
        if self.delay:
            await asyncio.sleep(self.delay)
        if url in self.fail:
            raise PageUnreadable("protected")
        return Page(url, "Pealkiri", "## Korter\n149 990 €\n3 tuba, 60 m²", "jina")

    async def aclose(self):
        pass


def scripted(*responses):
    """A handler answering each request with the next response; the bodies it saw."""
    bodies, queue = [], list(responses)

    def handler(req):
        bodies.append(json.loads(req.content))
        return queue.pop(0)
    return handler, bodies


def loop(handler, reader, model="claude-sonnet-5", **kw) -> WebResearcher:
    sdk = anthropic.AsyncAnthropic(
        api_key=KEY, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return WebResearcher(KEY, model, client=sdk, reader=reader, engine="loop", **kw)


async def test_tool_calls_run_and_are_answered_in_one_message():
    handler, bodies = scripted(
        message([text("Сейчас поищу."), tool_use("t1", "read", url="https://a.ee/1"),
                 tool_use("t2", "read", url="https://a.ee/2", look_for="цена")], "tool_use"),
        message([text("## Итог\n- 149 990 €")]))
    reader = FakeReader()
    answer = await loop(handler, reader).research("найди квартиру", "квартира", "text")
    assert answer == "## Итог\n- 149 990 €"  # the narration before the tools is not in it
    assert sorted(reader.calls) == ["https://a.ee/1", "https://a.ee/2"]
    results = bodies[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "149 990 €" in results[0]["content"]


async def test_tools_effort_and_caching_are_sent():
    handler, bodies = scripted(message([text("## Ок")]))
    await loop(handler, FakeReader(), max_searches=4).research("x", "y", "text")
    body = bodies[0]
    by_name = {t["name"]: t for t in body["tools"]}
    assert set(by_name) == {"web_search", "site_search", "read"}
    assert by_name["web_search"]["type"] == "web_search_20250305"
    assert by_name["web_search"]["max_uses"] == 4
    assert "kv.ee" in by_name["site_search"]["input_schema"]["properties"]["site"]["enum"]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "medium"}
    assert "tool_choice" not in body


async def test_haiku_gets_no_effort_setting():
    handler, bodies = scripted(message([text("## Ок")]))
    await loop(handler, FakeReader(), model="claude-haiku-4-5").research("x", "y", "text")
    assert "output_config" not in bodies[0]


async def test_site_search_reads_the_sites_own_search_page():
    handler, _ = scripted(
        message([tool_use("t1", "site_search", site="kv.ee", query="Kalevipoja põik 3")],
                "tool_use"),
        message([text("## Ок")]))
    reader = FakeReader()
    await loop(handler, reader).research("x", "y", "text")
    assert reader.calls == [
        "https://www.kv.ee/search?deal_type=1&keyword=Kalevipoja%20p%C3%B5ik%203"]


async def test_spent_reads_force_an_answer_with_tools_off():
    handler, bodies = scripted(
        message([tool_use(f"t{i}", "read", url=f"https://a.ee/{i}") for i in range(3)],
                "tool_use"),
        message([text("## Итог")]))
    reader = FakeReader()
    assert await loop(handler, reader, max_reads=2).research("x", "y", "text") == "## Итог"
    assert len(reader.calls) == 2  # the third was refused, not read
    results = bodies[1]["messages"][-1]["content"]
    assert results[2]["is_error"] is True
    assert results[-1] == {"type": "text", "text": FORCE_ANSWER}
    assert bodies[1]["tool_choice"] == {"type": "none"}


async def test_the_soft_deadline_forces_an_answer():
    now = [0.0]

    def later():
        now[0] = 100.0

    handler, bodies = scripted(
        message([tool_use("t1", "read", url="https://a.ee/1")], "tool_use"),
        message([text("## Итог")]))
    r = loop(handler, FakeReader(on_read=later), soft_deadline_s=90, clock=lambda: now[0])
    await r.research("x", "y", "text")
    assert bodies[1]["tool_choice"] == {"type": "none"}


async def test_a_url_read_twice_is_not_charged_again():
    handler, bodies = scripted(
        message([tool_use("t1", "read", url="https://a.ee/1")], "tool_use"),
        message([tool_use("t2", "read", url="https://a.ee/1")], "tool_use"),
        message([text("## Итог")]))
    reader = FakeReader()
    await loop(handler, reader, max_reads=2).research("x", "y", "text")
    assert reader.calls == ["https://a.ee/1"]
    assert "tool_choice" not in bodies[2]  # one read left: nothing forced


async def test_bad_tool_input_costs_nothing_and_says_why():
    handler, bodies = scripted(
        message([tool_use("t1", "site_search", site="bauhof.ee", query="liimpuit"),
                 tool_use("t2", "site_search", site="rimi.ee", query="piim", deal="rent"),
                 tool_use("t3", "read", url="kv.ee/1")], "tool_use"),
        message([text("## Итог")]))
    reader = FakeReader()
    await loop(handler, reader, max_reads=8).research("x", "y", "text")
    results = bodies[1]["messages"][-1]["content"]
    assert all(r["is_error"] for r in results)
    assert "unknown site" in results[0]["content"]
    assert "no rent search" in results[1]["content"]
    assert "http" in results[2]["content"]
    assert "8" in results[2]["content"]  # reads left: none were charged
    assert reader.calls == []


async def test_an_unreadable_page_tells_the_model_to_move_on():
    handler, bodies = scripted(
        message([tool_use("t1", "read", url="https://kv.ee/1")], "tool_use"),
        message([text("## Итог")]))
    await loop(handler, FakeReader(fail={"https://kv.ee/1"})).research("x", "y", "text")
    result = bodies[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "protected" in result["content"]


async def test_the_hard_deadline_still_raises_research_timeout():
    handler, _ = scripted(message([tool_use("t1", "read", url="https://a.ee/1")], "tool_use"))
    with pytest.raises(ResearchTimeout):
        await loop(handler, FakeReader(delay=1.0), deadline_s=0.05).research("x", "y", "text")


async def test_a_question_line_becomes_a_research_question():
    handler, _ = scripted(message([text("ВОПРОС: какой город?")]))
    with pytest.raises(ResearchQuestion, match="какой город"):
        await loop(handler, FakeReader()).research("x", "y", "text")


async def test_a_cut_off_answer_is_kept_and_an_empty_one_is_an_error():
    handler, _ = scripted(message([text("## Итог\n- обрыв")], "max_tokens"))
    assert await loop(handler, FakeReader()).research("x", "y", "text") == "## Итог\n- обрыв"
    handler, _ = scripted(message([], "max_tokens"))
    with pytest.raises(ResearchError, match="no answer"):
        await loop(handler, FakeReader()).research("x", "y", "text")


async def test_one_log_line_per_lookup_and_no_urls_at_info(caplog):
    caplog.set_level(logging.INFO)
    handler, _ = scripted(
        message([tool_use("t1", "site_search", site="kv.ee", query="Lasnamäe")], "tool_use"),
        message([text("## Итог")]))
    await loop(handler, FakeReader()).research("x", "y", "text")
    # The app's own lines only: the HTTP client logs every request URL at INFO by itself.
    info = [r.getMessage() for r in caplog.records
            if r.levelno >= logging.INFO and r.name.startswith("app.")]
    assert len([m for m in info if m.startswith("research:")]) == 1
    assert "kv.ee" in next(m for m in info if m.startswith("research:"))
    assert not any("http" in m for m in info)


def test_the_prompt_says_where_the_user_is_and_lists_estonian_sites_by_category():
    from app.llm.prompts import SITE_KINDS, research_loop_prompt
    from app.web.sites import KNOWN, SITES

    prompt = research_loop_prompt(4, 8)
    assert "Таллин" in prompt and ".ee" in prompt
    for site in (*SITES, *KNOWN):
        assert site.name in prompt
        assert SITE_KINDS[site.kind] in prompt
    assert "hinnavaatlus.ee" in prompt  # "where is it cheapest" goes to the price comparison


def test_site_search_offers_only_sites_with_a_working_search():
    from app.llm.research import loop_tools
    from app.web.sites import KNOWN, SITES

    site_search = next(t for t in loop_tools(4) if t["name"] == "site_search")
    enum = site_search["input_schema"]["properties"]["site"]["enum"]
    assert enum == [s.name for s in SITES]
    assert not {s.name for s in KNOWN} & set(enum)
    assert all(not s.template for s in KNOWN)
