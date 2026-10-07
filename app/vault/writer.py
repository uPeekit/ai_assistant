"""Writing to the vault: one action in, one file changed, and a way back.

Every write is atomic (temp file + rename) and remembers the file's previous text, so Undo puts
it back exactly. Nothing is ever deleted: a note the bot created is moved to the vault's own
`.trash`, which is where Obsidian's own delete puts it."""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from app import texts
from app.vault import frontmatter, groceries, mdedit, revert
from app.vault.frontmatter import render
from app.vault.index import SKIP_DIRS, VaultIndex
from app.vault.names import safe_name, unique

log = logging.getLogger(__name__)

TRASH_DIR = ".trash"
# A property value that is one wiki link: `[[Authors/Name]]`, `[[Name]]`, `[[Name|shown as]]`.
# Group 1 is the folder part with its slash, when there is one.
_LINK = re.compile(r"^\[\[((?:[^\]|/]+/)+)?[^\]|/]+(?:\|[^\]]*)?\]\]$")
MAX_LINE = 2000


class VaultAction(BaseModel):
    """One thing to do in the vault. The filer produces these; they are checked against the
    index before they reach the writer (app/vault/filer.py)."""

    model_config = ConfigDict(extra="forbid")
    action: str  # task | note | append | update | rewrite | move | log | grocery | inbox
    text: str = ""  # the task's, log line's or inbox line's words
    note: str = ""  # which note to add to or change
    to: str = ""  # move only: the note the lines go to (made next to `note` when it is new)
    folder: str = ""
    title: str = ""
    heading: str = ""
    body: list[str] = []
    props: dict[str, str] = {}  # "" clears the property
    tags: list[str] = []
    due: str = ""
    repeat: str = ""
    countdown: bool = False
    done: bool | None = None
    task: str = ""
    # A question about dates: a day or a range to look at, and which kind of answer is wanted
    # ("day" = what is planned then, "now" = what to start with).
    due_from: str = ""
    due_to: str = ""
    scope: str = ""
    # The body is to be looked up on the web before writing: what to search for, and
    # whether text, pictures or both (app/vault/pipeline.py fills it in; the writer never
    # sees an action that still carries one).
    research: str = ""
    media: str = ""


class VaultUndo(BaseModel):
    """How to take one write back. `previous` is None when the file did not exist before.

    `written` is the text the write left behind: with both, undo removes that one change from
    whatever the file holds by then (app/vault/revert.py) instead of putting the whole old
    text back over it. None on a record made before 0.6.18, which is undone the old way."""

    model_config = ConfigDict(extra="forbid")
    path: str
    previous: str | None = None
    written: str | None = None
    # What the write did, as a VaultAction dump, so a later message can fix part of it
    # (app/vault/fix.py) without the model reading the message again. None on old records.
    action: dict | None = None


class VaultWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    path: str
    note: str
    # What exactly was written, when the note's name is not the interesting part: the
    # products a grocery write touched.
    detail: str = ""
    undo: VaultUndo | None = None

    @property
    def what(self) -> str:
        return texts.VAULT_WHAT.get(self.kind, "{note}").format(note=self.note,
                                                                 detail=self.detail)


def task_line(action: VaultAction, countdown_tag: str) -> str:
    parts = [action.text.strip()]
    parts += [f"#{t.lstrip('#')}" for t in action.tags]
    if action.countdown:
        parts.append(f"#{countdown_tag}")
    if action.repeat:
        parts.append(f"🔁 {action.repeat}")
    if action.due:
        parts.append(f"📅 {action.due}")
    mark = mdedit.DONE if action.done else mdedit.OPEN
    return f"- [{mark}] " + " ".join(p for p in parts if p)


def _as_written(action: VaultAction, write: VaultWrite) -> VaultAction:
    """The action the write really carried out. A note that was not there turns an append or
    an update into an inbox line; a fix must see that line, not the append that never
    happened."""
    if write.kind.split("_")[0] == action.action:
        return action
    words = action.text or action.title or " ".join(action.body)
    return VaultAction(action="inbox", text=words.strip()[:MAX_LINE])


class VaultWriter:
    def __init__(self, index: VaultIndex, *, countdown_tag: str = texts.VAULT_COUNTDOWN_TAG,
                 now: callable = datetime.now,
                 tag_source: Callable[[], str] | None = None) -> None:
        self._index = index
        self._root = index.root
        self._tag_source = tag_source or (lambda: countdown_tag)
        self._now = now
        # Every change to a file is read, compute, write. Writes run in worker threads and the
        # linker amends a note behind the reply, so two of them on one file would each write
        # back a text that lacks the other's line. One lock for the vault: a write is
        # milliseconds, and there is one user.
        self._lock = threading.RLock()

    # ---- files -----------------------------------------------------------------------

    def _path(self, rel: str) -> Path:
        path = (self._root / rel).resolve()
        root = self._root.resolve()
        # Nothing the index refuses to read may be written either: a note written into
        # Syncthing's archive would be invisible here and resurrected on the next sync.
        # `.trash` is the one exception: it is where this writer puts things itself.
        forbidden = SKIP_DIRS - {TRASH_DIR}
        parts = PurePosixPath(rel).parts
        if not path.is_relative_to(root) or any(p in forbidden for p in parts[:-1]):
            raise ValueError(f"refusing to write outside the vault's notes: {rel}")
        return path

    def _read(self, rel: str) -> str | None:
        try:
            return self._path(rel).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _write(self, rel: str, text: str, previous: str | None) -> VaultUndo:
        path = self._path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        self._index.note_changed(rel)
        return VaultUndo(path=rel, previous=previous, written=text)

    def read(self, rel: str) -> str:
        return self._path(rel).read_text(encoding="utf-8")

    def amend(self, rel: str, change: Callable[[str], str]) -> bool:
        """Change a note the bot has just written (the linker's pass), starting from the text
        it holds *now*: the model call that decided the change took seconds, and an Undo or the
        next message may have rewritten the file meanwhile. False when the note is gone or
        `change` leaves it as it is. Undo still takes the write's own lines out afterwards
        (app/vault/revert.py reads through the links)."""
        with self._lock:
            current = self._read(rel)
            if current is None:
                return False
            text = change(current)
            if text == current:
                return False
            self._write(rel, text, None)
            return True

    def undo(self, undo: VaultUndo) -> bool:
        """Take one write back. False when the file has changed since in the very lines the
        write touched: it is then left exactly as it is, and the caller says so."""
        with self._lock:
            path = self._path(undo.path)
            current = self._read(undo.path)
            if current is None:
                # Already gone: undone before, or deleted by hand. A file the write only
                # changed is not brought back from nothing — somebody removed it on purpose.
                return undo.previous is None
            if undo.written is None:  # a record from before 0.6.18
                restored: str | None = undo.previous or ""
            else:
                restored = revert.take_back(undo.previous or "", undo.written, current)
            if restored is None:
                return False
            if undo.previous is None and not restored.strip():
                trash = self._path(f"{TRASH_DIR}/{PurePosixPath(undo.path).name}")
                trash.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, self._unused(trash))
                self._index.note_changed(undo.path)
            else:
                self._write(undo.path, restored, None)
            return True

    @staticmethod
    def _unused(path: Path) -> Path:
        candidate, n = path, 1
        while candidate.exists():
            n += 1
            candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
        return candidate

    # ---- actions ---------------------------------------------------------------------

    def replace_writes(self, old: list[VaultUndo], new: list[VaultAction]
                       ) -> tuple[list[VaultWrite], list[int], list[str], list[str]]:
        """Take `old` writes back and write `new` in their place, as one step: a fix.

        First a dry run of every take-back on the files as they are now. If any write's lines
        were changed by hand since, nothing at all is touched and their notes are returned —
        half a fix would leave the old and the new side by side. Returns (writes, failed,
        left, stuck): the new writes, the index into `new` of each action that could not be
        written, the notes left alone, and the notes whose take-back could not be written (a
        file held by another program). The new actions are written even then: a line twice
        beats a line lost."""
        with self._lock:
            now: dict[str, str | None] = {}
            left: list[str] = []
            for undo in reversed(old):
                current = now[undo.path] if undo.path in now else self._read(undo.path)
                if current is None:
                    if undo.previous is not None:
                        left.append(PurePosixPath(undo.path).stem)
                    continue
                if undo.written is None:
                    restored: str | None = undo.previous or ""
                else:
                    restored = revert.take_back(undo.previous or "", undo.written, current)
                if restored is None:
                    left.append(PurePosixPath(undo.path).stem)
                now[undo.path] = restored
            if left:
                return [], [], list(dict.fromkeys(left)), []
            stuck: list[str] = []
            for undo in reversed(old):
                try:
                    self.undo(undo)
                except (OSError, ValueError) as e:
                    log.warning("fix take-back failed: %s", type(e).__name__)
                    stuck.append(PurePosixPath(undo.path).stem)
            writes: list[VaultWrite] = []
            failed: list[int] = []
            for i, action in enumerate(new):
                try:
                    writes.append(self.run(action))
                except (OSError, ValueError) as e:
                    log.warning("fix write failed (%s): %s", action.action, e)
                    failed.append(i)
            return writes, failed, [], list(dict.fromkeys(stuck))

    def run(self, action: VaultAction) -> VaultWrite:
        with self._lock:
            write = self._run(action)
        if write.undo is not None:
            write.undo.action = _as_written(action, write).model_dump(exclude_defaults=True)
        return write

    def _run(self, action: VaultAction) -> VaultWrite:
        handler = {
            "task": self._task, "note": self._note, "append": self._append,
            "update": self._update, "rewrite": self._rewrite, "log": self._log,
            "grocery": self._grocery, "inbox": self._inbox,
        }.get(action.action)
        if handler is None:
            return self._inbox(action)
        return handler(action)

    def _task(self, action: VaultAction) -> VaultWrite:
        rel = f"{texts.VAULT_TASKS_NOTE}.md"
        previous = self._read(rel)
        line = task_line(action, self._tag_source())
        text = previous or ""
        if action.heading:
            updated = mdedit.append_to(text, [line], action.heading, create_heading=True)
        else:
            updated = mdedit.append_to(text, [line])
        undo = self._write(rel, updated or f"{text}\n{line}\n", previous)
        return VaultWrite(kind="task", path=rel, note=texts.VAULT_TASKS_NOTE, undo=undo)

    def _note(self, action: VaultAction) -> VaultWrite:
        folder = action.folder.strip("/") or texts.VAULT_NOTES_DIR
        taken = {n.name.casefold() for n in self._index.notes}
        name = unique(safe_name(action.title or action.text), taken)
        rel = f"{folder}/{name}.md" if folder else f"{name}.md"
        props = {k: v for k, v in action.props.items() if v != ""}
        # Tags arrive as a list, or as a property holding "#a, #b" — which Obsidian would keep
        # as one string that is no tag at all. Both end as one list of bare names.
        tags = [*action.tags, *str(props.pop("tags", "")).replace(",", " ").split()]
        tags = list(dict.fromkeys(t.lstrip("#") for t in tags if t.lstrip("#")))
        if tags:
            props["tags"] = tags
        props = self._shaped(folder, props)
        undo = self._write(rel, render(props, "\n".join(action.body)), None)
        return VaultWrite(kind="note", path=rel, note=name, undo=undo)

    def _shaped(self, folder: str, props: dict) -> dict:
        """`props` with the tick boxes every note already in `folder` has.

        A folder of meetings or books is a table: its notes share properties, and a base over
        it shows a tick box only for a real boolean. The model sends every value as text and
        leaves out what the message did not mention, so this is read from the folder instead:
        a property that is a boolean in every sibling is one here too — "false" becomes False,
        and a missing one starts unticked."""
        siblings = [n for n in self._index.notes if n.folder == folder]
        if len(siblings) < 2:
            return props
        out = self._linked(siblings, props)
        shared = set.intersection(*(set(n.props) for n in siblings))
        for key in sorted(shared):
            if not all(isinstance(n.props[key], bool) for n in siblings):
                continue
            given = out.get(key)
            out[key] = (given if isinstance(given, bool)
                        else str(given).strip().casefold() in ("true", "yes"))
        return out

    @staticmethod
    def _linked(siblings: list, props: dict) -> dict:
        """`props` with plain text turned into a link wherever the folder keeps that property
        as one.

        The user's books store the author as a link into an authors folder, so a click makes
        the author's note there rather than in the vault's root. The model sends a
        name; the folder says what shape it takes. A property that is a link in most
        siblings becomes a link, into the folder most of them point at; anything else
        (a folder that keeps it as text, a value that is already a link) is left as it is."""
        out = dict(props)
        for key, value in props.items():
            text = str(value).strip() if isinstance(value, str) else ""
            if not text or text.startswith("[["):
                continue
            values = [n.props.get(key) for n in siblings if n.props.get(key)]
            links = [m for m in (_LINK.match(str(v).strip()) for v in values) if m]
            if not values or len(links) * 2 <= len(values):
                continue
            folders = [m.group(1) or "" for m in links]
            folder = max(set(folders), key=folders.count)
            out[key] = f"[[{folder}{text}]]"
        return out

    def _append(self, action: VaultAction) -> VaultWrite:
        note = self._index.by_name(action.note)
        if note is None:
            return self._inbox(action)
        previous = self._read(note.path) or ""
        lines = action.body or [action.text]
        updated = mdedit.append_to(previous, lines, action.heading or None)
        if updated is None:  # the heading is gone: the note's end is still the right place
            updated = mdedit.append_to(previous, lines) or previous
        undo = self._write(note.path, updated, previous)
        return VaultWrite(kind="append", path=note.path, note=note.name, undo=undo)

    def _update(self, action: VaultAction) -> VaultWrite:
        note = self._index.by_name(action.note)
        if note is None:
            return self._inbox(action)
        previous = self._read(note.path) or ""
        text = previous
        changed = False
        if action.props:
            siblings = [n for n in self._index.notes
                        if n.folder == note.folder and n.path != note.path]
            props = self._linked(siblings, action.props)
            text = mdedit.set_props(text, {k: (None if v == "" else v)
                                           for k, v in props.items()})
            changed = True
        if action.task or action.done is not None or action.due:
            line = mdedit.find_task(text, action.task or action.text or note.name)
            if line is not None:
                text = mdedit.set_task(text, line, checked=action.done,
                                        due=action.due or None)
                changed = True
        if action.heading and action.body:
            replaced = mdedit.replace_section(text, action.heading, action.body)
            if replaced is not None:
                text, changed = replaced, True
        if not changed:
            return self._inbox(action)
        undo = self._write(note.path, text, previous)
        return VaultWrite(kind="update", path=note.path, note=note.name, undo=undo)

    def _rewrite(self, action: VaultAction) -> VaultWrite:
        """Replace a note's text (or one section of it) with text already written by the
        rewriter — the pipeline makes that call, because it is the only part of the vault
        side that has to read a note before it can write it.

        The old version also goes to `.trash`, where it outlives the undo window: this is
        the one write that can lose something the user spent an evening on."""
        note = self._index.by_name(action.note)
        if note is None or not action.body:
            return self._inbox(action)
        previous = self._read(note.path) or ""
        if action.heading:
            text = mdedit.replace_section(previous, action.heading, action.body)
            if text is None:  # the heading is gone: better to write nothing than all of it
                return self._inbox(action)
        else:
            props, _ = frontmatter.split(previous)
            text = frontmatter.render(props, "\n".join(action.body))
        self._to_trash(note.path, previous)
        undo = self._write(note.path, text, previous)
        return VaultWrite(kind="rewrite", path=note.path, note=note.name, undo=undo)

    def _to_trash(self, rel: str, text: str) -> None:
        """A copy of what a note said before, kept whatever happens to the undo record."""
        if not text.strip():
            return
        stamp = self._now().strftime("%Y-%m-%d %H%M%S")
        name = PurePosixPath(rel).stem
        path = self._unused(self._path(f"{TRASH_DIR}/{name} ({stamp}).md"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def _grocery(self, action: VaultAction) -> VaultWrite:
        """Untick the products that have to be bought, or tick back the ones that were.

        Nothing is created and nothing is archived: the page holds one permanent line per
        product, and a message only changes which of them are ticked. A product the page
        has never heard of gets a line, which is how the registry learns."""
        rel = f"{texts.VAULT_GROCERIES_NOTE}.md"
        previous = self._read(rel)
        names = [n for n in (action.body or [action.text]) if n.strip()]
        done = bool(action.done)
        text, changed = groceries.apply(previous or groceries.note(), names, done=done)
        if not changed:  # already in the state that was asked for
            return VaultWrite(kind="grocery_none", path=rel,
                              note=texts.VAULT_GROCERIES_NOTE,
                              detail=", ".join(names))
        undo = self._write(rel, text, previous)
        return VaultWrite(kind="grocery_done" if done else "grocery", path=rel,
                          note=texts.VAULT_GROCERIES_NOTE,
                          detail=", ".join(changed), undo=undo)

    def _log(self, action: VaultAction) -> VaultWrite:
        day = self._now().strftime("%Y-%m-%d")
        rel = self._index.daily_note(day)
        previous = self._read(rel)
        line = f"- {self._now().strftime('%H:%M')} {action.text.strip()}"
        updated = mdedit.append_to(previous or "", [line]) or line
        undo = self._write(rel, updated, previous)
        return VaultWrite(kind="log", path=rel, note=day, undo=undo)

    def _inbox(self, action: VaultAction) -> VaultWrite:
        rel = f"{texts.VAULT_INBOX_NOTE}.md"
        previous = self._read(rel)
        line = f"- {(action.text or action.title or ' '.join(action.body)).strip()[:MAX_LINE]}"
        updated = mdedit.append_to(previous or "", [line]) or line
        undo = self._write(rel, updated, previous)
        return VaultWrite(kind="inbox", path=rel, note=texts.VAULT_INBOX_NOTE, undo=undo)
