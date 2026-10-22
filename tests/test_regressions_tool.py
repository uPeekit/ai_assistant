"""tools/regressions.py: what decides a case passed, and the Google replay."""

import pytest

from app.google.errors import GoogleError
from tools.regressions import ReplayClient, _key, check


def test_a_reply_with_everything_expected_passes():
    expect = {"contains": ["Лолита", "5|пять"], "absent": ["obsidian://"],
              "writes": ["task", "grocery"], "reminders": 1, "lines_at_least": 2}
    reply = "Самая высокая — 5\nкнига «Лолита»"
    assert check(expect, reply, ["grocery", "task"], 1) == []


def test_each_miss_is_named():
    expect = {"contains": ["Лолита"], "absent": ["Фудзи"], "writes": ["grocery"],
              "reminders": 2, "lines_at_least": 3}
    misses = check(expect, "Тайные виды на гору Фудзи", ["task"], 0)
    assert misses == ["lacks «Лолита»", "has «Фудзи»", "wrote ['task'], not ['grocery']",
                      "set 0 reminder(s), not 2", "1 line(s), not 3+"]


def test_a_folder_case_is_judged_on_whether_its_file_was_reported():
    assert check({"reported": False}, "", [], 0, reported=False) == []
    assert check({"reported": False}, "📂 T: …", [], 0, reported=True) == ["reported"]


async def test_the_replay_answers_what_was_recorded_and_fails_the_rest_like_google():
    client = ReplayClient({_key("file", ("abc",)): {"modifiedTime": "t1"}})
    assert await client.file("abc") == {"modifiedTime": "t1"}
    with pytest.raises(GoogleError) as e:
        await client.document("other")
    assert not e.value.gone  # failing, not unshared: the copies there are still stand
