"""An Instagram post sent back as a reply to its link (app/media/instagram.py fetches it).

One item goes as a photo or a video, more as an album with the caption under the first. The
post that could not be fetched or sent leaves no trace in the chat: someone's link is not the
place for the bot's errors."""

from __future__ import annotations

import logging

from telegram import InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError

from app import texts
from app.media.instagram import Instagram, Post, codes
from app.telegram.sending import pieces

log = logging.getLogger(__name__)

# A caption holds 1024 characters.
MAX_CAPTION = 1024
# Uploading 50 MB takes longer than PTB's default five seconds.
TIMEOUTS = {"write_timeout": 120.0, "read_timeout": 60.0}


def caption(text: str) -> str:
    parts = pieces(text.strip(), MAX_CAPTION - 1)
    return parts[0] + "…" if len(parts) > 1 else parts[0]


async def reply_with_posts(message, instagram: Instagram, *, say_failure: bool = False) -> None:
    """Every post linked in `message`, each as a reply to it. With `say_failure`, a post that
    could not be fetched or sent is said in a line of text."""
    for code in codes(message.text or ""):
        post = await instagram.fetch(code)
        if post is None:
            log.info("instagram: a post could not be fetched (chat %s)", message.chat_id)
            await _failed(message, say_failure)
            continue
        try:
            await _send(message, post)
        except TelegramError as e:
            log.warning("instagram: sending a post failed (%s)", e)
            await _failed(message, say_failure)
            continue
        log.info("instagram: %d item(s) via %s (chat %s)", len(post.items), post.source,
                 message.chat_id)


async def _failed(message, say: bool) -> None:
    if not say:
        return
    try:
        await message.reply_text(texts.INSTAGRAM_NOT_FETCHED, do_quote=True)
    except TelegramError as e:
        log.warning("instagram: saying so failed too (%s)", e)


async def _send(message, post: Post) -> None:
    text = caption(post.caption) or None
    if len(post.items) == 1:
        [item] = post.items
        if item.kind == "photo":
            await message.reply_photo(item.data, caption=text, do_quote=True, **TIMEOUTS)
        else:
            await message.reply_video(item.data, caption=text, supports_streaming=True,
                                      do_quote=True, **TIMEOUTS)
        return
    media = [InputMediaPhoto(item.data) if item.kind == "photo"
             else InputMediaVideo(item.data, supports_streaming=True) for item in post.items]
    if text:
        media[0] = type(media[0])(post.items[0].data, caption=text)
    await message.reply_media_group(media, do_quote=True, **TIMEOUTS)
