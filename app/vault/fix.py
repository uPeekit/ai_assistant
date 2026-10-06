"""Fixing part of what a turn wrote: one question about the difference, not the whole message
again.

The bot already knows what it did — each write keeps the action it carried out
(`VaultUndo.action`). The model is shown those actions as short keyed lines, the place list and
the correction, and answers only what changes: drop this one, set that field, move this one
elsewhere, add these. Code turns that into the writes to take back and the actions to write in
their place; actions the correction does not name are not touched at all.

Every value passes the same gate as a fresh message (`staged._Gate`), over the original message,
the correction and the words already written: a date only when one of them names a time, a
title only when its words are there. Moving to a folder is the one case that asks a second
question — that folder's details stage, for that one action — because a book note needs the
properties a task never had."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app import texts
from app.llm import staged_prompts as P
from app.vault.writer import VaultAction

OPS = ("drop", "set", "move")
FIELDS = ("text", "title", "due", "repeat", "heading", "tag", "prop")
MAX_BODY_SHOWN = 3
_STRING = {"type": "string"}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props),
            "properties": props}


@dataclass(frozen=True)
class Item:
    """One thing the turn wrote, as the model is shown it. A grocery write of three products
    is three items, so "no bread after all" can drop one of them; `write` is the write it
    came from."""

    write: int
    action: VaultAction


@dataclass
class FixPlan:
    take_back: list[int] = field(default_factory=list)  # indexes of the turn's writes
    write: list[dict] = field(default_factory=list)  # raw actions, checked like any answer
    said: list[str] = field(default_factory=list)  # one reply phrase per change
    unclear: bool = False


def words(action: VaultAction) -> str:
    """The words an action is about: a task's text, a note's title, a list's lines."""
    return (action.text or action.title or ", ".join(action.body[:MAX_BODY_SHOWN])).strip()


def items(previous: list[VaultAction]) -> list[Item]:
    out: list[Item] = []
    for i, action in enumerate(previous):
        if action.action == "grocery" and len(action.body) > 1:
            out += [Item(i, action.model_copy(update={"body": [name]})) for name in action.body]
        else:
            out.append(Item(i, action))
    return out


def may_drop(correction: str) -> bool:
    """Does the correction ask for something to go? A drop is the one change that loses words,
    so it takes a word of the user's that says so, whatever the model answered."""
    words_said = re.findall(r"\w+", correction.casefold())
    return any(w in texts.FIX_DROP_WORDS or w.startswith(texts.FIX_DROP_STEMS)
               for w in words_said)


def describe(action: VaultAction, tasks_note: str) -> str:
    """One line of what was written, for the model."""
    note = {"task": tasks_note, "grocery": texts.VAULT_GROCERIES_NOTE,
            "inbox": texts.VAULT_INBOX_NOTE}.get(action.action, action.note)
    line = P.FIX_LINES.get(action.action, P.FIX_LINES["inbox"]).format(
        words=words(action), note=note, folder=action.folder)
    if action.due:
        line += P.FIX_DUE.format(due=action.due)
    if action.heading:
        line += P.FIX_HEADING.format(heading=action.heading)
    if action.tags:
        line += P.FIX_TAGS.format(tags=", ".join(action.tags))
    if action.props:
        line += P.FIX_PROPS.format(props=", ".join(f"{k}: {v}" for k, v in action.props.items()))
    return line


def schema(keys: list[str], places: list[str]) -> dict:
    return _obj({
        "changes": {"type": "array", "items": _obj({
            "key": {"enum": keys}, "op": {"enum": list(OPS)},
            "field": {"enum": [*FIELDS, ""]}, "prop": _STRING, "value": _STRING,
            "to": {"enum": [*places, ""]},
        })},
        "add": {"type": "array", "items": _STRING},
        "unclear": {"type": "boolean"},
    })


def _set(action: VaultAction, change: dict, gate, tags: list[str]) -> VaultAction | None:
    """The action with one field changed, or None when the new value has no source."""
    name, value = str(change.get("field", "")), str(change.get("value", "")).strip()
    kind = action.action
    if name in ("text", "title") and value:
        if kind == "note":
            return action.model_copy(update={"title": value}) if gate.named(value) else None
        if not gate.said(value):
            return None
        if kind in ("grocery", "append"):
            return action.model_copy(update={"body": [value]})
        return action.model_copy(update={"text": value})
    if name == "due":
        due = gate.date(value)
        return action.model_copy(update={"due": due}) if due or not value else None
    if name == "repeat":
        ok = value.lower().startswith("every") or not value
        return action.model_copy(update={"repeat": value}) if ok else None
    if name == "heading":
        heading = gate.heading(value)
        return action.model_copy(update={"heading": heading}) if heading else None
    if name == "tag":
        tag = value.lstrip("#")
        known = {t.casefold(): t for t in tags}
        return action.model_copy(update={"tags": [known[tag.casefold()]]}) \
            if tag.casefold() in known else None
    if name == "prop":
        prop = str(change.get("prop", "")).strip()
        checked = gate.prop(value)
        if not prop or checked is None:
            return None
        return action.model_copy(update={"props": {**action.props, prop: checked}})
    return None


async def plan(previous: list[VaultAction], answer: dict, gate, *, tags: list[str],
               places: dict[str, str], place_names: dict[str, str],
               move: Callable[[VaultAction, str], Awaitable[list[dict]]],
               dropping: bool = True) -> FixPlan:
    """What the model's answer means for the files. `move(action, key)` returns the raw actions
    that put `action` in place `key` — none when it could not be done."""
    its = items(previous)
    by_key = {f"a{i}": i for i in range(1, len(its) + 1)}
    out = FixPlan()
    changed: dict[int, list[dict] | None] = {}  # item index -> its raw actions now (None: gone)
    for change in answer.get("changes") or []:
        if not isinstance(change, dict) or change.get("key") not in by_key:
            continue
        i = by_key[change["key"]] - 1
        current = changed.get(i, [its[i].action.model_dump(exclude_defaults=True)])
        if current is None:  # already dropped
            continue
        what = words(its[i].action)
        op = change.get("op")
        if op == "drop" and dropping:
            changed[i] = None
            out.said.append(texts.FIX_DROPPED.format(item=what))
        elif op == "set" and len(current) == 1:
            new = _set(VaultAction(**current[0]), change, gate, tags)
            if new is not None:
                changed[i] = [new.model_dump(exclude_defaults=True)]
                out.said.append(texts.FIX_SET.format(item=what, value=change.get("value")))
        elif op == "move" and change.get("to") in places:
            key = change["to"]
            source = VaultAction(**current[0]) if len(current) == 1 else its[i].action
            moved = await move(source, key)
            if moved:
                changed[i] = moved
                out.said.append(texts.FIX_MOVED.format(item=what, place=place_names[key]))
            else:
                out.said.append(texts.FIX_NOT_MOVED.format(item=what))
    touched = sorted({its[i].write for i in changed})
    for w in touched:
        for i, item in enumerate(its):
            if item.write != w:
                continue
            if i in changed:
                out.write += changed[i] or []
            else:  # an untouched item of a write that is taken back is written again as it was
                out.write.append(item.action.model_dump(exclude_defaults=True))
    first = previous[0] if previous else None
    for name in answer.get("add") or []:
        name = str(name).strip()
        if first is None or not name or not gate.said(name):
            continue
        added = _like(first, name)
        if added is not None:
            out.write.append(added)
            out.said.append(texts.FIX_ADDED.format(item=name))
    out.take_back = touched
    out.unclear = not out.take_back and not out.write
    return out


def _like(model: VaultAction, name: str) -> dict | None:
    """A new item of the same kind, in the same place, as the turn's first action."""
    if model.action == "grocery":
        return {"action": "grocery", "body": [name]}
    if model.action in ("task", "log", "inbox"):
        return {"action": model.action, "text": name,
                **({"heading": model.heading} if model.heading else {})}
    if model.action == "note":
        return {"action": "note", "folder": model.folder, "title": name}
    if model.action == "append":
        return {"action": "append", "note": model.note, "heading": model.heading,
                "body": [name]}
    return None


def simple_move(action: VaultAction, key: str, *, tasks: str, groceries: str, diary: str,
                inbox: str, notes: dict[str, str]) -> list[dict] | None:
    """Moving between places whose shape is one line: the words carry over. None for a place
    that needs details (a folder)."""
    text = words(action)
    if key == tasks:
        return [{"action": "task", "text": text, **({"due": action.due} if action.due else {})}]
    if key == groceries:
        return [{"action": "grocery", "body": [text]}]
    if key == diary:
        return [{"action": "log", "text": text}]
    if key == inbox:
        return [{"action": "inbox", "text": text}]
    if key in notes:
        return [{"action": "append", "note": notes[key], "body": [text]}]
    return None
