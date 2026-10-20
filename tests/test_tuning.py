"""Settings the admin page owns: they fall back to .env and the code, they win once saved, and
they take effect without a restart."""

from __future__ import annotations

import json
from datetime import time

import pytest

from app.daily import DailyMessage, parse_times
from app.tuning import Defaults, Tuning

DEFAULTS = Defaults(
    bot_name="Робо", agenda_at="09:00", mail_at="12:00,19:00",
    web_words=("найди", "погугли"), countdown_tag="отсчёт",
    date_props=("date", "дата"),
)


@pytest.fixture
def tuning(tmp_path) -> Tuning:
    return Tuning(tmp_path / "tuning.json", DEFAULTS)


def test_without_a_file_everything_comes_from_env_and_the_code(tuning):
    assert tuning.bot_name == "Робо" and tuning.agenda_at == "09:00"
    assert tuning.web_words == ("найди", "погугли")
    assert tuning.countdown_tag == "отсчёт" and tuning.date_props == ("date", "дата")
    assert tuning.research_note == "" and tuning.linker_note == ""


def test_saved_values_win_and_an_empty_one_resets(tuning):
    assert tuning.save({"bot_name": "Жора", "web_words": "поищи, найди"}) == 2
    assert tuning.bot_name == "Жора" and tuning.web_words == ("поищи", "найди")
    assert tuning.save({"bot_name": "Жора"}) == 0  # unchanged
    assert tuning.save({"bot_name": ""}) == 1
    assert tuning.bot_name == "Робо"  # back to what .env said


def test_lists_accept_commas_newlines_and_stray_hashes(tuning):
    tuning.save({"date_props": "date,\nдата\n when ", "countdown_tag": "#дедлайн"})
    assert tuning.date_props == ("date", "дата", "when")
    assert tuning.countdown_tag == "дедлайн"


def test_a_change_on_disk_is_noticed_without_a_restart(tuning, tmp_path):
    assert tuning.bot_name == "Робо"
    (tmp_path / "tuning.json").write_text(json.dumps({"bot_name": "Жора"}), encoding="utf-8")
    assert tuning.bot_name == "Жора"


def test_a_broken_file_falls_back_instead_of_breaking_the_bot(tmp_path, caplog):
    (tmp_path / "tuning.json").write_text("{not json", encoding="utf-8")
    assert Tuning(tmp_path / "tuning.json", DEFAULTS).bot_name == "Робо"


def test_only_strings_are_accepted(tuning):
    with pytest.raises(ValueError):
        tuning.save({"bot_name": 5})


def test_the_prompts_themselves_are_not_editable(tuning):
    """Deliberate: the extra-instruction fields add to a prompt, they never replace it."""
    assert set(tuning.all()) == {"bot_name", "agenda_at", "mail_at", "folders_at", "web_words",
                                 "countdown_tag", "date_props", "research_note", "linker_note",
                                 "never_open", "sync_after"}


async def test_the_schedule_re_reads_its_times(tuning):
    times: list[tuple[time, ...]] = [parse_times("09:00")]
    daily = DailyMessage(lambda: None, lambda: times[0], "Europe/Athens")
    assert daily._times == (time(9, 0),)
    times[0] = parse_times("12:00,19:00")
    assert daily._times == (time(12, 0), time(19, 0))  # picked up with no restart


def test_extra_instructions_are_appended_never_substituted():
    from app.llm.research import _with_extra

    assert _with_extra("ПРАВИЛА", "") == "ПРАВИЛА"
    assert _with_extra("ПРАВИЛА", "  и ещё  ") == "ПРАВИЛА\n\nи ещё"


def test_the_never_open_list_starts_with_documents_and_mail(tuning):
    from app.web.links import NEVER_OPEN_DEFAULT

    assert tuning.never_open == NEVER_OPEN_DEFAULT
    assert "docs.google.com" in tuning.never_open and "mail.google.com" in tuning.never_open


def test_the_never_open_list_is_edited_like_the_search_words(tuning):
    assert tuning.save({"never_open": "mybank.example, intranet.example.com"}) == 1
    assert tuning.never_open == ("mybank.example", "intranet.example.com")
    assert tuning.save({"never_open": ""}) == 1
    assert "notion.so" in tuning.never_open  # cleared: back to the defaults


def test_a_pasted_link_in_the_never_open_list_counts_as_its_site(tuning):
    tuning.save({"never_open": "https://www.MyBank.example/login?x=1\nhttp://wiki.example.com:8080/a"})
    assert tuning.never_open == ("mybank.example", "wiki.example.com")


def test_a_malformed_never_open_entry_breaks_nothing(tuning):
    tuning.save({"never_open": "http://[bad, mybank.example"})
    assert "mybank.example" in tuning.never_open
    assert "never_open" in tuning.all()


def test_times_every_n_hours_within_a_range():
    """«every 2h 08:00-22:00»: the owner's wish for the folder digest, besides a plain list."""
    assert parse_times("every 3h 09:00-21:00") == tuple(time(h, 0) for h in (9, 12, 15, 18, 21))
    assert parse_times("every 2h 08:30-13:00") == (time(8, 30), time(10, 30), time(12, 30))
    assert parse_times("every 6h") == tuple(time(h, 0) for h in (0, 6, 12, 18))
    assert parse_times("every 30m 10:00-11:00") == (time(10, 0), time(10, 30), time(11, 0))


def test_an_interval_may_cross_midnight_and_mix_with_plain_times():
    assert parse_times("every 2h 22:00-02:00") == (time(0, 0), time(2, 0), time(22, 0))
    assert parse_times("every 4h 08:00-16:00, 21:30") == (
        time(8, 0), time(12, 0), time(16, 0), time(21, 30))


def test_a_bad_interval_is_dropped_like_a_bad_time():
    assert parse_times("every 0h, 12:00") == (time(12, 0),)
    assert parse_times("every 2h 25:00-26:00") == ()
    assert parse_times("every 1m") == ()  # under five minutes: a typo, not a schedule


def test_the_folder_digest_has_its_own_times_and_follows_the_mail_until_set(tuning):
    assert tuning.folders_at == "12:00,19:00" and tuning.all()["folders_at"] == ""
    tuning.save({"folders_at": "every 2h   08:00-22:00"})
    assert tuning.folders_at == "every 2h 08:00-22:00"
    assert tuning.mail_at == "12:00,19:00"
