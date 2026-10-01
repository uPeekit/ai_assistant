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
