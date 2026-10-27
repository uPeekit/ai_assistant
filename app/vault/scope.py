"""What one chat may see and do in the vault (app/chats.py holds the chats).

Three layers, each enough on its own to keep a group chat out of the owner's notes:

- `ScopedIndex`: the vault as the chat's turn sees it. Every reader — the staged reader's
  context, `check()`, search, the answerer, the agenda, Omnisearch results — gets only the notes
  in scope, and the tasks note with only the chat's task lines. A model never sees an
  out-of-scope note, so no answer can quote one.
- `gate()`: after `check()`, every action is held to the chat's rights and put where the
  chat's things go. Deterministic: a prompt rule alone does not hold.
- `allows_path()`: the writer refuses any other file, in case the gate has a hole.

A whole note is in scope when its *frontmatter* carries the chat's tag, or when it lies in the
chat's home folder (or below it): the owner gave the folder to the chat, and notes made there
from a template without the tag are the chat's too. An inline `#tag` only brings single task
lines of the tasks note into scope: tags in a note's text also come from task lines, and the
task archive would otherwise be handed over whole the day one of the chat's tasks was
archived."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable

from app import texts
from app.chats import ChatScope
from app.vault import frontmatter, mdedit
from app.vault.filer import GROCERY_LIST
from app.vault.index import Note, VaultIndex
from app.vault.writer import VaultAction

log = logging.getLogger(__name__)

# What each kind of action needs. Diary, groceries and the inbox are the owner's: never.
NEEDS = {"search": "ask", "agenda": "ask", "link_answer": "ask", "task": "tasks",
         "note": "add", "append": "add", "rewrite": "change", "move": "move",
         "reminder": "remind", "reminder_list": "remind", "reminder_cancel": "remind"}
NEVER = ("log", "grocery", "inbox")
MAX_SUFFIX = 20
_HEADING = re.compile(r"^#{1,6}\s+(\S.*?)\s*$")
_TASK = re.compile(r"^\s*[-*+]\s+\[.\]\s+")
_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
# An area tag the owner wrote into a task's words: the chat's task carries the chat's tag only.
_HASHTAG = re.compile(r"(?<!\S)#[^\s#]+")


def _front_tags(note: Note) -> set[str]:
    raw = note.props.get("tags") or note.props.get("tag") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(t).strip().lstrip("#").casefold() for t in raw if str(t).strip()}


def in_scope(note: Note, tag: str, folder: str = "") -> bool:
    home = folder.strip("/")
    if home and note.path.startswith(f"{home}/"):
        return True
    return bool(tag) and tag.casefold() in _front_tags(note)


def _tag_re(tag: str) -> re.Pattern:
    # The whole tag: "#club" is not "#club_my" or "#club/sub".
    return re.compile(rf"#{re.escape(tag)}(?![\w/-])", re.IGNORECASE)


class ScopedIndex(VaultIndex):
    """The same interface as VaultIndex over the chat's part of the vault. Built per turn from
    the refreshed full index; it never refreshes itself."""

    def __init__(self, base: VaultIndex, scope: ChatScope) -> None:
        super().__init__(base.root)
        self.base = base
        self.scope = scope
        self._tasks = base.by_name(texts.VAULT_TASKS_NOTE)
        self._notes = {n.path: n for n in base.notes
                       if in_scope(n, scope.tag, scope.folder)}
        if self._tasks is not None:
            self._notes[self._tasks.path] = self._tasks
        self._task_tag = _tag_re(scope.tag)

    def refresh(self) -> None:
        pass  # the turn's view: the full index was refreshed before it was built

    def note_changed(self, rel: str) -> None:
        self.base.note_changed(rel)

    def read(self, rel: str) -> str:
        if self._tasks is not None and rel == self._tasks.path:
            return self._tasks_text()
        if rel not in self._notes:
            raise FileNotFoundError(rel)  # out of scope reads as not there
        return self._unlinked(self.base.read(rel))

    def _unlinked(self, text: str) -> str:
        """Links to notes out of scope, shown as their words. The linker wraps a phrase that is
        already in the text, so the words say nothing new; the link would name an owner's note."""
        def plain(m: re.Match) -> str:
            target, shown = m.group(1).strip(), m.group(2)
            note = self.base.by_name(target.rsplit("/", 1)[-1])
            if note is not None and note.path in self._notes:
                return m.group(0)
            return shown if shown is not None else target.rsplit("/", 1)[-1]
        return _LINK.sub(plain, text)

    def _tasks_text(self) -> str:
        """The chat's own task lines, under the headings they sit under; nothing else of the
        owner's list — not even the names of the owner's sections."""
        _, body = frontmatter.split(self.base.read(self._tasks.path))
        out: list[str] = []
        heading: str | None = None
        for line in body.splitlines():
            if _HEADING.match(line):
                heading = line
            elif _TASK.match(line) and self._task_tag.search(line):
                if heading is not None:
                    out.append(heading)
                    heading = None
                out.append(line)
        return "\n".join(out) + "\n"

    def own_task(self, wanted: str) -> str | None:
        """The exact words of the chat's task line `wanted` means, or None. The writer finds a
        task in the whole file, exact match first: handing it the full words of the chat's own
        line is what keeps "call" from ticking the owner's "call" instead of "call Ann #club"."""
        if self._tasks is None:
            return None
        text = self._tasks_text()
        line_no = mdedit.find_task(text, wanted)
        if line_no is None:
            return None
        return next((line for i, _, line in mdedit.task_lines(text) if i == line_no), None)

    def guide(self) -> str:
        return self.scope.instruction.strip()

    def groceries(self) -> str:
        return ""

    def folders(self) -> list[str]:
        return [self.scope.folder] if self.scope.folder else []

    def tags(self) -> list[str]:
        return [self.scope.tag]


def _unique_title(title: str, scoped: ScopedIndex) -> str:
    """A title no out-of-scope note has: the writer finds notes by name across the vault, and
    the chat's new note must not land in the owner's note of the same name."""
    base = scoped.base
    taken = base.by_name(title)
    if taken is None or taken.path in {n.path for n in scoped.notes}:
        return title
    for i in range(1, MAX_SUFFIX + 1):
        candidate = f"{title} ({scoped.scope.tag})" if i == 1 else \
            f"{title} ({scoped.scope.tag} {i})"
        found = base.by_name(candidate)
        if found is None or found.path in {n.path for n in scoped.notes}:
            return candidate
    return f"{title} ({scoped.scope.tag} {MAX_SUFFIX + 1})"


def _in_scope_note(name: str, scoped: ScopedIndex) -> bool:
    note = scoped.by_name(name) if name else None
    return note is not None and note.name != texts.VAULT_TASKS_NOTE


def _allowed(action: VaultAction, scope: ChatScope, scoped: ScopedIndex) -> bool:
    kind = action.action
    if kind in NEVER:
        return False
    if kind == "update":
        if action.done is not None and not scope.may("tick"):
            return False
        edits = bool(action.props) or bool(action.heading and action.body) or bool(
            action.due or action.time)
        if edits and not scope.may("change"):
            return False
        if action.task or action.note == texts.VAULT_TASKS_NOTE:
            # The owner's file: a task line of the chat's, and nothing else of it — not its
            # properties, not a section's text.
            if action.props or (action.heading and action.body):
                return False
            return scoped.own_task(action.task or action.text) is not None
        return _in_scope_note(action.note, scoped)
    need = NEEDS.get(kind)
    if need is None or not scope.may(need):
        return False
    if kind in ("append", "rewrite"):
        return _in_scope_note(action.note, scoped)
    if kind == "move":
        return _in_scope_note(action.note, scoped) and _in_scope_note(action.to, scoped)
    return True


def gate(actions: list[VaultAction], scoped: ScopedIndex) -> tuple[list[VaultAction], list[str]]:
    """The actions this chat may carry out, put where its things go, and the kinds refused."""
    scope = scoped.scope
    kept: list[VaultAction] = []
    refused: list[str] = []
    # A reminder refused here takes its task (the same words) with it, as one that cannot be
    # set does in the pipeline: «remind me» never leaves a lone task behind.
    orphans = set() if scope.full or "remind" in scope.rights else {
        a.text for a in actions if a.action == "reminder"}
    for action in actions:
        if scope.full:
            kept.append(action)
            continue
        if action.action == "task" and action.text in orphans:
            continue
        if action.action == "grocery" and action.scope == GROCERY_LIST:
            refused.append("grocery")  # "what to buy" is the owner's shopping list
            continue
        if not _allowed(action, scope, scoped):
            refused.append(action.action)
            continue
        if action.action == "task":
            # The chat's tag and only that: an owner's area tag would file it as the owner's.
            words = " ".join(_HASHTAG.sub(" ", action.text).split()) or action.text
            action = action.model_copy(update={"text": words, "tags": [scope.tag],
                                               "heading": scope.tag, "countdown": False})
        elif action.action == "update" and (action.task or
                                            action.note == texts.VAULT_TASKS_NOTE):
            action = action.model_copy(update={
                "task": scoped.own_task(action.task or action.text) or action.task})
        elif action.action == "note":
            tags = [t for t in action.tags if t.casefold() != scope.tag.casefold()]
            action = action.model_copy(update={
                "folder": scope.folder, "tags": [scope.tag, *tags],
                "title": _unique_title(action.title or action.text, scoped)})
        kept.append(action)
    if refused:
        log.info("scope %s refused: %s", scope.chat_id, ", ".join(refused))
    return kept, refused


def _own_lines_only(previous: str | None, text: str, scoped: ScopedIndex) -> bool:
    """Every line the write adds to or takes from the owner's task file is the chat's: a task
    line with its tag, its section heading, or a blank line."""
    old = Counter((previous or "").split("\n"))
    new = Counter(text.split("\n"))
    for line in (new - old) + (old - new):
        heading = _HEADING.match(line)
        if not line.strip() or scoped._task_tag.search(line):
            continue
        if heading and heading.group(1).strip().casefold() == scoped.scope.tag.casefold():
            continue
        return False
    return True


def allows_path(scoped: ScopedIndex) -> Callable[[str, str | None, str], bool]:
    """For the writer: may this chat's turn write `text` over `previous` at `rel`? A new note
    only in the home folder (the turn may write it again); the owner's task file only in the
    chat's own lines; a note in scope as it likes."""
    allowed = {n.path for n in scoped.notes}
    tasks = scoped._tasks.path if scoped._tasks is not None else None
    folder = scoped.scope.folder.strip("/")
    made: set[str] = set()

    def allow(rel: str, previous: str | None = None, text: str = "") -> bool:
        if scoped.scope.full:
            return True
        if rel == tasks:
            return _own_lines_only(previous, text, scoped)
        if rel in allowed or rel in made:
            return True
        new = folder and rel.startswith(f"{folder}/") and "/" not in rel[len(folder) + 1:] \
            and not (scoped.root / rel).exists()
        if new:
            made.add(rel)
        return bool(new)

    return allow
