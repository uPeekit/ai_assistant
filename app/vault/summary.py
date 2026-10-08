"""A short account of one vault write, for the Obsidian line of a reply.

"a task in the task file" told the user nothing they did not know: which task, under which tags, for
which day is what they need to check that the bot understood. Everything here is built from the
action as the writer carried it out — no model is asked to describe its own work."""

from __future__ import annotations

import re
from datetime import date

from app import texts

# How many writes a reply lists one per line before it says how many more there were.
MAX_LISTED = 8
# How much of a quoted line or property value is shown.
MAX_QUOTE = 50
MAX_VALUE = 30
MAX_PROPS = 3
SEP = " · "
_LINK = re.compile(r"^\[\[(?:[^\]|]*/)?([^\]|/]+)(?:\|([^\]]*))?\]\]$")


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def when(day: str, today: date) -> str:
    """Today, tomorrow, or the weekday and day — with the year only when it is not this
    one. "" for anything that is not a date."""
    try:
        d = date.fromisoformat(str(day).strip())
    except ValueError:
        return ""
    gap = (d - today).days
    if gap == 0:
        return texts.VAULT_SUM_TODAY
    if gap == 1:
        return texts.VAULT_SUM_TOMORROW
    stamp = d.strftime("%d.%m") if d.year == today.year else d.strftime("%d.%m.%Y")
    return f"{texts.VAULT_WEEKDAYS_SHORT[d.weekday()]} {stamp}"


def _value(value: object) -> str:
    """A property value as the user reads it: a link shows the name it points at."""
    if isinstance(value, list):
        return ", ".join(_value(v) for v in value)
    text = str(value).strip()
    m = _LINK.match(text)
    if m:
        text = m.group(2) or m.group(1)
    return _cut(text, MAX_VALUE)


def task(action, today: date, countdown_tag: str) -> str:
    parts = [f"«{_cut(action.text, MAX_QUOTE)}»"]
    tags = [f"#{t.lstrip('#')}" for t in action.tags]
    if action.countdown:
        tags.append(f"#{countdown_tag}")
    if tags:
        parts.append(" ".join(tags))
    if action.due and (day := when(action.due, today)):
        parts.append(f"📅 {day}")
    if action.repeat:
        parts.append(f"🔁 {action.repeat}")
    return SEP.join(parts)


def note(name: str, folder: str, props: dict) -> str:
    """The note, where it went, and the first few properties it was given. Tick boxes are
    left out: «false» next to a new meeting says nothing."""
    parts = [texts.VAULT_SUM_NOTE.format(name=name, folder=folder) if folder
             else f"«{name}»"]
    shown = [_value(v) for k, v in props.items()
             if k != "tags" and not isinstance(v, bool) and str(v).strip()]
    parts += [v for v in shown if v][:MAX_PROPS]
    tags = props.get("tags") or []
    if tags:
        parts.append(" ".join(f"#{t}" for t in tags))
    return SEP.join(parts)


def lines(written: list[str]) -> str:
    """The first line added, quoted, and how many more came with it."""
    first = next((line for line in written if line.strip()), "")
    if not first:
        return ""
    first = first.strip().lstrip("-*").strip()
    first = re.sub(r"^\[[ xX]\]\s*", "", first)
    more = sum(1 for line in written if line.strip()) - 1
    return f": «{_cut(first, MAX_QUOTE)}»" + (f" (+{more})" if more > 0 else "")


def update(action, today: date) -> str:
    """What changed: a task ticked off or moved, or the properties set."""
    parts = []
    if action.task:
        target = f"«{_cut(action.task, MAX_QUOTE)}»"
        if action.done is True:
            parts.append(f"✓ {target}")
        elif action.due and (day := when(action.due, today)):
            parts.append(f"{target} → 📅 {day}")
        elif action.done is False:
            parts.append(texts.VAULT_SUM_REOPENED.format(task=target))
    for key, value in list(action.props.items())[:MAX_PROPS]:
        shown = _value(value) if str(value).strip() else texts.VAULT_SUM_CLEARED
        parts.append(f"{key} → {shown}")
    return (": " + "; ".join(parts)) if parts else ""


def text(words: str) -> str:
    return f"«{_cut(words, MAX_QUOTE)}»" if words.strip() else ""
