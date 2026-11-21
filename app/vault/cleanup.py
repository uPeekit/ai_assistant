"""Going through the task file's undated tasks one by one, and moving done tasks to the archive.

Only the text work lives here: which tasks are asked about, and what an answer does to the task
file. The conversation is the orchestrator's (a card per message, the next message answering it),
the writing is the writer's, and reading an answer in words is a model's (app/vault/pipeline.py).

A task is found again by its own line, never by a line number: between two answers the file may
have been synced, edited by hand, or written by another message. A line that is no longer there
is left alone, and the caller skips that card."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app import texts
from app.vault import frontmatter, mdedit

log = logging.getLogger(__name__)

_TASK = re.compile(r"^(\s*)([-*+]\s+)\[([^\]])\]\s+(.*)$")
_HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
_DUE = re.compile(r"📅\s*\d{4}-\d{2}-\d{2}")
_DONE_ON = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
_REPEAT = "🔁"
_TAG = re.compile(r"(?<!\S)#[^\s#]+")
# Where a task's words end: its first tag or Tasks-plugin marker.
_TAIL = re.compile(r"\s(?:#[^\s#]|[📅🔁✅❌⏳🛫➕⏫🔼🔽⏬])")
_TIME = re.compile(r"^\d{1,2}[:.]\d{2}\s+")
CANCELLED = "-"



@dataclass(frozen=True)
class Card:
    line: str  # the task as it stands in the file, without "- [ ] "
    heading: str  # the section it is under ("" above the first heading)


def key(line: str) -> str:
    """The same task however its spacing or case: how a kept task is recognised."""
    return " ".join(line.split()).casefold()


def _body(text: str) -> tuple[dict, list[str]]:
    props, body = frontmatter.split(text)
    return props, body.split("\n")


def _join(props: dict, lines: list[str]) -> str:
    return frontmatter.render(props, "\n".join(lines))


def cards(text: str, kept: set[str]) -> list[Card]:
    """The open tasks with no date and no repeat, in file order, minus the ones kept."""
    _, lines = _body(text)
    heading, out = "", []
    for line in lines:
        h = _HEADING.match(line)
        if h:
            heading = h.group(1)
            continue
        m = _TASK.match(line)
        if not m or m.group(3) != mdedit.OPEN:
            continue
        rest = m.group(4).strip()
        if _DUE.search(rest) or _REPEAT in rest or key(rest) in kept:
            continue
        out.append(Card(rest, heading))
    return out


def _find(lines: list[str], line: str) -> int | None:
    """The one open task whose words are `line`; None when it is gone or not unique."""
    wanted = key(line)
    hits = [i for i, row in enumerate(lines)
            if (m := _TASK.match(row)) and m.group(3) == mdedit.OPEN
            and key(m.group(4)) == wanted]
    return hits[0] if len(hits) == 1 else None


def _edit(text: str, line: str, change) -> str | None:
    props, lines = _body(text)
    i = _find(lines, line)
    if i is None:
        return None
    indent, marker, mark, rest = _TASK.match(lines[i]).groups()
    lines[i] = change(indent, marker, mark, rest.strip())
    return _join(props, lines)


def done(text: str, line: str, today: date) -> str | None:
    """Ticked, with the day it was done, as Obsidian's Tasks plugin writes it."""
    return _edit(text, line, lambda ind, mk, _m, rest:
                 f"{ind}{mk}[{mdedit.DONE}] {rest} ✅ {today.isoformat()}")


def changed(text: str, line: str, *, words: str = "", heading: str = "", tag: str = "",
            due: str = "", time: str = "") -> str | None:
    """The line with new words (its leading time, tags and dates stay), a new tag in place of
    its tags, a date and a time, and under another heading — whichever are given."""
    props, lines = _body(text)
    i = _find(lines, line)
    if i is None:
        return None
    indent, marker, mark, rest = _TASK.match(lines[i]).groups()
    rest = rest.strip()
    if words.strip():
        old, tail = _split(rest)
        leading = _TIME.match(old)
        rest = f"{leading.group(0) if leading else ''}{words.strip()}{tail}"
    if tag.strip():
        rest = " ".join(_TAG.sub(" ", rest).split())
        old, tail = _split(rest)
        rest = f"{old} #{tag.strip().lstrip('#')}{tail}"
    if due or time:
        if due:
            rest = _DUE.sub("", rest).strip()
        rest = mdedit.with_time(rest, time)
        if due:
            rest = f"{rest} 📅 {due}"
    if not heading.strip():
        lines[i] = f"{indent}{marker}[{mark}] {rest}"
        return _join(props, lines)
    del lines[i]
    return mdedit.append_to(_join(props, lines), [f"- [{mark}] {rest}"], heading.strip(),
                            create_heading=True)


def dated(text: str, line: str, due: str, time: str = "") -> str | None:
    return changed(text, line, due=due, time=time)


def renamed(text: str, line: str, words: str) -> str | None:
    return changed(text, line, words=words)


def moved(text: str, line: str, heading: str, tag: str = "") -> str | None:
    return changed(text, line, heading=heading, tag=tag)


def _split(rest: str) -> tuple[str, str]:
    """(the words, everything after them: tags, dates, markers)."""
    m = _TAIL.search(rest)
    return (rest[:m.start()], rest[m.start():]) if m else (rest, "")


def section_tag(text: str, heading: str) -> str:
    """The tag the tasks under `heading` carry most often, "" when none: what a task moved
    there should wear, when nobody named one."""
    _, lines = _body(text)
    counts: dict[str, int] = {}
    current = ""
    for line in lines:
        h = _HEADING.match(line)
        if h:
            current = h.group(1)
            continue
        m = _TASK.match(line)
        if m and current.casefold() == heading.casefold():
            for tag in _TAG.findall(m.group(4)):
                counts[tag.lstrip("#")] = counts.get(tag.lstrip("#"), 0) + 1
    return max(counts, key=lambda t: counts[t]) if counts else ""


def headings(text: str) -> list[str]:
    _, lines = _body(text)
    return [m.group(1) for line in lines if (m := _HEADING.match(line))]


def dropped(text: str, line: str, today: date) -> tuple[str, str] | None:
    """(the file without it, the line for the archive, marked cancelled)."""
    props, lines = _body(text)
    i = _find(lines, line)
    if i is None:
        return None
    rest = _TASK.match(lines[i]).group(4).strip()
    del lines[i]
    return _join(props, lines), f"- [{CANCELLED}] {rest} ❌ {today.isoformat()}"


def archive_done(text: str, today: date) -> tuple[str, list[str]]:
    """(the file without its ticked tasks, the lines taken out). A task ticked today stays,
    so its Undo still finds it; one ticked with no date goes."""
    props, lines = _body(text)
    kept, moved_out = [], []
    for row in lines:
        m = _TASK.match(row)
        if m and m.group(3) == mdedit.DONE:
            when = _DONE_ON.search(m.group(4))
            if not (when and when.group(1) >= today.isoformat()):
                moved_out.append(f"- [{mdedit.DONE}] {m.group(4).strip()}")
                continue
        kept.append(row)
    return _join(props, kept), moved_out


def without(text: str, rows: list[str]) -> str:
    """The file without these ticked or open lines, each taken out once."""
    props, lines = _body(text)
    left = list(rows)
    out = []
    for row in lines:
        m = _TASK.match(row)
        plain = f"- [{m.group(3)}] {m.group(4).strip()}" if m else None
        if plain is not None and plain in left:
            left.remove(plain)
            continue
        out.append(row)
    return _join(props, out)


def prepended(archive: str, rows: list[str]) -> str:
    """`rows` at the top of the archive, newest batch first, the rest as it was."""
    old = archive.lstrip("\n")
    return "\n".join(rows) + "\n" + (old if old.endswith("\n") or not old else old + "\n")


def weekly(value: str) -> tuple[int, str] | None:
    """"<weekday> 04:00" -> (0..6, "04:00"): a weekday as texts.VAULT_WEEKDAYS_SHORT spells it
    (Monday when none is given) and a time."""
    parts = value.strip().casefold().split()
    if not parts:
        return None
    day = 0
    if parts[0][:2] in texts.VAULT_WEEKDAYS_SHORT and len(parts) == 2:
        day, parts = texts.VAULT_WEEKDAYS_SHORT.index(parts[0][:2]), parts[1:]
    if len(parts) != 1 or not re.fullmatch(r"\d{1,2}:\d{2}", parts[0]):
        return None
    return day, parts[0]


def due_on(value: str, day: date) -> bool:
    """Is the weekly run due on `day`? Never when the setting is empty or unreadable."""
    when = weekly(value)
    return when is not None and day.weekday() == when[0]


class Kept:
    """The tasks the owner chose to keep without a date: never asked about again. A file in
    data/, not a mark in the task line, so the vault stays the owner's own text."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        if path is not None and path.exists():
            try:
                self._keys = set(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as e:
                log.warning("could not read %s (%s); asking about every undated task", path, e)

    def all(self) -> set[str]:
        with self._lock:
            return set(self._keys)

    def add(self, line: str) -> None:
        with self._lock:
            self._keys.add(key(line))
            if self._path is None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(sorted(self._keys), ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self._path)
