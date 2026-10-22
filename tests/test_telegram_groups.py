"""Groups at the Telegram edge: a listed group is answered only when addressed; an unlisted one
is remembered and ignored; buttons and commands follow the same rules."""

from __future__ import annotations

import datetime
import logging
from datetime import UTC
from types import SimpleNamespace

import pytest
from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User, Voice

from app import texts
from app.chats import Chats
from app.telegram import handlers as h
from tests.test_handlers import (
    ALLOWED_USER,
    FakeBot,
    FakeDiscovery,
    FakeOrchestrator,
    FakeSpeech,
    FakeStore,
    build_app,
    dispatch,
    make_settings,
)

GROUP, OTHER_GROUP, MEMBER, BOT_ID = -100777, -100888, 501, 999


class GroupBot(FakeBot):
    id = BOT_ID


@pytest.fixture
def harness(tmp_path):
    chats = Chats(tmp_path / "chats.json")
    chats.save([{"chat_id": GROUP, "title": "club", "rights": ["ask", "add", "tasks"],
                 "tag": "клуб", "folder": "Клуб"}])
    app = build_app()
    orch = FakeOrchestrator()
    h.register(app, orch, make_settings(), FakeSpeech(), FakeDiscovery(), FakeStore(),
               chats=chats, names=lambda: ("Робо",))
    bot = GroupBot()
    context = SimpleNamespace(bot_data=app.bot_data, bot=bot, error=None, args=[])
    return SimpleNamespace(app=app, orch=orch, bot=bot, context=context, chats=chats)


def _msg(bot, chat_id, uid, *, text=None, entities=None, reply_to=None, voice=None, mid=1):
    chat = Chat(id=chat_id, type="supergroup", title="Клуб")
    msg = Message(message_id=mid, date=datetime.datetime.now(UTC), chat=chat,
                  from_user=User(id=uid, is_bot=False, first_name="m"), text=text,
                  entities=entities, reply_to_message=reply_to, voice=voice)
    msg.set_bot(bot)
    if reply_to is not None:
        reply_to.set_bot(bot)
    return Update(update_id=mid, message=msg)


def _bot_message(bot, chat_id):
    return Message(message_id=50, date=datetime.datetime.now(UTC),
                   chat=Chat(id=chat_id, type="supergroup"),
                   from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), text="✅")


def mention(text: str) -> list[MessageEntity]:
    at = text.index("@")
    return [MessageEntity(type=MessageEntity.MENTION, offset=at,
                          length=len("@" + FakeBot.username))]


async def test_a_mention_is_handled_without_the_mention(harness):
    text = f"@{FakeBot.username} добавь встречу"
    handled = await dispatch(harness.app, _msg(harness.bot, GROUP, MEMBER, text=text,
                                               entities=mention(text)), harness.context)
    assert handled
    assert harness.orch.calls[0][1:4] == (GROUP, MEMBER, "добавь встречу")


async def test_ordinary_chatter_in_the_group_is_ignored_without_a_trace(harness, caplog):
    with caplog.at_level(logging.DEBUG):
        handled = await dispatch(harness.app, _msg(harness.bot, GROUP, MEMBER,
                                                   text="кто идёт в субботу?"),
                                 harness.context)
    assert not handled and harness.orch.calls == []
    assert not any("AUTH_DENIED" in r.getMessage() for r in caplog.records)


async def test_a_reply_to_the_bot_and_its_name_both_address_it(harness):
    reply = _msg(harness.bot, GROUP, MEMBER, text="и ещё чай",
                 reply_to=_bot_message(harness.bot, GROUP))
    assert await dispatch(harness.app, reply, harness.context)
    named = _msg(harness.bot, GROUP, MEMBER, text="Робо, когда встреча?", mid=2)
    assert await dispatch(harness.app, named, harness.context)
    assert len(harness.orch.calls) == 2


async def test_voice_counts_only_as_a_reply_to_the_bot(harness):
    voice = Voice(file_id="v", file_unique_id="vu", duration=2)
    plain = _msg(harness.bot, GROUP, MEMBER, voice=voice)
    assert not await dispatch(harness.app, plain, harness.context)
    replied = _msg(harness.bot, GROUP, MEMBER, voice=voice,
                   reply_to=_bot_message(harness.bot, GROUP), mid=2)
    assert any(handler.check_update(replied) not in (None, False)
               for handler in harness.app.handlers[0])


async def test_an_unlisted_group_is_remembered_and_gets_nothing_even_from_an_owner(harness):
    text = f"@{FakeBot.username} привет"
    update = _msg(harness.bot, OTHER_GROUP, ALLOWED_USER, text=text, entities=mention(text))
    assert not await dispatch(harness.app, update, harness.context)
    assert [s["chat_id"] for s in harness.chats.all()["seen"]] == [OTHER_GROUP]


async def test_notion_commands_are_private_and_undo_carries_the_sender(harness):
    def command(name: str, mid: int) -> Update:
        text = f"/{name}"
        return _msg(harness.bot, GROUP, MEMBER, text=text, mid=mid, entities=[
            MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))])

    assert not await dispatch(harness.app, command("targets", 1), harness.context)
    assert await dispatch(harness.app, command("undo", 2), harness.context)
    assert harness.orch.calls[-1][0] == "undo"


def _press(bot, chat_id, uid, data):
    msg = Message(message_id=7, date=datetime.datetime.now(UTC),
                  chat=Chat(id=chat_id, type="supergroup"),
                  from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), text="q")
    msg.set_bot(bot)
    query = CallbackQuery(id="cb", from_user=User(id=uid, is_bot=False, first_name="m"),
                          chat_instance="ci", data=data, message=msg)
    query.set_bot(bot)
    return Update(update_id=9, callback_query=query)


async def test_a_member_may_press_the_groups_buttons_but_not_the_mails(harness):
    assert await dispatch(harness.app, _press(harness.bot, GROUP, MEMBER, "u:3"),
                          harness.context)
    assert harness.orch.calls[-1] == ("handle_callback", GROUP, MEMBER, "u:3")
    calls = len(harness.orch.calls)
    await dispatch(harness.app, _press(harness.bot, GROUP, MEMBER, "mr:abc"), harness.context)
    await dispatch(harness.app, _press(harness.bot, OTHER_GROUP, MEMBER, "u:3"),
                   harness.context)
    assert len(harness.orch.calls) == calls


# ---- from the branch review ------------------------------------------------------------------

async def test_a_refused_press_is_answered_to_the_presser_and_the_message_is_left_alone(
        harness):
    from app.conversation.reply import Reply

    answered: list = []

    async def answer(callback_query_id, text=None, **kwargs):
        answered.append(text)
        return True

    harness.bot.answer_callback_query = answer
    harness.orch.reply = Reply("это не ваше", notice=True)
    await dispatch(harness.app, _press(harness.bot, GROUP, MEMBER, "u:3"), harness.context)
    assert answered == ["это не ваше"]
    assert harness.bot.sent == [] and not getattr(harness.bot, "cleared", [])


async def test_anonymous_admins_and_channel_posts_are_not_let_in(harness):
    text = f"@{FakeBot.username} добавь встречу"
    update = _msg(harness.bot, GROUP, MEMBER, text=text, entities=mention(text))
    update.message._unfreeze()
    update.message.sender_chat = Chat(id=GROUP, type="supergroup")
    assert not await dispatch(harness.app, update, harness.context)


async def test_slash_text_that_is_not_a_command_does_not_address_the_bot(harness):
    update = _msg(harness.bot, GROUP, MEMBER, text="/шутка про книгу")
    assert not await dispatch(harness.app, update, harness.context)


# ---- what a message may point back at -----------------------------------------------------------

from app.telegram.recent import RecentMessages, points_back  # noqa: E402


@pytest.fixture
def with_ring(tmp_path):
    chats = Chats(tmp_path / "chats.json")
    chats.save([{"chat_id": GROUP, "title": "club", "rights": ["ask", "add"], "tag": "клуб",
                 "folder": "Клуб"}])
    app = build_app()
    orch = FakeOrchestrator()
    ring = RecentMessages()
    h.register(app, orch, make_settings(), FakeSpeech(), FakeDiscovery(), FakeStore(),
               chats=chats, names=lambda: ("Робо",), recent=ring)
    bot = GroupBot()
    context = SimpleNamespace(bot_data=app.bot_data, bot=bot, error=None, args=[])
    return SimpleNamespace(app=app, orch=orch, bot=bot, context=context, ring=ring)


async def test_unaddressed_group_talk_is_kept_and_handed_over_only_when_pointed_at(
        with_ring, caplog):
    w = with_ring
    with caplog.at_level(logging.DEBUG):
        await dispatch(w.app, _msg(w.bot, GROUP, MEMBER, text="встречаемся в кафе Кофейня"),
                       w.context)
    assert w.ring.recent(GROUP) == [("m", "встречаемся в кафе Кофейня")]
    assert not any("Кофейня" in r.getMessage() for r in caplog.records)  # never logged

    plain = "@notionbot когда встреча?"
    await dispatch(w.app, _msg(w.bot, GROUP, MEMBER, text=plain, entities=mention(plain),
                               mid=2), w.context)
    assert w.orch.pointed == ()  # nothing points back: the ring stays out of it

    back = "@notionbot запиши это"
    await dispatch(w.app, _msg(w.bot, GROUP, MEMBER, text=back, entities=mention(back),
                               mid=3), w.context)
    assert w.orch.pointed == (("m1", "m: встречаемся в кафе Кофейня"),)


async def test_a_reply_hands_over_the_quoted_message(with_ring):
    w = with_ring
    quoted = Message(message_id=40, date=datetime.datetime.now(UTC),
                     chat=Chat(id=GROUP, type="supergroup"),
                     from_user=User(id=777, is_bot=False, first_name="Аня"),
                     text="предлагаю Солярис")
    text = "@notionbot запиши это"
    await dispatch(w.app, _msg(w.bot, GROUP, MEMBER, text=text, entities=mention(text),
                               reply_to=quoted), w.context)
    assert w.orch.pointed[0] == ("q", "Аня: предлагаю Солярис")


def test_points_back_reads_whole_words():
    assert points_back("запиши это") and points_back("сохрани что Аня написала")
    assert not points_back("что купить на ужин")  # "что" holds "то", it is not "то"


async def test_replying_to_the_bots_answer_quotes_its_words_not_its_marks(with_ring):
    w = with_ring
    answer = (texts.VAULT_ANSWER.format(answer="Плитка серая.") + "\n"
              + texts.VAULT_ANSWER_SOURCES.format(links="[Ремонт](obsidian://x)"))
    quoted = Message(message_id=41, date=datetime.datetime.now(UTC),
                     chat=Chat(id=GROUP, type="supergroup"),
                     from_user=User(id=BOT_ID, is_bot=True, first_name="bot"), text=answer)
    text = "@notionbot запиши это"
    await dispatch(w.app, _msg(w.bot, GROUP, MEMBER, text=text, entities=mention(text),
                               reply_to=quoted), w.context)
    assert w.orch.pointed[0] == ("q", "Плитка серая.")



async def test_an_edited_message_is_left_alone(harness):
    """An edit arrives with no message of its own: the text handler fell over on it."""
    text = f"@{FakeBot.username} добавь встречу"
    update = _msg(harness.bot, GROUP, MEMBER, text=text, entities=mention(text))
    edited = Update(update_id=2, edited_message=update.message)
    await dispatch(harness.app, edited, harness.context)
    assert harness.orch.calls == []
