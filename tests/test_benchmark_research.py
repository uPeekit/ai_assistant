from types import SimpleNamespace

from tools.benchmark_research import QUESTIONS, Meter


class FakeMessages:
    async def create(self, **kw):
        return SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, output_tokens=10, cache_read_input_tokens=50,
            cache_creation_input_tokens=0,
            server_tool_use=SimpleNamespace(web_search_requests=1)))


async def test_the_meter_adds_up_every_call_and_resets():
    meter = Meter(SimpleNamespace(messages=FakeMessages()))
    await meter.messages.create(model="m")
    await meter.messages.create(model="m")
    assert (meter.calls, meter.input, meter.output, meter.cache_read, meter.searches) == (
        2, 200, 20, 100, 2)
    meter.reset()
    assert meter.calls == 0 and meter.input == 0


def test_the_questions_cover_real_estate_shops_and_general_research():
    keys = [key for key, _, _ in QUESTIONS]
    assert len(keys) == len(set(keys)) == 8
    assert {"kv", "flats", "panels", "milk", "iphone", "borscht", "trip", "pelevin"} == set(keys)


class Answers:
    def __init__(self, outcome):
        self.outcome = outcome

    async def research(self, request, query, media):
        from app.llm.research import ResearchError, ResearchQuestion

        if self.outcome == "asked":
            raise ResearchQuestion("какой город?")
        if self.outcome == "failed":
            raise ResearchError("no answer")
        return "## Ответ"


async def test_a_question_back_or_a_failure_is_an_outcome_not_a_crash():
    from tools.benchmark_research import ask

    assert await ask(Answers("ok"), "r", "q") == ("## Ответ", "ok")
    assert await ask(Answers("asked"), "r", "q") == ("[asked: какой город?]", "asked")
    assert await ask(Answers("failed"), "r", "q") == ("[ResearchError: no answer]", "failed")
