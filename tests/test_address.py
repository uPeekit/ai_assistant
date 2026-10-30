"""The bot's name: when it is being addressed, and when the name is just a word in a sentence."""

from __future__ import annotations

import pytest

from app import address, texts
from tests.test_orchestrator import CHAT, USER, bot, task  # noqa: F401  (bot is a fixture)

NAMES = address.names("Робо,Робот,Robo")


@pytest.mark.parametrize("message,expected", [
    ("Робо, купи молоко", "купи молоко"),
    ("робо купи молоко", "купи молоко"),
    ("Робот! что у меня сегодня?", "что у меня сегодня?"),
    ("эй Робо — напомни про зубы", "напомни про зубы"),
    ("Robo, buy milk", "buy milk"),
    ("купи молоко, Робо", "купи молоко"),
    ("Робо, купи молоко, Робо", "купи молоко"),
])
def test_the_name_is_taken_off_the_ends(message, expected):
    out = address.strip(message, NAMES)
    assert out.text == expected and out.called and not out.only_name


@pytest.mark.parametrize("message", [
    "Рассмотреть возможность использовать Робот в Боте",
    "позвонить Роботу насчёт машины",
    "Робототехника 12",
    "робогриль купить",
])
def test_the_name_inside_a_sentence_is_left_alone(message):
    out = address.strip(message, NAMES)
    assert out.text == message and not out.called


@pytest.mark.parametrize("message", ["Робо", "  робо?", "Робот!!"])
def test_being_called_with_nothing_else(message):
    out = address.strip(message, NAMES)
    assert out.only_name and out.called and out.text == ""


def test_without_a_configured_name_nothing_changes():
    out = address.strip("Робо, купи молоко", address.names(""))
    assert out.text == "Робо, купи молоко" and not out.called


def test_questions_about_the_bot():
    assert address.about_question("кто ты?")
    assert address.about_question("а что ты умеешь")
    # a long message that merely contains the words is a note, not a question about the bot
    assert not address.about_question("запиши в заметку про бота что ты умеешь и чего нет")
    assert not address.about_question("купить молоко")


# ---- through the bot ----------------------------------------------------------------------


@pytest.fixture
def named(bot):  # noqa: F811
    bot.orch._fallback_names = NAMES  # what .env said; the admin page can override it
    return bot


def sent(bot) -> str:
    return bot.claude.seen[0]["messages"][0]["content"]


async def test_a_message_addressed_by_name_is_written_without_it(named):
    task(named, "купи молоко")
    await named.orch.handle_text(CHAT, USER, "Робо, купи молоко")
    assert "купи молоко" in sent(named) and "Робо" not in sent(named)


async def test_calling_the_bot_by_name_alone_answers_without_a_model(named):
    reply = await named.orch.handle_text(CHAT, USER, "Робо?")
    assert reply.text == texts.CALLED and named.claude.seen == []


async def test_asking_what_it_is_answers_from_texts(named):
    reply = await named.orch.handle_text(CHAT, USER, "Робо, кто ты?")
    assert "Робо" in reply.text and named.claude.seen == []


async def test_an_unnamed_bot_takes_the_words_as_they_are(bot):  # noqa: F811
    task(bot, "купи молоко")
    await bot.orch.handle_text(CHAT, USER, "Робо, купи молоко")
    assert "Робо, купи молоко" in sent(bot)
