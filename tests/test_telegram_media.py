"""An Instagram link in a group with the "media" right gets the post in reply
(app/telegram/media.py); everywhere else, and for a request to the bot, nothing happens here."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram import InputMediaPhoto, InputMediaVideo
from telegram.error import TimedOut

from app.chats import Chats
from app.media.instagram import Item, Post
from app.telegram import handlers as h
from app.telegram import media
from tests.test_handlers import (
    FakeDiscovery,
    FakeOrchestrator,
    FakeSpeech,
    FakeStore,
    build_app,
    make_settings,
)
from tests.test_telegram_groups import GROUP, MEMBER, OTHER_GROUP, GroupBot, _msg, mention

LINK = "https://www.instagram.com/reel/CrUJOHFNEQC/?igsh=x"
PLAIN_GROUP = -100999


class MediaBot(GroupBot):
    def __init__(self):
        super().__init__()
        self.media: list[tuple[str, dict]] = []
        self.fail: Exception | None = None

    async def _record(self, kind, chat_id, payload, kwargs):
        if self.fail is not None:
            raise self.fail
        self.media.append((kind, {"chat_id": chat_id, "payload": payload, **kwargs}))

    async def send_video(self, chat_id, video, **kwargs):
        await self._record("video", chat_id, video, kwargs)

    async def send_photo(self, chat_id, photo, **kwargs):
        await self._record("photo", chat_id, photo, kwargs)

    async def send_media_group(self, chat_id, media, **kwargs):
        await self._record("album", chat_id, media, kwargs)


class FakeInstagram:
    def __init__(self, posts: dict[str, Post | None]) -> None:
        self.posts = posts
        self.asked: list[str] = []

    async def fetch(self, code):
        self.asked.append(code)
        return self.posts.get(code)


REEL = Post([Item("video", b"mp4")], caption="Earth at night", source="instagram")


@pytest.fixture
def harness(tmp_path):
    chats = Chats(tmp_path / "chats.json")
    chats.save([{"chat_id": GROUP, "title": "club", "rights": ["ask", "media"], "tag": "клуб"},
                {"chat_id": PLAIN_GROUP, "title": "plain", "rights": ["ask"], "tag": "x"}])
    app = build_app()
    instagram = FakeInstagram({"CrUJOHFNEQC": REEL})
    h.register(app, FakeOrchestrator(), make_settings(), FakeSpeech(), FakeDiscovery(),
               FakeStore(), chats=chats, names=lambda: ("Робо",), instagram=instagram)
    bot = MediaBot()
    context = SimpleNamespace(bot_data=app.bot_data, bot=bot, error=None, args=[])
    return SimpleNamespace(app=app, bot=bot, context=context, instagram=instagram)


def media_handler(app):
    [handler] = app.handlers[1]
    return handler


async def run(harness, update) -> bool:
    handler = media_handler(harness.app)
    check = handler.check_update(update)
    if check in (None, False):
        return False
    await h._on_instagram(update, harness.context)
    return True


async def test_a_link_in_the_group_gets_the_video_in_reply(harness):
    update = _msg(harness.bot, GROUP, MEMBER, text=f"смотрите {LINK}", mid=7)
    assert await run(harness, update)
    [(kind, sent)] = harness.bot.media
    assert kind == "video" and sent["chat_id"] == GROUP and sent["payload"] == b"mp4"
    assert sent["caption"] == "Earth at night" and sent["supports_streaming"]
    assert sent["reply_parameters"].message_id == 7  # a reply to the link


async def test_the_handler_does_not_block_the_conversation(harness):
    assert media_handler(harness.app).block is False


@pytest.mark.parametrize("chat", [PLAIN_GROUP, OTHER_GROUP])
async def test_a_group_without_the_right_or_unlisted_gets_nothing(harness, chat):
    assert not await run(harness, _msg(harness.bot, chat, MEMBER, text=LINK))
    assert harness.instagram.asked == []


async def test_a_message_to_the_bot_is_a_request_not_a_link_to_show(harness):
    text = f"@{GroupBot.username} сохрани {LINK}"
    update = _msg(harness.bot, GROUP, MEMBER, text=text, entities=mention(text))
    assert not await run(harness, update)


async def test_a_message_without_a_link_is_not_looked_at(harness):
    assert not await run(harness, _msg(harness.bot, GROUP, MEMBER, text="просто текст"))


async def test_a_post_that_could_not_be_fetched_leaves_no_trace(harness):
    harness.instagram.posts = {}
    assert await run(harness, _msg(harness.bot, GROUP, MEMBER, text=LINK))
    assert harness.bot.media == [] and harness.bot.sent == []


async def test_telegram_refusing_the_upload_says_nothing_in_the_chat(harness):
    harness.bot.fail = TimedOut()
    assert await run(harness, _msg(harness.bot, GROUP, MEMBER, text=LINK))
    assert harness.bot.sent == []


class Replying:
    """The message's reply calls, as PTB's Message makes them."""

    chat_id = GROUP

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, dict]] = []

    async def reply_photo(self, photo, **kw):
        self.calls.append(("photo", photo, kw))

    async def reply_video(self, video, **kw):
        self.calls.append(("video", video, kw))

    async def reply_media_group(self, items, **kw):
        self.calls.append(("album", items, kw))


async def test_a_carousel_is_one_album_with_the_caption_under_the_first():
    post = Post([Item("photo", b"1"), Item("video", b"2"), Item("photo", b"3")],
                caption="Talk about complicated", source="vx.example")
    message = Replying()
    await media._send(message, post)
    [(kind, items, kw)] = message.calls
    assert kind == "album" and kw["do_quote"] is True
    assert [type(i) for i in items] == [InputMediaPhoto, InputMediaVideo, InputMediaPhoto]
    assert items[0].caption == "Talk about complicated"
    assert items[1].caption is None and items[2].caption is None


async def test_a_single_photo_goes_as_a_photo_without_an_empty_caption():
    message = Replying()
    await media._send(message, Post([Item("photo", b"1")], source="kk.example"))
    [(kind, data, kw)] = message.calls
    assert kind == "photo" and data == b"1" and kw["caption"] is None


def test_a_long_caption_is_cut_to_what_telegram_takes():
    cut = media.caption("слово " * 400)
    assert len(cut) <= media.MAX_CAPTION and cut.endswith("…")
    assert media.caption(" коротко ") == "коротко"
