"""Transport-neutral reply model: plain text plus button rows. Nothing here imports
python-telegram-bot."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Button:
    id: str
    label: str


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    undo_id: int | None = None
    # Shown only to whoever pressed the button (a callback answer), never posted to the chat,
    # and the pressed message keeps its buttons: another member's write in a group.
    notice: bool = False
    # The answer to a question in this reply, as words alone (app/conversation/memory.py
    # remembers it, so "save this" saves the answer, not a refusal or a list of links).
    answer: str = ""
