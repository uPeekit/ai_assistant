"""Sending text Telegram will take.

One message holds 4096 characters, counted in UTF-16 units. A reply or a digest longer than
that was refused whole: the mail digest had already moved its bookmark, so forty letters' worth
of summaries were simply never seen, and a long answer from the vault came out as "could not
handle the message" after the write had been done."""

from __future__ import annotations

from typing import Any

MAX_MESSAGE = 4096


def _units(s: str) -> int:
    """The length Telegram counts: UTF-16 code units, so an emoji outside the BMP is two."""
    return len(s.encode("utf-16-le")) // 2


def _fits(s: str, limit: int) -> int:
    """How many characters from the start of `s` fit in `limit` UTF-16 units. Counted per
    character, so a cut there never splits a surrogate pair."""
    units = 0
    for i, ch in enumerate(s):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return i
    return len(s)


def pieces(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """`text` as messages of at most `limit` UTF-16 units (what Telegram counts), cut at a line
    end where there is one. A single line longer than a message is cut where it has to be."""
    out: list[str] = []
    rest = text
    while _units(rest) > limit:
        end = max(1, _fits(rest, limit))
        cut = rest.rfind("\n", 0, end + 1)
        if cut <= 0:
            cut = end
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
