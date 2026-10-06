"""Sending text Telegram will take.

One message holds 4096 characters. A reply or a digest longer than that was refused whole: the
mail digest had already moved its bookmark, so forty letters' worth of summaries were simply
never seen, and a long answer from the vault came out as "could not handle the message" after
the write had been done."""

from __future__ import annotations

from typing import Any

MAX_MESSAGE = 4096


def pieces(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """`text` as messages of at most `limit` characters, cut at a line end where there is one.
    A single line longer than a message is cut where it has to be."""
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest or not out:
        out.append(rest)
    return out


async def send_text(bot: Any, chat_id: int, text: str, reply_markup: Any = None) -> Any:
    """Send `text` to a chat, in as many messages as it takes. The buttons go under the last
    one, which is also the message returned — the one a later turn edits its keyboard on."""
    parts = pieces(text)
    sent = None
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        extra = {"reply_markup": reply_markup} if last and reply_markup is not None else {}
        sent = await bot.send_message(chat_id, part, **extra)
    return sent
