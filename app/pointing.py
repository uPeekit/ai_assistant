"""Messages that point back at something said before ("save this"), and the bot's own answer
as words alone.

Shared by the Telegram edge (which decides whether to hand recent group talk over) and the
vault reader (which saves what was pointed at)."""

from __future__ import annotations

import re

from app import texts

_WORD = re.compile(r"[^\W\d_]+")
# The marks an answer to a question starts with, and the line naming its sources.
ANSWER_MARKS = tuple(t.split("{")[0] for t in (texts.VAULT_ANSWER, texts.ANSWER_GENERAL,
                                               texts.ANSWER_WEB))
SOURCES_MARK = texts.VAULT_ANSWER_SOURCES.split("{")[0]


def points_back(text: str) -> bool:
    """Does the message point at something said before ("save this", "what Anna wrote")?
    Whole words and stems: the Russian "what" contains the word "that" and is not it."""
    words = _WORD.findall(text.casefold())
    return any(w in texts.BACK_WORDS for w in words) or any(
        w.startswith(stem) for w in words for stem in texts.BACK_STEMS)


def clean_answer(text: str) -> str:
    """An answer as the bot sent it, without its mark and its sources line."""
    body = text.strip()
    for mark in ANSWER_MARKS:
        if body.startswith(mark):
            body = body[len(mark):]
            break
    return "\n".join(ln for ln in body.splitlines()
                     if not ln.startswith(SOURCES_MARK)).strip()
