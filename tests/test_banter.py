"""A group that may joke: a mention with no request in it gets a joke, not «не понял». A real
request is never joked at, and a chat without the right, or the owner's private chat, is what it
always was."""

from __future__ import annotations

from dataclasses import replace

import anthropic
import pytest

from app import texts
from app.chats import ChatScope
from app.llm.banter import Banter, BanterError
from app.llm.health import Health
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultWriter
from tests.test_orchestrator import (
    CHAT,
    USER,
    bot,  # noqa: F401 (a fixture)
    staged_bot,  # noqa: F401 (a fixture)
)
from tests.test_vault_filer import FakeAnthropic
from tests.test_vault_scoped_pipeline import CLUB, NOW, Script, index  # noqa: F401 (a fixture)

JOKER = replace(CLUB, rights=CLUB.rights | {"banter"})
NOISE = Script(INTENT_PROMPT={"intent": "unclear"}, TARGET_PROMPT={"target": "t"})


class FakeBanter:
    model = "sonnet"

    def __init__(self, line: str = "Хули надо?", fail: bool = False) -> None:
        self.reply, self.fail = line, fail
        self.asked: list[tuple[str, str, str]] = []

    async def line(self, message, name="", history=""):
        self.asked.append((message, name, history))
        if self.fail:
            raise BanterError("no joke")
        return self.reply, 120, 20

    async def aclose(self) -> None:
        pass


def pipe(index, script, banter=None, scope=JOKER):  # noqa: F811
    pipeline = VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                             StagedFiler(script, "haiku"), now=lambda: NOW, banter=banter)
    return pipeline.scoped(scope) if scope is not None else pipeline


def files(index):  # noqa: F811
    return {p: p.read_bytes() for p in index.root.rglob("*.md")}


async def test_a_mention_with_no_request_gets_a_joke_and_writes_nothing(index):  # noqa: F811
    before = files(index)
    banter = FakeBanter()

    turn = await pipe(index, NOISE, banter).handle("эй, ты тут?")

    assert turn.reply_line() == "Хули надо?"
    assert banter.asked[0][0] == "эй, ты тут?"
    assert files(index) == before and turn.undos == []


async def test_the_joke_is_not_something_save_this_can_pick_up(index):  # noqa: F811
    turn = await pipe(index, NOISE, FakeBanter()).handle("ау")
    assert turn.said == ""


async def test_the_joke_knows_who_spoke_and_what_was_just_said(index):  # noqa: F811
    from app.chats import ChatRef

    banter = FakeBanter()
    await pipe(index, NOISE, banter).handle(
        "ау", chat=ChatRef(-1, 5, 1, owner=False, name="Оля"), history="Ассистент: привет")
    assert banter.asked == [("ау", "Оля", "Ассистент: привет")]


async def test_a_joke_that_fails_is_a_stock_line_never_a_shrug(index):  # noqa: F811
    turn = await pipe(index, NOISE, FakeBanter(fail=True)).handle("ау")
    assert turn.reply_line() in texts.BANTER_LINES


async def test_with_no_model_it_still_never_shrugs(index):  # noqa: F811
    turn = await pipe(index, NOISE, None).handle("ау")
    assert turn.reply_line() in texts.BANTER_LINES


async def test_a_group_without_the_right_is_told_what_it_always_was(index):  # noqa: F811
    banter = FakeBanter()
    turn = await pipe(index, NOISE, banter, scope=CLUB).handle("ау")
    assert turn.reply_line() == texts.SCOPE_NOTHING and banter.asked == []


async def test_a_real_request_is_never_joked_at(index):  # noqa: F811
    """Add is the group's right: this is written, and the joker is not asked."""
    banter = FakeBanter()
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                    TASKS_PROMPT={"items": [{"text": "купить чай", "due": "", "repeat": "",
                                             "heading": "", "tag": "", "countdown": False}],
                                  "lookup": ""})
    turn = await pipe(index, script, banter).handle("купить чай")
    assert [w.kind for w in turn.writes] == ["task"] and banter.asked == []


async def test_a_request_the_group_may_not_make_is_refused_not_joked_at(index):  # noqa: F811
    banter = FakeBanter()
    scope = replace(JOKER, rights=frozenset({"ask", "banter"}))
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                    TASKS_PROMPT={"items": [{"text": "купить чай", "due": "", "repeat": "",
                                             "heading": "", "tag": "", "countdown": False}],
                                  "lookup": ""})
    turn = await pipe(index, script, banter, scope=scope).handle("купить чай")
    assert turn.reply_line().startswith(texts.SCOPE_NOT_HERE.split("{")[0])
    assert banter.asked == []


async def test_a_message_the_reader_could_not_place_is_not_taken_for_noise(index):  # noqa: F811
    """A request the stages lost still says so: joking there would hide that it failed."""
    banter = FakeBanter()
    script = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "inbox"})
    turn = await pipe(index, script, banter).handle("запиши что-то")
    assert banter.asked == [] and turn.reply_line() == texts.SCOPE_NOTHING


async def test_the_owners_private_chat_keeps_the_words_and_never_jokes(index):  # noqa: F811
    banter = FakeBanter()
    turn = await pipe(index, NOISE, banter, scope=None).handle("привет")
    assert [w.kind for w in turn.writes] == ["inbox"] and banter.asked == []


# ---- through the orchestrator -----------------------------------------------------------------

GROUP = -100777
ALICE = 501


@pytest.fixture
def group(staged_bot):  # noqa: F811
    club = ChatScope(chat_id=GROUP, title="club", rights=frozenset({"ask", "banter"}),
                     tag="клуб", folder="Клуб")
    staged_bot.orch._scopes = lambda chat_id: club if chat_id == GROUP else None
    staged_bot.vault._banter = staged_bot.joker = FakeBanter()
    return staged_bot


@pytest.mark.parametrize("text", ["", "   "])
async def test_a_bare_mention_gets_a_joke_with_no_reader_in_between(group, text):
    reply = await group.orch.handle_text(GROUP, ALICE, text, name="Alice")

    assert reply.text == "Хули надо?" and reply.buttons == []
    assert group.joker.asked[0][1] == "Alice"
    assert group.script.stages == []


async def test_being_called_by_name_alone_gets_a_joke_too(group):
    group.orch._fallback_names = ("Робо",)
    reply = await group.orch.handle_text(GROUP, ALICE, "Робо?")
    assert reply.text == "Хули надо?"


async def test_without_the_right_the_name_alone_is_what_it_was(group):
    group.orch._fallback_names = ("Робо",)
    plain = ChatScope(chat_id=GROUP, title="club", rights=frozenset({"ask"}), tag="клуб",
                      folder="Клуб")
    group.orch._scopes = lambda chat_id: plain if chat_id == GROUP else None
    reply = await group.orch.handle_text(GROUP, ALICE, "Робо?")
    assert reply.text == texts.CALLED and group.joker.asked == []


async def test_the_private_chat_called_by_name_is_what_it_was(group):
    group.orch._fallback_names = ("Робо",)
    reply = await group.orch.handle_text(CHAT, USER, "Робо?")
    assert reply.text == texts.CALLED and group.joker.asked == []


# ---- the model call -----------------------------------------------------------------------------

async def test_the_tone_set_on_the_admin_page_reaches_the_model():
    client = FakeAnthropic()
    client.answers = [{"line": "ignored"}]
    banter = Banter("", "sonnet", note=lambda: "матерись и подкалывай", client=client)

    line, tokens_in, tokens_out = await banter.line("ау", "Оля", "Ассистент: привет")

    sent = client.seen[0]
    assert "матерись и подкалывай" in sent["system"]
    user = sent["messages"][0]["content"]
    assert "ау" in user and "Оля" in user and "Ассистент: привет" in user
    assert line and tokens_in and tokens_out


async def test_with_no_tone_set_the_prompt_is_the_plain_one():
    client = FakeAnthropic()
    client.answers = [{"line": "x"}]
    await Banter("", "sonnet", client=client).line("ау")
    assert "Характер" not in client.seen[0]["system"]


async def test_a_refused_or_empty_answer_is_no_joke():
    class Empty(FakeAnthropic):
        async def create(self, **kwargs):
            resp = await super().create(**kwargs)
            resp.content = []
            return resp

    client = Empty()
    client.answers = [{}]
    with pytest.raises(BanterError):
        await Banter("", "sonnet", client=client).line("ау")


async def test_claude_down_is_recorded_for_the_warning():
    error = anthropic.APIError("boom", request=None,  # type: ignore[arg-type]
                               body={"error": {"message": "Your credit balance is too low"}})
    error.status_code = 400  # type: ignore[attr-defined]
    client = FakeAnthropic()
    client.answers = [error]
    health = Health()
    with pytest.raises(BanterError) as caught:
        await Banter("", "sonnet", client=client, health=health).line("ау")
    assert caught.value.reason
