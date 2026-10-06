# Reliability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the data-loss and lost-message gaps found in the 0.6.17 review — vault Undo that erases newer writes, partial writes and linker races, messages dropped when Claude is down, long notes truncated by a rewrite, Telegram messages over 4 096 characters, mail skipped past the cap, the agenda re-sent on restart, the Notion undo on long pages — plus one speed-up of the staged reader and one admin-page guard (Part 1, Tasks 1–16); then let the user fix part of a vault write with a Поправить button or a reply (Part 2, Tasks 17–21).

**Architecture:** Every fix is a guard in the deterministic layer; no prompt changes. The one new module is `app/vault/revert.py` (a pure line-level reverse diff) that `VaultWriter.undo` uses instead of restoring a whole file, and `app/telegram/sending.py` (message splitting) that replies and digests share. Each task is independently shippable and ends with the full suite green.

**Tech Stack:** Python 3.12, `uv`, `pytest` + `pytest-asyncio` (auto mode), `ruff`, `anthropic` SDK faked in tests, `python-telegram-bot` 22.

**Spec:** `docs/superpowers/specs/2026-10-06-reliability-fixes-design.md` — the findings, what was dropped and why, and the three behaviour changes the user may veto. Every fix below was prototyped on a throwaway clone with the whole suite green before the plan was written; the code in the steps is that prototype.

## Global Constraints

- Run `uv run pytest -q` and `uv run ruff check .` before every commit; both clean. The suite is ~1 300 tests and takes under a minute.
- **Line endings are mixed in this repo** (`app/vault/linker.py`, `app/vault/index.py`, `app/vault/pipeline.py`, `app/conversation/orchestrator.py` and most tests are CRLF; `app/vault/writer.py` is LF). Edit with the Edit tool so each file keeps its own endings. Never write file contents through a shell heredoc or a Python script.
- No Cyrillic in `app/` outside `app/texts.py`, `app/llm/prompts.py`, `app/llm/context.py`, `app/llm/staged_prompts.py` (`tests/test_reply.py` enforces it). Every user-facing string goes in `app/texts.py`.
- No message text and no URLs in logs above DEBUG (`tests/test_security.py`).
- The vault pipeline never asks a question and never raises to the orchestrator.
- Commit messages end with a second `-m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`.
- Work on a branch from `main` (`reliability-fixes`); merge to main and push when green (standing instruction). Releases and prod updates are the user's.

## Review Focus

1. **A file rewritten with CRLF endings by Obsidian/Syncthing between a write and its Undo** — the lines are the same lines and Undo must still take its own line out: Task 2, `test_undo_reads_through_line_endings_another_program_wrote`.
2. **A reply of exactly 4 096 characters, and a single line longer than that** — one message, and a hard cut, never a Telegram refusal: Task 9, `test_pieces_cut_at_line_ends_and_only_hard_when_they_must`.
3. **A broken `data/daily_state.json`** (half-written, not JSON) — the morning message still goes out and the file is rewritten: Task 11, `test_a_broken_memory_file_does_not_stop_the_morning_message`.
4. **The target call failing while the intent call succeeds** — the message is still filed, with one retry, and a non-"add" intent never waits on it: Task 14, `test_a_target_call_that_failed_is_asked_again_rather_than_lost` and the `done` half of `test_the_target_is_asked_alongside_the_intent_and_dropped_for_other_intents`.
5. **An Undo pressed while Claude is down and the message was only "kept"** — the inbox line is a write like any other, with its own undo record: Task 7, the `len(turn.undos) == 1` assertion in `test_a_model_that_is_down_costs_the_filing_not_the_words`.

---

### Task 1: Taking one write back out of a changed text (`app/vault/revert.py`)

**Files:**
- Create: `app/vault/revert.py`
- Create: `tests/test_vault_revert.py`

**Interfaces:**
- Produces: `take_back(previous: str, written: str, current: str) -> str | None` — `current` with the change `previous → written` removed; `None` when a line that change added or replaced is not in `current` as written (through `[[links]]`, case-insensitively), or the place of a removed line cannot be found. Task 2 calls it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vault_revert.py`:

```python
"""Taking one write back out of a text that has changed since (app/vault/revert.py)."""

from __future__ import annotations

from app.vault.revert import take_back

BEFORE = "## дом\n\n- [ ] счета\n\n## разное\n\n- [ ] посылка\n"


def test_an_untouched_file_goes_back_exactly():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    assert take_back(BEFORE, written, written) == BEFORE


def test_a_line_added_elsewhere_since_is_kept():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    now = written.replace("- [ ] посылка\n", "- [ ] посылка\n- [ ] позвонить\n")
    assert take_back(BEFORE, written, now) == BEFORE.replace(
        "- [ ] посылка\n", "- [ ] посылка\n- [ ] позвонить\n")


def test_a_line_added_right_after_ours_is_kept():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    now = written.replace("- [ ] лампочки\n", "- [ ] лампочки\n- [ ] позвонить\n")
    assert take_back(BEFORE, written, now) == BEFORE.replace(
        "- [ ] счета\n", "- [ ] счета\n- [ ] позвонить\n")


def test_a_tick_is_put_back_when_another_line_was_ticked_since():
    before = "- [x] молоко\n- [x] яйца\n- [x] хлеб\n"
    written = before.replace("- [x] молоко", "- [ ] молоко")
    now = written.replace("- [x] хлеб", "- [ ] хлеб")
    assert take_back(before, written, now) == "- [x] молоко\n- [x] яйца\n- [ ] хлеб\n"


def test_our_line_changed_since_is_a_conflict():
    before = "- [x] молоко\n- [x] яйца\n"
    written = before.replace("- [x] молоко", "- [ ] молоко")
    now = written.replace("- [ ] молоко", "- [ ] молоко 2 л")
    assert take_back(before, written, now) is None


def test_our_line_deleted_since_is_a_conflict():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    now = written.replace("- [ ] лампочки\n", "")
    assert now == BEFORE and take_back(BEFORE, written, now) is None


def test_a_removed_line_goes_back_where_it_was():
    written = BEFORE.replace("- [ ] счета\n", "")
    now = written + "- [ ] новое\n"
    assert take_back(BEFORE, written, now) == BEFORE + "- [ ] новое\n"


def test_a_removed_line_with_nothing_left_around_it_is_a_conflict():
    assert take_back("старое", "", "совсем другое") is None


def test_links_added_since_do_not_hide_our_line():
    written = BEFORE + "- [ ] дочитать Чапаев и Пустота\n"
    now = written.replace("Чапаев и Пустота", "[[Чапаев и Пустота]]")
    assert take_back(BEFORE, written, now) == BEFORE
    aliased = written.replace("Чапаев и Пустота", "[[Чапаев и Пустота|чапаев и пустота]]")
    assert take_back(BEFORE, written, aliased) == BEFORE


def test_a_whole_rewrite_changed_since_is_a_conflict():
    before = "один\nдва\nтри\n"
    written = "совсем\nновый\nтекст\n"
    assert take_back(before, written, written + "приписка\n") == before + "приписка\n"
    assert take_back(before, written, "совсем\nдругой\nтекст\n") is None


def test_a_created_file_is_emptied_of_what_the_write_put_there():
    written = "- первое\n"
    assert take_back("", written, written + "- второе\n").strip() == "- второе"
    assert take_back("", written, written) == ""
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `uv run pytest tests/test_vault_revert.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.vault.revert'`

- [ ] **Step 3: Write the module**

Create `app/vault/revert.py`:

```python
"""Taking one write back out of a file that has changed since.

Undo used to put a file's whole previous text back. Two messages a minute apart each add a
line to the task file; undoing the first restored the text from before *both*, and the second
task was gone. A write is taken back as a change instead: the lines it added are removed and
the lines it removed are put back, wherever they now are. When those lines are no longer there
as the write left them, nothing is changed and the caller says so."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# [[note|phrase]] and [[note]]: what the linker wraps a phrase in after the write.
_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")


def _key(line: str) -> str:
    """A line as it compares. The linker links a phrase a moment after the write — and when
    the phrase is the note's own name, in the note's spelling — which is still the same line."""
    return _LINK.sub(r"\1", line).casefold()


def take_back(previous: str, written: str, current: str) -> str | None:
    """`current` without the change that turned `previous` into `written`.

    None when a line that change added or replaced is not in `current` as it was written, or
    the place a removed line came from cannot be found: guessing there would damage text that
    somebody wrote after the bot did."""
    if current == written:
        return previous
    before, after, now = previous.split("\n"), written.split("\n"), current.split("\n")
    # Where each line of the written text is in the file as it is now.
    at: dict[int, int] = {}
    matcher = SequenceMatcher(None, [_key(x) for x in after], [_key(x) for x in now],
                              autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            at[a + k] = b + k
    changes: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, after, before,
                                               autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:
            # The write removed lines here and added none: they go back between the lines
            # that stood around them, one of which has to be found.
            if i1 in at:
                start = at[i1]
            elif i1 - 1 in at:
                start = at[i1 - 1] + 1
            else:
                return None
            end = start
        else:
            if any(k not in at for k in range(i1, i2)) or at[i2 - 1] - at[i1] != i2 - 1 - i1:
                return None  # not all there, or no longer next to each other
            start, end = at[i1], at[i2 - 1] + 1
        changes.append((start, end, before[j1:j2]))
    for start, end, lines in sorted(changes, key=lambda c: c[0], reverse=True):
        now[start:end] = lines
    return "\n".join(now)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_vault_revert.py -q`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add app/vault/revert.py tests/test_vault_revert.py
git commit -m "feat(vault): a write can be taken back out of a text that changed since" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The writer takes back its own change, under one lock

**Files:**
- Modify: `app/vault/writer.py` (imports, `VaultUndo`, `VaultWriter.__init__`, `_write`, `replace` → `amend`, `undo`, `run`)
- Test: `tests/test_vault_writer.py`

**Interfaces:**
- Consumes: `app.vault.revert.take_back` (Task 1).
- Produces: `VaultUndo.written: str | None` (new field; old rows have `None`); `VaultWriter.undo(undo) -> bool` (`False` = the file changed since in those lines and was left alone); `VaultWriter.amend(rel: str, change: Callable[[str], str]) -> bool` (replaces `replace`; Task 6 uses it); `VaultWriter.run` and `undo` and `amend` all take `self._lock` (a `threading.RLock`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vault_writer.py`:

```python


# ---- undo takes back one write, not the file ---------------------------------------------------

def test_undo_of_an_older_write_keeps_the_newer_one(writer, index):
    """Two tasks a minute apart, then Undo on the first: restoring the file's whole previous
    text also erased the second task."""
    first = writer.run(VaultAction(action="task", text="купить лампочки", heading="дом"))
    writer.run(VaultAction(action="task", text="позвонить маме", heading="дом"))

    assert writer.undo(first.undo) is True

    text = index.read(first.path)
    assert "купить лампочки" not in text
    assert "- [ ] позвонить маме" in text
    assert "- [ ] платить счета" in text  # what was there before either is untouched


def test_undo_leaves_a_file_alone_when_its_own_line_was_changed_since(writer, index):
    first = writer.run(VaultAction(action="task", text="купить лампочки", heading="дом"))
    edited = index.read(first.path).replace("- [ ] купить лампочки", "- [x] купить лампочки")
    (index.root / first.path).write_text(edited, encoding="utf-8", newline="\n")

    assert writer.undo(first.undo) is False
    assert index.read(first.path) == edited


def test_undo_of_the_write_that_made_a_file_keeps_what_came_after(writer, index, vault):
    """The inbox note did not exist; the first message made it, the second added a line. Undo
    of the first used to move the whole file — second line included — to the trash."""
    first = writer.run(VaultAction(action="inbox", text="первое"))
    writer.run(VaultAction(action="inbox", text="второе"))

    assert writer.undo(first.undo) is True

    assert index.read(first.path).strip() == "- второе"
    assert not (vault / ".trash" / f"{texts.VAULT_INBOX_NOTE}.md").exists()


def test_undo_survives_the_links_the_linker_added(writer, index):
    """The linker rewrites a line the bot wrote a moment later: `[[note|phrase]]` around a
    phrase. That is still the line this write added."""
    write = writer.run(VaultAction(action="append", note="дом", heading="Заметки",
                                    body=["спросить про Чапаев и Пустота"]))
    linked = index.read(write.path).replace("Чапаев и Пустота", "[[Чапаев и Пустота]]")
    (index.root / write.path).write_text(linked, encoding="utf-8", newline="\n")

    assert writer.undo(write.undo) is True
    assert "спросить про" not in index.read(write.path)


def test_undo_of_a_note_that_is_gone_is_not_an_error(writer, vault):
    created = writer.run(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR, title="Идея",
                                      body=["текст"]))
    (vault / created.path).unlink()
    assert writer.undo(created.undo) is True


def test_undo_reads_through_line_endings_another_program_wrote(writer, index):
    """Obsidian on the phone, through Syncthing, can write the file back with CRLF endings.
    The lines are the same lines."""
    first = writer.run(VaultAction(action="task", text="купить лампочки", heading="дом"))
    text = index.read(first.path) + "- [ ] позвонить маме\n"
    (index.root / first.path).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    assert writer.undo(first.undo) is True
    after = index.read(first.path)
    assert "купить лампочки" not in after and "позвонить маме" in after
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_vault_writer.py -q`
Expected: the six new tests FAIL (`undo` returns `None`, and the first keeps "позвонить маме" out); the existing 24 pass.

- [ ] **Step 3: Change the writer**

In `app/vault/writer.py`, add `import threading` after `import os`, and change the vault import to:

```python
from app.vault import frontmatter, groceries, mdedit, revert
```

Replace the `VaultUndo` class:

```python
class VaultUndo(BaseModel):
    """How to take one write back. `previous` is None when the file did not exist before.

    `written` is the text the write left behind: with both, undo removes that one change from
    whatever the file holds by then (app/vault/revert.py) instead of putting the whole old
    text back over it. None on a record made before 0.6.18, which is undone the old way."""

    model_config = ConfigDict(extra="forbid")
    path: str
    previous: str | None = None
    written: str | None = None
```

In `VaultWriter.__init__`, after `self._now = now` add:

```python
        # Every change to a file is read, compute, write. Writes run in worker threads and the
        # linker amends a note behind the reply, so two of them on one file would each write
        # back a text that lacks the other's line. One lock for the vault: a write is
        # milliseconds, and there is one user.
        self._lock = threading.RLock()
```

In `_write`, change the last line to `return VaultUndo(path=rel, previous=previous, written=text)`.

Replace `replace` and `undo` (the two methods between `read` and `_unused`) with:

```python
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
```

Replace the start of `run`:

```python
    def run(self, action: VaultAction) -> VaultWrite:
        with self._lock:
            return self._run(action)

    def _run(self, action: VaultAction) -> VaultWrite:
        handler = {
```

(the rest of the old `run` body stays, now under `_run`).

- [ ] **Step 4: Run the writer tests**

Run: `uv run pytest tests/test_vault_writer.py -q`
Expected: all pass — including `test_the_writer_stays_inside_the_vault` (the path check still runs first) and `test_undo_restores_text_and_trashes_what_was_created` (an untouched file goes back exactly).

- [ ] **Step 5: Keep the linker working until Task 6**

`Linker.link` calls `self._writer.replace`, which no longer exists. In `app/vault/linker.py`, change the one call `self._writer.replace(write.path, text.replace(body, linked, 1))` to:

```python
        self._writer.amend(write.path, lambda _current: text.replace(body, linked, 1))
```

(Task 6 replaces this whole block with one that works from `_current`; this keeps the suite green between tasks.)

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/vault/writer.py app/vault/linker.py tests/test_vault_writer.py
git commit -m "fix(vault): undo takes back one write, not the file; one lock for every write" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: An Undo button on vault-only replies

**Files:**
- Modify: `app/conversation/orchestrator.py` (`_finish_vault`)
- Test: `tests/test_vault_orchestrator.py` (`test_notion_off_leaves_a_working_obsidian_bot`)

**Interfaces:**
- Consumes: `_record_vault_undo` sets `turn.execution_id`; `_undo_buttons(execution_id)` already exists.
- Produces: a vault-only reply has `buttons == [[Button("u:<id>", BTN_UNDO)]]` and `undo_id == <id>` when it carries no question keyboard. Task 4's tests use `reply.undo_id`.

- [ ] **Step 1: Change the pinned test**

In `tests/test_vault_orchestrator.py`, in `test_notion_off_leaves_a_working_obsidian_bot`, replace:

```python
    assert reply.text.startswith("✅ Obsidian —") and not reply.buttons
    assert bot.llm.calls == 0 and bot.notion.calls == []
    assert "- [ ] зубы" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert json.loads(executions(bot)[0]["undo"])["kind"] == "vault"
```

with:

```python
    assert reply.text.startswith("✅ Obsidian —")
    assert bot.llm.calls == 0 and bot.notion.calls == []
    assert "- [ ] зубы" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    [row] = executions(bot)
    assert json.loads(row["undo"])["kind"] == "vault"
    # No question, so the only button is Undo for the vault's own write.
    assert [[b.id for b in r] for r in reply.buttons] == [[f"u:{row['id']}"]]
    assert reply.undo_id == row["id"]

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{row['id']}")
    assert undone.text == texts.UNDONE
    assert "зубы" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_vault_orchestrator.py -q -k notion_off`
Expected: FAIL on the buttons assertion (`reply.buttons == []`).

- [ ] **Step 3: Attach the button**

In `app/conversation/orchestrator.py`, `_finish_vault`, replace:

```python
        undos = result.undos
        if undos:
            self._record_vault_undo(turn, undos)
```

with:

```python
        undos = result.undos
        if undos:
            self._record_vault_undo(turn, undos)
            if not reply.buttons and turn.execution_id is not None:
                # Notion wrote nothing, or is switched off: the vault's write is the only
                # thing to take back, and it gets the same button a Notion write has. A reply
                # that already carries buttons is a question; /undo still reaches the row.
                reply = replace(reply, buttons=_undo_buttons(turn.execution_id),
                                undo_id=turn.execution_id)
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/conversation/orchestrator.py tests/test_vault_orchestrator.py
git commit -m "feat: a vault-only reply carries the Undo button its row already had" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Undo says which note it left alone

**Files:**
- Modify: `app/vault/pipeline.py` (`VaultPipeline.undo`)
- Modify: `app/conversation/orchestrator.py` (`_undo`)
- Modify: `app/texts.py` (`VAULT_UNDO_LEFT`)
- Test: `tests/test_vault_orchestrator.py`

**Interfaces:**
- Consumes: `VaultWriter.undo -> bool` (Task 2); `reply.undo_id` on vault-only replies (Task 3).
- Produces: `VaultPipeline.undo(undos) -> list[str]` — the note names (file stems) left alone. `texts.VAULT_UNDO_LEFT` with a `{notes}` placeholder.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vault_orchestrator.py`:

```python


# ---- undo takes back one message, not the file ----------------------------------------------

async def test_undo_of_the_first_of_two_messages_keeps_the_second(bot, tmp_path):
    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
    bot.claude.answers = [
        {"actions": [{"action": "task", "text": "лампочки", "heading": "дом"}]},
        {"actions": [{"action": "task", "text": "позвонить маме", "heading": "дом"}]},
    ]
    first = await bot.orch.handle_text(CHAT, USER, "лампочки")
    await bot.orch.handle_text(CHAT, USER, "позвонить маме")

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{first.undo_id}")

    assert undone.text == texts.UNDONE
    tasks = bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "лампочки" not in tasks and "- [ ] позвонить маме" in tasks


async def test_undo_says_so_when_the_line_was_changed_by_hand_since(bot, tmp_path):
    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
    bot.claude.answers = [{"actions": [{"action": "task", "text": "лампочки",
                                         "heading": "дом"}]}]
    reply = await bot.orch.handle_text(CHAT, USER, "лампочки")
    path = bot.dir / f"{texts.VAULT_TASKS_NOTE}.md"
    edited = path.read_text(encoding="utf-8").replace("- [ ] лампочки", "- [x] лампочки")
    path.write_text(edited, encoding="utf-8", newline="\n")

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")

    assert undone.text == texts.VAULT_UNDO_LEFT.format(notes=f"«{texts.VAULT_TASKS_NOTE}»")
    assert path.read_text(encoding="utf-8") == edited
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_vault_orchestrator.py -q`
Expected: the second new test FAILS (`AttributeError: VAULT_UNDO_LEFT`); the first passes already (Task 2 did the work) and stays as the end-to-end pin.

- [ ] **Step 3: The text**

In `app/texts.py`, after the line `VAULT_NOTHING_TO_MOVE = "не нашёл, что переносить — ничего не тронул"` add:

```python
# Undo found the file changed since, in the very lines it would take back.
VAULT_UNDO_LEFT = "↩️ Не отменил в {notes}: там с тех пор что-то изменили, и я не стал трогать."
```

- [ ] **Step 4: The pipeline reports what it left**

In `app/vault/pipeline.py`, replace the `undo` method at the end of `VaultPipeline`:

```python
    async def undo(self, undos: list[VaultUndo]) -> list[str]:
        """Take every write of one turn back, newest first. Returns the notes that have
        changed since in the lines the turn wrote: those are left as they are."""
        left: list[str] = []
        for undo in reversed(undos):
            if not await asyncio.to_thread(self._writer.undo, undo):
                left.append(PurePosixPath(undo.path).stem)
        return left
```

(`PurePosixPath` is already imported there.)

- [ ] **Step 5: The orchestrator says so**

In `app/conversation/orchestrator.py`, `_undo`, replace:

```python
        vault_undos = _vault_undos(record)
        if vault_undos and self._vault is not None:
            try:
                await self._vault.undo(vault_undos)
            except Exception:  # Notion is already back: say so rather than fail the undo
                log.exception("undoing the vault side failed")
        self._store.mark_undone(row["id"])
        turn.audit(decision=_kind("UNDO"))
        return Reply(texts.UNDONE)
```

with:

```python
        vault_undos = _vault_undos(record)
        left: list[str] = []
        if vault_undos and self._vault is not None:
            try:
                left = await self._vault.undo(vault_undos)
            except Exception:  # Notion is already back: say so rather than fail the undo
                log.exception("undoing the vault side failed")
        self._store.mark_undone(row["id"])
        turn.audit(decision=_kind("UNDO"))
        if not left:
            return Reply(texts.UNDONE)
        # A note somebody changed since, in the lines this turn wrote, is left as it is.
        notes = ", ".join(f"«{name}»" for name in dict.fromkeys(left))
        kept = texts.VAULT_UNDO_LEFT.format(notes=notes)
        undone_some = record.kind != "vault" or len(left) < len(vault_undos)
        return Reply(f"{texts.UNDONE}\n{kept}" if undone_some else kept)
```

- [ ] **Step 6: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/vault/pipeline.py app/conversation/orchestrator.py app/texts.py tests/test_vault_orchestrator.py
git commit -m "fix: undo names the note it left alone instead of overwriting a later change" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: One failed write does not hide the others

**Files:**
- Modify: `app/vault/pipeline.py` (`VaultTurn.reply_line`, `handle` write step, `_write_all`)
- Modify: `app/texts.py` (`VAULT_SOME_FAILED`)
- Test: `tests/test_vault_filer.py`

**Interfaces:**
- Produces: `_write_all(actions) -> tuple[list[VaultWrite], list[str]]`; `reply_line` shows the writes and appends `remark`/error after " — " when both exist (Task 7 relies on this). `texts.VAULT_SOME_FAILED` with `{n}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vault_filer.py`:

```python


# ---- one write failing does not hide the others ---------------------------------------------

async def test_a_write_that_fails_does_not_cost_the_others_their_undo(index):
    pipe = pipeline(index, {"actions": [
        {"action": "task", "text": "первое", "heading": "дом"},
        {"action": "log", "text": "второе"},
        {"action": "task", "text": "третье", "heading": "дом"},
    ]})
    real = pipe._writer.run

    def flaky(action):
        if action.action == "log":
            raise OSError("disk says no")
        return real(action)

    pipe._writer.run = flaky
    turn = await pipe.handle("первое, второе, третье")

    assert [w.kind for w in turn.writes] == ["task", "task"]
    assert len(turn.undos) == 2
    line = turn.reply_line()
    assert line.startswith(texts.VAULT_REPLY.split("{")[0])
    assert texts.VAULT_SOME_FAILED.format(n=1) in line
    tasks = index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "первое" in tasks and "третье" in tasks


async def test_when_every_write_fails_the_reply_says_not_written(index):
    pipe = pipeline(index, {"actions": [{"action": "task", "text": "зубы", "heading": "дом"}]})

    def broken(action):
        raise OSError("disk says no")

    pipe._writer.run = broken
    turn = await pipe.handle("зубы")
    assert turn.writes == [] and turn.error == "OSError"
    assert texts.VAULT_FAILED.split("{")[0] in turn.reply_line()
```

- [ ] **Step 2: Run them to see the first fail**

Run: `uv run pytest tests/test_vault_filer.py -q -k "fails or every_write"`
Expected: the first FAILS (`turn.writes == []`), the second passes (it pins today's behaviour for the all-failed case).

- [ ] **Step 3: The text**

In `app/texts.py`, after the `VAULT_UNDO_LEFT` line from Task 4 add:

```python
# Some of a message's writes failed after others were already on disk.
VAULT_SOME_FAILED = "ещё {n} записать не удалось"
```

- [ ] **Step 4: The pipeline**

In `app/vault/pipeline.py`, `VaultTurn.reply_line`, replace the opening:

```python
    def reply_line(self) -> str:
        if self.error:
            why = texts.LLM_DOWN_SHORT.get(self.reason) or self.error
            return texts.VAULT_FAILED.format(error=why)
        if self.answer and not self.writes:
```

with:

```python
    def reply_line(self) -> str:
        why = (texts.LLM_DOWN_SHORT.get(self.reason) or self.error) if self.error else ""
        if why and not self.writes:
            return texts.VAULT_FAILED.format(error=why)
        if self.answer and not self.writes:
```

and, further down in the same method, replace:

```python
        if self.remark:
            what += f" — {self.remark}"
        line = texts.VAULT_REPLY.format(what=what)
```

with:

```python
        # What was written is said even when something else went wrong: those files are on
        # disk and Undo reaches them, so a bare "not written" would be untrue twice over.
        notes = "; ".join(n for n in (self.remark, why) if n)
        if notes:
            what += f" — {notes}"
        line = texts.VAULT_REPLY.format(what=what)
```

In `handle`, replace:

```python
        try:
            turn.writes = await asyncio.to_thread(self._write_all, actions)
        except (OSError, ValueError) as e:
            log.warning("vault write failed: %s", e)
            turn.error = type(e).__name__
            return turn
```

with:

```python
        turn.writes, failed = await asyncio.to_thread(self._write_all, actions)
        if failed and not turn.writes:
            turn.error = failed[0]
            return turn
        if failed:
            turn.remark = turn.remark or texts.VAULT_SOME_FAILED.format(n=len(failed))
```

Replace `_write_all`:

```python
    def _write_all(self, actions: list[VaultAction]) -> tuple[list[VaultWrite], list[str]]:
        """What was written, and the error of each action that could not be. One file that
        cannot be written must not hide the ones already on disk, or cost them their Undo."""
        writes: list[VaultWrite] = []
        failed: list[str] = []
        for action in actions:
            try:
                writes.append(self._writer.run(action))
            except (OSError, ValueError) as e:
                log.warning("vault write failed (%s): %s", action.action, e)
                failed.append(type(e).__name__)
        return writes, failed
```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/vault/pipeline.py app/texts.py tests/test_vault_filer.py
git commit -m "fix(vault): a write that fails no longer hides the ones already on disk" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The linker writes from the text as it is now

**Files:**
- Modify: `app/vault/linker.py` (`link`, `import asyncio`)
- Modify: `app/vault/index.py` (readers iterate a copy)
- Test: `tests/test_vault_filer.py`

**Interfaces:**
- Consumes: `VaultWriter.amend` (Task 2).
- Produces: nothing new; `Linker.link` keeps its signature.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vault_filer.py`:

```python


# ---- the linker writes from the text as it is now --------------------------------------------

class _Meanwhile(FakeAnthropic):
    """A model call during which something else happens to the vault."""

    def __init__(self, meanwhile, *answers) -> None:
        super().__init__(*answers)
        self._meanwhile = meanwhile

    async def create(self, **kwargs):
        self._meanwhile()
        return await super().create(**kwargs)


async def test_the_linker_keeps_a_line_written_while_the_model_was_thinking(index):
    writer = VaultWriter(index, now=lambda: NOW)
    write = writer.run(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR, title="Мысль",
                                   body=["перечитать Пелевина"]))

    def next_message() -> None:
        writer.run(VaultAction(action="append", note="Мысль", body=["и Сорокина тоже"]))

    client = _Meanwhile(next_message,
                        {"links": [{"phrase": "Пелевина", "note": "Чапаев и Пустота"}]})
    assert await Linker(index, writer, model="m", client=client).link(write) == 1
    text = index.read(write.path)
    assert "[[Чапаев и Пустота|Пелевина]]" in text and "и Сорокина тоже" in text


async def test_the_linker_does_not_bring_back_a_note_that_was_undone_meanwhile(index, tmp_path):
    writer = VaultWriter(index, now=lambda: NOW)
    write = writer.run(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR, title="Мысль",
                                   body=["перечитать Пелевина"]))
    client = _Meanwhile(lambda: writer.undo(write.undo),
                        {"links": [{"phrase": "Пелевина", "note": "Чапаев и Пустота"}]})
    assert await Linker(index, writer, model="m", client=client).link(write) == 0
    assert not (tmp_path / write.path).exists()


async def test_a_linked_note_can_still_be_undone(index, tmp_path):
    before = index.read(f"{texts.VAULT_AREAS_DIR}/дом.md")
    writer = VaultWriter(index, now=lambda: NOW)
    write = writer.run(VaultAction(action="append", note="дом",
                                   body=["перечитать Чапаев и Пустота"]))
    linker = Linker(index, writer, model="m", client=FakeAnthropic({"links": []}))
    assert await linker.link(write) == 1  # the name is in the text: no model needed
    assert "[[Чапаев и Пустота]]" in index.read(write.path)
    assert writer.undo(write.undo) is True
    assert index.read(write.path) == before
```

- [ ] **Step 2: Run them to see the first two fail**

Run: `uv run pytest tests/test_vault_filer.py -q -k linker`
Expected: `keeps_a_line_written_while` FAILS ("и Сорокина тоже" gone) and `does_not_bring_back` FAILS (the file exists again); the third passes.

- [ ] **Step 3: The linker**

In `app/vault/linker.py`, add `import asyncio` above `import json`. In `link`, replace everything from `props, body = split(text)` to the end of the method with:

```python
        _, body = split(text)
        pairs = obvious_links(body, self._index, exclude=write.note)
        pairs += await self._model_links(body, write.note, [p for p, _ in pairs])
        if not pairs:
            return 0
        added = 0

        def relink(current: str) -> str:
            # The text as it is now, not as it was before the model was asked: an Undo or the
            # next message may have changed the note in those seconds, and writing the old
            # text back would undo the undo or drop that message's line.
            nonlocal added
            _, now_body = split(current)
            linked = apply_links(now_body, pairs)
            added = linked.count("[[") - now_body.count("[[")
            return current.replace(now_body, linked, 1) if linked != now_body else current

        if not await asyncio.to_thread(self._writer.amend, write.path, relink):
            return 0
        log.info("linker added %d link(s) to %s", added, write.note)
        return added
```

- [ ] **Step 4: The index reads a copy**

In `app/vault/index.py`, give the `notes` property a docstring:

```python
    @property
    def notes(self) -> list[Note]:
        """A copy: writes, undo and the morning digest change the index from worker threads
        while the event loop reads it, and a dict that changes size under a loop raises."""
        return list(self._notes.values())
```

and in the five readers below it (`by_name` twice, `folders`, `tags`, `candidates`) replace `self._notes.values()` with `self.notes`. `refresh` and `note_changed` keep `self._notes`.

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/vault/linker.py app/vault/index.py tests/test_vault_filer.py
git commit -m "fix(vault): the linker amends the note as it is now, never the text it read" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: A model that cannot be asked costs the filing, not the words

**Files:**
- Modify: `app/vault/pipeline.py` (`handle`'s `except FilerError`, new `_kept`)
- Modify: `app/texts.py` (`VAULT_WHAT["kept"]`)
- Test: `tests/test_vault_filer.py` (`test_pipeline_survives_a_model_that_is_down` is replaced)

**Interfaces:**
- Consumes: `reply_line` showing writes plus the error (Task 5).
- Produces: `VaultWrite.kind == "kept"` for an inbox line written because the filer failed; `VaultTurn.error`/`reason` still set (the audit and the hourly health note read them).

- [ ] **Step 1: Replace the pinned test and add the gate test**

In `tests/test_vault_filer.py`, replace `test_pipeline_survives_a_model_that_is_down` (four lines) with:

```python
async def test_a_model_that_is_down_costs_the_filing_not_the_words(index):
    """With Notion off nothing else would have kept the message: it goes to the inbox note as
    it is, and the reply says both that it is saved and why it was not filed."""
    error = anthropic.APIError("down", request=None, body=None)  # type: ignore[arg-type]
    turn = await pipeline(index, error).handle("записаться к зубному")
    assert [w.kind for w in turn.writes] == ["kept"] and turn.error
    assert "- записаться к зубному" in index.read(f"{texts.VAULT_INBOX_NOTE}.md")
    line = turn.reply_line()
    assert line.startswith(texts.VAULT_REPLY.split("{")[0])
    assert texts.VAULT_WHAT["kept"].format(note=texts.VAULT_INBOX_NOTE) in line
    assert len(turn.undos) == 1  # and it can be taken back like any other write


async def test_a_held_back_turn_keeps_nothing_when_the_model_is_down(index):
    """The gate still decides: a turn the orchestrator held back writes nothing at all."""
    error = anthropic.APIError("down", request=None, body=None)  # type: ignore[arg-type]

    async def no() -> bool:
        return False

    turn = await pipeline(index, error).handle("зубы", go=no)
    assert turn.writes == [] and turn.error
    assert index.by_name(texts.VAULT_INBOX_NOTE) is None
    assert texts.VAULT_FAILED.split("{")[0] in turn.reply_line()
```

- [ ] **Step 2: Run them to see the first fail**

Run: `uv run pytest tests/test_vault_filer.py -q -k "model_that_is_down or held_back"`
Expected: the first FAILS (`turn.writes == []`), the second passes.

- [ ] **Step 3: The text**

In `app/texts.py`, in `VAULT_WHAT`, after the `"inbox"` entry add:

```python
    # The model could not be asked at all: the words are kept as they are.
    "kept": "сохранил в «{note}» как есть",
```

- [ ] **Step 4: The pipeline**

In `app/vault/pipeline.py`, `handle`, replace:

```python
        except FilerError as e:
            log.warning("filer failed: %s", e)
            return VaultTurn(error=str(e), reason=e.reason, model=self._filer.model)
```

with:

```python
        except FilerError as e:
            log.warning("filer failed: %s", e)
            return await self._kept(message, e, go)
```

and add, directly above `async def _looked_up(`:

```python
    async def _kept(self, message: str, error: FilerError,
                    go: Callable[[], Awaitable[bool]] | None) -> VaultTurn:
        """The model could not be asked — Claude is down, out of credit, or answered rubbish.
        The words go to the inbox note as they are, which takes no model: with Notion off
        there is no other place that would have kept them. The reply says both things, that
        they are saved and why they are not filed."""
        turn = VaultTurn(error=str(error), reason=error.reason, model=self._filer.model)
        if not message.strip() or (go is not None and not await go()):
            return turn
        try:
            write = await asyncio.to_thread(
                self._writer.run, VaultAction(action="inbox", text=message))
        except (OSError, ValueError) as e:
            log.warning("could not keep the message in the inbox note: %s", e)
            return turn
        turn.writes = [write.model_copy(update={"kind": "kept"})]
        return turn

```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass (the orchestrator tests that assert "Obsidian —" on a down model still match: the line now starts with `VAULT_REPLY`).

- [ ] **Step 6: Commit**

```bash
git add app/vault/pipeline.py app/texts.py tests/test_vault_filer.py
git commit -m "fix(vault): when Claude cannot be asked the words are kept in the inbox note" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: A text too long to read is refused, never cut

**Files:**
- Modify: `app/llm/rewrite.py` (`TooLong`, the cap check, no slicing)
- Modify: `app/llm/edits.py` (same)
- Modify: `app/vault/pipeline.py` (`_new_body`, `_moved`, imports)
- Modify: `app/commands/executor.py` (`_plan`, `_rewrite_whole`, imports)
- Modify: `app/texts.py` (`VAULT_TOO_LONG`, `ERRORS["REWRITE_TOO_LONG"]`)
- Modify: `documentation/ERRORS.md` (one row)
- Test: `tests/test_rewrite.py`, `tests/test_edits.py`, `tests/test_texts.py`

**Interfaces:**
- Produces: `app.llm.rewrite.TooLong(RewriteError)`, `app.llm.edits.TooLong(EditError)`; `Refused("REWRITE_TOO_LONG", target_name=...)` on the Notion side; `texts.VAULT_TOO_LONG` on the vault side.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewrite.py`:

```python


async def test_the_rewriter_refuses_a_text_it_could_only_read_the_top_of():
    """It used to send the first 20 000 characters and hand back a text the caller then wrote
    in place of the whole: everything past the cut was deleted without ever being read."""
    from app.llm.rewrite import MAX_INPUT, TooLong

    client = FakeAnthropic("короче")
    with pytest.raises(TooLong):
        await Rewriter("", "m", client=client).rewrite("я" * (MAX_INPUT + 1), "сократи")
    assert client.seen == []
```

Append to `tests/test_edits.py`:

```python


# ---- a page too long to show the model -------------------------------------------------------

async def test_the_editor_refuses_a_page_it_could_only_read_the_top_of():
    from app.llm.edits import MAX_INPUT, TooLong

    client = FakeAnthropic({"edits": [], "full": "короче"})
    with pytest.raises(TooLong):
        await Editor("", "m", client=client).plan("[1] " + "я" * MAX_INPUT, "сократи")
    assert client.seen == []  # nothing was sent, so nothing can come back to be written


async def test_a_note_too_long_to_edit_is_left_exactly_as_it_is(tmp_path):
    from app.llm.edits import MAX_INPUT
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline, VaultTurn
    from app.vault.writer import VaultAction, VaultWriter

    long_text = "\n".join(f"строка {i} " + "я" * 80 for i in range(MAX_INPUT // 80))
    (tmp_path / "Длинная.md").write_text(long_text, encoding="utf-8", newline="\n")
    index = VaultIndex(tmp_path)
    index.refresh()
    client = FakeAnthropic({"edits": [], "full": "короче"})
    pipe = VaultPipeline(index, VaultWriter(index), filer=None,  # type: ignore[arg-type]
                         editor=Editor("", "m", client=client))
    turn = VaultTurn()

    action = await pipe._rewritten(VaultAction(action="rewrite", note="Длинная",
                                               text="сократи"), turn)

    assert action is None and turn.error == texts.VAULT_TOO_LONG
    assert client.seen == []
    assert (tmp_path / "Длинная.md").read_text(encoding="utf-8") == long_text
```

In `tests/test_texts.py`, in `ERROR_PLACEHOLDERS`, change the line `"REWRITE_FAILED": {"error"},` to:

```python
    "REWRITE_FAILED": {"error"}, "REWRITE_TOO_LONG": {"target_name"},
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_rewrite.py tests/test_edits.py tests/test_texts.py -q`
Expected: the three new tests FAIL (`ImportError: TooLong`); `test_error_templates_ask_only_for_what_their_callers_can_supply` FAILS (`REWRITE_TOO_LONG` not in `texts.ERRORS`).

- [ ] **Step 3: The two model wrappers**

In `app/llm/rewrite.py`, above `def unfence(` add:

```python
class TooLong(RewriteError):
    """The text is longer than one call may read. Sending the top of it and writing the answer
    back as the whole would delete the rest unread, so nothing is sent at all."""


```

In `Rewriter.rewrite`, after the `nothing to rewrite` check add:

```python
        if len(current) > MAX_INPUT:
            raise TooLong(f"{len(current)} characters, the limit is {MAX_INPUT}")
```

and change `rewrite_message(current[:MAX_INPUT], instruction)` to `rewrite_message(current, instruction)`.

In `app/llm/edits.py`, above `class NothingToChange(EditError):` add:

```python
class TooLong(EditError):
    """The page is longer than one call may read. The model would be shown the top of it, and
    a whole new text written from that would delete the rest unread."""


```

In `Editor.plan`, after the `nothing to edit` check add:

```python
        if len(page) > MAX_INPUT:
            raise TooLong(f"{len(page)} characters, the limit is {MAX_INPUT}")
```

and change `edit_message(page[:MAX_INPUT], instruction)` to `edit_message(page, instruction)`.

- [ ] **Step 4: The texts**

In `app/texts.py`, after the `VAULT_SOME_FAILED` line (Task 5) add:

```python
# A note (or the section named) is longer than the editing model may read in one go.
VAULT_TOO_LONG = "заметка слишком длинная, чтобы менять её целиком — назовите раздел"
```

and in `ERRORS`, after the `"REWRITE_FAILED"` entry add:

```python
    "REWRITE_TOO_LONG": "«{target_name}» слишком длинная, чтобы менять её за один раз. "
                        "Ничего не изменил.",
```

- [ ] **Step 5: The vault pipeline**

In `app/vault/pipeline.py`, change the two imports to:

```python
from app.llm.edits import EditError, Editor, NothingToChange
from app.llm.edits import TooLong as EditTooLong
```

```python
from app.llm.rewrite import RewriteError, Rewriter
from app.llm.rewrite import TooLong as RewriteTooLong
```

In `_new_body`, the editor branch — after the `except NothingToChange:` block and before `except EditError as e:` — insert:

```python
            except EditTooLong:
                turn.error = turn.error or texts.VAULT_TOO_LONG
                return None
```

and in the rewriter branch, before `except RewriteError as e:` insert:

```python
        except RewriteTooLong:
            turn.error = turn.error or texts.VAULT_TOO_LONG
            return None
```

In `_moved`, after the `except NothingToChange:` block and before `except EditError as e:` insert:

```python
        except EditTooLong:
            turn.error = turn.error or texts.VAULT_TOO_LONG
            return []
```

- [ ] **Step 6: The Notion executor**

In `app/commands/executor.py`, change the imports to:

```python
from app.llm.edits import Edit, EditError, Editor, EditPlan, NothingToChange
from app.llm.edits import TooLong as EditTooLong
from app.llm.edits import check as check_edits
from app.llm.rewrite import RewriteError, Rewriter
from app.llm.rewrite import TooLong as RewriteTooLong
```

In `_plan`, before `except EditError as e:` insert:

```python
        except EditTooLong:
            raise Refused("REWRITE_TOO_LONG", target_name=cmd.page_title) from None
```

In `_rewrite_whole`, before `except RewriteError as e:` insert:

```python
        except RewriteTooLong:
            raise Refused("REWRITE_TOO_LONG", target_name=cmd.page_title) from None
```

- [ ] **Step 7: The error table**

In `documentation/ERRORS.md`, after the `REWRITE_FAILED` row add:

```markdown
| `REWRITE_TOO_LONG` | llm/edits, llm/rewrite | the page (or the vault note) holds more than `MAX_INPUT` characters; it used to be cut and the answer written back over the whole | «…» слишком длинная, чтобы менять её за один раз | nothing written; name a section instead |
```

- [ ] **Step 8: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add app/llm/rewrite.py app/llm/edits.py app/vault/pipeline.py app/commands/executor.py app/texts.py documentation/ERRORS.md tests/test_rewrite.py tests/test_edits.py tests/test_texts.py
git commit -m "fix: a text longer than one rewrite may read is refused instead of cut" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Telegram messages that fit, and a press that is never lost

**Files:**
- Create: `app/telegram/sending.py`
- Modify: `app/telegram/handlers.py` (`_send`, `_on_callback`, import)
- Modify: `app/main.py` (both digest senders, import)
- Test: `tests/test_handlers.py`

**Interfaces:**
- Produces: `pieces(text: str, limit: int = 4096) -> list[str]`; `async send_text(bot, chat_id: int, text: str, reply_markup=None)` → the last message sent. Called as `bot.send_message(chat_id, part, **extra)` — positional, so the digest tests' `send_message(chat_id, body)` fakes keep working.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_handlers.py`:

```python


# ---- a reply longer than one Telegram message ---------------------------------------------------


def test_pieces_cut_at_line_ends_and_only_hard_when_they_must():
    from app.telegram.sending import pieces

    assert pieces("") == [""]
    assert pieces("a" * 4096) == ["a" * 4096]
    lines = "\n".join(f"строка {i}" for i in range(600))
    parts = pieces(lines)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert "\n".join(parts) == lines  # nothing lost, nothing doubled
    assert all(not p.startswith("\n") and not p.endswith("\n") for p in parts)
    hard = pieces("x" * 5000)
    assert [len(p) for p in hard] == [4096, 904]


async def test_a_long_reply_arrives_in_several_messages_with_the_buttons_under_the_last():
    long_text = "\n".join(f"• пункт {i}" for i in range(700))
    hs = build(reply=Reply(long_text, buttons=[[Button("u:7", "Undo")]], undo_id=7))
    update = text_update(ALLOWED_USER, "что у меня по дому", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert len(hs.bot.sent) > 1
    assert "".join(m["text"] for m in hs.bot.sent).replace("\n", "") == long_text.replace("\n", "")
    assert [m["reply_markup"] is not None for m in hs.bot.sent][-1] is True
    assert all(m["reply_markup"] is None for m in hs.bot.sent[:-1])
    assert hs.store.recorded == [(7, hs.bot.sent[-1]["message_id"])]


async def test_a_press_telegram_will_no_longer_acknowledge_is_still_handled():
    """A button pressed while a long turn ran reaches the handler late, and Telegram refuses
    to acknowledge it by then. The refusal used to be raised from here: the press was lost
    and the chat got "could not handle the message"."""
    from telegram.error import BadRequest

    class StaleBot(FakeBot):
        async def answer_callback_query(self, callback_query_id, text=None, **kwargs):
            raise BadRequest("Query is too old and response timeout expired")

    hs = build(reply=Reply("undone"))
    hs.bot = StaleBot()
    hs.context.bot = hs.bot
    update = callback_update(ALLOWED_USER, "u:7", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == [("handle_callback", CHAT_ID, ALLOWED_USER, "u:7")]
    assert [m["text"] for m in hs.bot.sent] == ["undone"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_handlers.py -q -k "pieces or long_reply or no_longer_acknowledge"`
Expected: 3 FAIL (`ModuleNotFoundError`, one message sent, `BadRequest` raised).

- [ ] **Step 3: The sending module**

Create `app/telegram/sending.py`:

```python
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
```

- [ ] **Step 4: The handlers**

In `app/telegram/handlers.py`, add `from app.telegram.sending import send_text` after the `keyboards` import. In `_send`, replace:

```python
    chat_id = update.effective_chat.id
    sent = await context.bot.send_message(
        chat_id=chat_id, text=reply.text, reply_markup=to_markup(reply)
    )
```

with:

```python
    chat_id = update.effective_chat.id
    sent = await send_text(context.bot, chat_id, reply.text, to_markup(reply))
```

In `_on_callback`, replace:

```python
    query = update.callback_query
    await query.answer()  # Telegram's 10-second budget; never carries text
```

with:

```python
    query = update.callback_query
    # Only stops the button's spinner. Telegram refuses it for a press it considers too old —
    # one that waited behind a long turn — and that refusal used to be raised from here, before
    # the orchestrator was ever called: the press was lost and the user was told the bot broke.
    with contextlib.suppress(TelegramError):
        await query.answer()
```

- [ ] **Step 5: The digests**

In `app/main.py`, add `from app.telegram.sending import send_text` after `from app.telegram.handlers import register`. In `_daily_digest`'s `send`, change `await application().bot.send_message(chat_id, text)` to `await send_text(application().bot, chat_id, text)`. In `_mail_digest`'s `to_everyone`, change `await application().bot.send_message(chat_id, body)` to `await send_text(application().bot, chat_id, body)`.

- [ ] **Step 6: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/telegram/sending.py app/telegram/handlers.py app/main.py tests/test_handlers.py
git commit -m "fix(telegram): long replies and digests are split; a late button press is still handled" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Mail past the cap waits instead of vanishing

**Files:**
- Modify: `app/mail/imap.py` (`fetch_since`, `_one`'s log line)
- Modify: `app/mail/local.py` (`_ask`)
- Modify: `documentation/GMAIL_PLAN.md` (the `GMAIL_MAX_PER_RUN` bullet)
- Test: `tests/test_mail.py`, `tests/test_mail_local.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mail.py`:

```python


def test_unread_mail_past_the_cap_waits_for_the_next_run_instead_of_vanishing(monkeypatch):
    """Forty-one unread letters and a cap of forty: the digest took the newest forty and moved
    the bookmark past all of them, so the one left over was never in any digest."""
    from app.mail.imap import GmailIMAP

    box = FakeBox({u: (raw(f"S{u} <s@x.ee>", f"letter {u}", "t"), False)
                   for u in range(10, 15)})
    imap = GmailIMAP("me@x.ee", "pw")
    monkeypatch.setattr(imap, "_open", lambda: box)

    messages, newest, _ = imap.fetch_since(None, limit=3)
    assert [m.subject for m in messages] == ["letter 10", "letter 11", "letter 12"]
    assert newest == "12"

    messages, newest, _ = imap.fetch_since(newest, limit=3)
    assert [m.subject for m in messages] == ["letter 13", "letter 14"]
    assert newest == "14"
```

Append to `tests/test_mail_local.py`:

```python


async def test_an_answer_that_is_not_json_is_a_classifier_error_not_a_crash():
    """A proxy's HTML error page in place of Ollama's JSON raised out of the run that is
    documented never to raise."""
    from app.mail.classify import ClassifyError
    from app.mail.local import LocalClassifier
    from tests.test_mail import message

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>bad gateway</html>")

    classifier = LocalClassifier("http://x", "m", BUCKETS,
                                 transport=httpx.MockTransport(handler))
    with pytest.raises(ClassifyError):
        await classifier._ask([message("1")])
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_mail.py tests/test_mail_local.py -q -k "past_the_cap or not_json"`
Expected: the first FAILS (`["letter 12", "letter 13", "letter 14"]`, newest `"14"`), the second FAILS (`JSONDecodeError` is raised, not `ClassifyError`).

- [ ] **Step 3: The fetch**

In `app/mail/imap.py`, `fetch_since`, replace:

```python
                unread = set(self._search(box, [*criteria, "UNSEEN"])) if uids else set()
                uids = [u for u in uids if u in unread]
                messages = [self._one(box, uid) for uid in uids[-limit:]]
                return [m for m in messages if m is not None], newest, validity
```

with:

```python
                unread = set(self._search(box, [*criteria, "UNSEEN"])) if uids else set()
                uids = [u for u in uids if u in unread]
                if len(uids) > limit:
                    # More than one run holds: the oldest go now and the bookmark stops at the
                    # last of them, so the rest are the next run's. Taking the newest and
                    # moving the bookmark past the whole lot left the others out of every
                    # digest, with nothing to say so.
                    log.info("mail: %d unread, %d taken now, the rest next run",
                             len(uids), limit)
                    uids, newest = uids[:limit], uids[limit - 1]
                messages = [self._one(box, uid) for uid in uids]
                return [m for m in messages if m is not None], newest, validity
```

In `_one`, change the log line `log.info("could not fetch one message (uid kept for the next run)")` to:

```python
            log.warning("could not fetch one message; it is left out of this digest")
```

- [ ] **Step 4: The local classifier**

In `app/mail/local.py`, `_ask`, replace `data = resp.json()` with:

```python
        try:
            data = resp.json()
        except ValueError:  # a proxy's error page, a half-written reply
            raise ClassifyError("ollama answered something that is not JSON") from None
```

- [ ] **Step 5: The document**

In `documentation/GMAIL_PLAN.md`, replace the bullet starting `- A run handles at most \`GMAIL_MAX_PER_RUN\` messages (default 40)` with:

```markdown
- A run handles at most `MAIL_MAX_PER_RUN` messages (default 40) so a backlog cannot turn into
  a huge model call or a wall of text. Past the cap the *oldest* go now and the bookmark stops
  at the last of them: the rest wait for the next run rather than being skipped for good.
```

- [ ] **Step 6: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/mail/imap.py app/mail/local.py documentation/GMAIL_PLAN.md tests/test_mail.py tests/test_mail_local.py
git commit -m "fix(mail): mail past the cap waits for the next run; a non-JSON answer is a classifier error" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: The morning message remembers it went out, and follows a moved time

**Files:**
- Modify: `app/daily.py` (`LastRun`, `RECHECK_S`, `DailyMessage.__init__/_loop/_wait/_fire`)
- Modify: `app/main.py` (both `DailyMessage(...)` constructions)
- Test: `tests/test_vault_agenda.py`

**Interfaces:**
- Produces: `app.daily.LastRun(path: Path, key: str)` with `get() -> datetime | None` and `set(when: datetime)`; `DailyMessage(..., remember: LastRun | None = None)`. The file is `data/daily_state.json` (next to `bot.sqlite`), keys `"agenda"` and `"mail"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_vault_agenda.py`, change the import `from app.daily import DailyMessage, parse_at` to `from app.daily import DailyMessage, parse_at, parse_times`, then append:

```python


async def test_a_restart_inside_the_grace_does_not_send_the_same_morning_twice(tmp_path):
    """Updating the bot at 09:30 sent the agenda again: nothing remembered that 09:00 had
    already gone out."""
    from zoneinfo import ZoneInfo

    from app.daily import LastRun

    tallinn = ZoneInfo("Europe/Tallinn")
    sent: list[int] = []
    memory = LastRun(tmp_path / "daily_state.json", "agenda")
    first = DailyMessage(lambda: _record(sent), time(9, 0), "Europe/Tallinn",
                         now=lambda: datetime(2026, 9, 25, 9, 5, tzinfo=tallinn),
                         remember=memory)
    first.start()
    await asyncio.sleep(0.05)
    await first.stop()
    assert sent == [1]

    restarted = DailyMessage(lambda: _record(sent), time(9, 0), "Europe/Tallinn",
                             now=lambda: datetime(2026, 9, 25, 9, 30, tzinfo=tallinn),
                             remember=memory)
    restarted.start()
    await asyncio.sleep(0.05)
    await restarted.stop()
    assert sent == [1]  # 09:00 went out before the restart

    next_day = DailyMessage(lambda: _record(sent), time(9, 0), "Europe/Tallinn",
                            now=lambda: datetime(2026, 9, 26, 9, 10, tzinfo=tallinn),
                            remember=memory)
    next_day.start()
    await asyncio.sleep(0.05)
    await next_day.stop()
    assert sent == [1, 1]  # a new day is a new message


async def test_a_time_moved_earlier_on_the_admin_page_is_honoured_before_the_old_one():
    from zoneinfo import ZoneInfo

    tallinn = ZoneInfo("Europe/Tallinn")
    clock = [datetime(2026, 9, 25, 8, 0, tzinfo=tallinn)]
    times = [parse_times("12:00")]
    fired_at: list[datetime] = []
    parked = asyncio.Event()

    async def send() -> None:
        fired_at.append(clock[0])

    async def sleep(seconds: float) -> None:
        if fired_at:
            parked.set()
            await asyncio.Event().wait()
        clock[0] += timedelta(seconds=seconds)
        if clock[0] >= datetime(2026, 9, 25, 8, 30, tzinfo=tallinn):
            times[0] = parse_times("09:00")  # changed while the bot waits for 12:00

    daily = DailyMessage(send, lambda: times[0], "Europe/Tallinn", now=lambda: clock[0],
                         sleep=sleep)
    daily.start()
    await asyncio.wait_for(parked.wait(), 5)
    await daily.stop()
    assert fired_at and fired_at[0].hour == 9 and fired_at[0].minute == 0


async def test_a_broken_memory_file_does_not_stop_the_morning_message(tmp_path):
    from zoneinfo import ZoneInfo

    from app.daily import LastRun

    path = tmp_path / "daily_state.json"
    path.write_text("{not json", encoding="utf-8")
    tallinn = ZoneInfo("Europe/Tallinn")
    sent: list[int] = []
    daily = DailyMessage(lambda: _record(sent), time(9, 0), "Europe/Tallinn",
                         now=lambda: datetime(2026, 9, 25, 9, 5, tzinfo=tallinn),
                         remember=LastRun(path, "agenda"))
    daily.start()
    await asyncio.sleep(0.05)
    await daily.stop()
    assert sent == [1]
    assert LastRun(path, "agenda").get() == datetime(2026, 9, 25, 9, 0, tzinfo=tallinn)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_vault_agenda.py -q`
Expected: the three new tests FAIL (`ImportError: LastRun`; the moved-time test fires at 12:00).

- [ ] **Step 3: `daily.py`**

Add `import json` after `import asyncio` and `from pathlib import Path` after the `datetime` import. After `MIN_SLEEP_S = 0.05` add:

```python
# How long a wait goes before the schedule is read again: a time moved on the admin page is
# honoured within this, instead of after the old time has fired.
RECHECK_S = 60.0


class LastRun:
    """When a message last went out, kept in a small JSON file under one key per message, so
    a restart inside the grace period does not send the morning agenda a second time."""

    def __init__(self, path: Path, key: str) -> None:
        self._path = path
        self._key = key

    def _all(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self) -> datetime | None:
        value = self._all().get(self._key)
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def set(self, when: datetime) -> None:
        data = {**self._all(), self._key: when.isoformat()}
        try:
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError as e:  # a digest that cannot be remembered is still sent
            log.warning("could not remember the last %s run: %s", self._key, e)
```

In `DailyMessage.__init__`, add the keyword `remember: LastRun | None = None` after `sleep=...` and `self._remember = remember` after `self._sleep = sleep`.

Replace `_loop`, `_until` and `_fire` with:

```python
    async def _loop(self) -> None:
        # A bot started shortly after the time still sends today's (GRACE), so a restart at
        # 09:05 does not silently skip the day.
        now = self._now()
        # The most recent time that has already passed today (or yesterday's last one).
        passed = [now.astimezone(self._zone).replace(hour=t.hour, minute=t.minute, second=0,
                                                      microsecond=0) for t in self._times]
        previous = max([p for p in passed if p <= now], default=min(passed) - timedelta(days=1))
        if now - previous < GRACE and not self._sent_already(previous):
            await self._fire(previous)
        while True:
            await self._fire(await self._wait())

    def _sent_already(self, due: datetime) -> bool:
        """Did this run go out before the restart? Without the memory a bot updated at 09:30
        sent the morning agenda again."""
        last = self._remember.get() if self._remember is not None else None
        return last is not None and last >= due

    async def _wait(self) -> datetime:
        """Sleep until the next due time, and return it.

        The schedule is read again every RECHECK_S, from the moment the wait began: a time
        moved on the admin page is honoured then, not after the old one has fired. And the
        wall clock has to have really reached the time: asyncio's timers run on the monotonic
        clock and may fire up to a clock tick early — about 16 ms on Windows. Waking at
        08:59:59.985 and then asking for the next run gave 09:00 *today* again, and the
        morning digest went out twice, a second apart."""
        start = self._now()
        while True:
            due = self.next_run(start)
            left = (due - self._now()).total_seconds()
            if left <= 0:
                if self._now() - due < GRACE:
                    return due
                start = self._now()  # moved to a time long past: that one is not sent
                continue
            await self._sleep(min(max(left, MIN_SLEEP_S), RECHECK_S))

    async def _fire(self, due: datetime) -> None:
        try:
            await self._send()
        except Exception:  # a failed digest must never stop the schedule
            log.exception("daily message failed")
            return
        if self._remember is not None:
            self._remember.set(due)
```

- [ ] **Step 4: `main.py`**

Change the import to `from app.daily import DailyMessage, LastRun, parse_times`. In `_daily_digest`, replace the `return DailyMessage(...)` with:

```python
    return DailyMessage(send, lambda: parse_times(tuning.agenda_at), settings.timezone,
                        remember=LastRun(settings.db_path.with_name("daily_state.json"),
                                         "agenda"))
```

In `_mail_digest`, replace `return service, DailyMessage(send, lambda: parse_times(tuning.mail_at), settings.timezone)` with:

```python
    return service, DailyMessage(send, lambda: parse_times(tuning.mail_at), settings.timezone,
                                 remember=LastRun(settings.db_path.with_name("daily_state.json"),
                                                  "mail"))
```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass, `test_a_timer_that_wakes_a_little_early_still_sends_once` included (the 60-second chunks and the early wake are both handled by `_wait`).

- [ ] **Step 6: Commit**

```bash
git add app/daily.py app/main.py tests/test_vault_agenda.py
git commit -m "fix(daily): a restart does not resend the agenda; a moved time is honoured at once" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Notion undo never claims a block it did not add

**Files:**
- Modify: `app/commands/executor.py` (`_match_list`, `_append`, `_apply`)
- Modify: `tests/fakes.py` (`block_children` honours `limit`)
- Test: `tests/test_edits.py`

- [ ] **Step 1: Make the fake read the way Notion is read**

In `tests/fakes.py`, `FakeNotionProvider.block_children`, replace `return list(self.page_blocks.get(block_id, []))` with:

```python
        # As DirectNotionProvider reads: whole pages of 100 until `limit` is reached.
        return list(self.page_blocks.get(block_id, []))[:-(-limit // 100) * 100]
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_edits.py`:

```python


# ---- a page longer than one read ------------------------------------------------------------

async def test_undo_never_claims_a_block_the_page_read_did_not_cover():
    """The page is read up to a cap, and Notion answers an insert with every sibling after
    it. Ids past the cap were unknown to the executor, so they looked freshly created — and
    Undo of one added line deleted the user's own blocks from there to the end of the page."""
    from app.commands.models import AppendBlocks

    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = [
        _text("bulleted_list_item", "анкеры", "b-1"),
        _text("bulleted_list_item", "маты", "b-2"),
        _text("heading_2", "Заметки", "h-2"),
        *[_text("paragraph", f"абзац {i}", f"p-{i}") for i in range(400)],
    ]
    executor = Executor(provider)
    result = await executor.run(AppendBlocks(
        page_id=PAGE, target_name="Идеи", page_title="Шведская стенка",
        paragraphs=["шурупы"], markdown=True, request="добавь шурупы"))

    [call] = [c for c in provider.calls if c[0] == "append_blocks"]
    assert call[3] == "b-2"  # inserted into the list, so the siblings were reported
    assert result.undo is not None and len(result.undo.block_ids) == 1
    assert not any(b.startswith("p-") for b in result.undo.block_ids)


async def test_two_lines_inserted_at_one_place_keep_the_order_they_were_written_in():
    provider = FakeNotionProvider()
    provider.page_blocks[PAGE] = _page()
    await _executor(provider, _edits(
        {"op": "insert", "at": 2, "text": "первая вставка"},
        {"op": "insert", "at": 2, "text": "вторая вставка"},
    )).run(_cmd())

    page = [str(b) for b in provider.page_blocks[PAGE]]
    first = next(i for i, b in enumerate(page) if "первая вставка" in b)
    second = next(i for i, b in enumerate(page) if "вторая вставка" in b)
    assert second == first + 1
```

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/test_edits.py -q -k "did_not_cover or keep_the_order"`
Expected: both FAIL (the undo record holds 300+ ids; the inserts come out reversed).

- [ ] **Step 4: The executor**

In `_match_list`, change `children = await self._p.block_children(page_id)` to:

```python
            children = await self._p.block_children(page_id, limit=MAX_PAGE_BLOCKS)
```

In `_append`, replace the last three sentences of the docstring (from `delete lines nobody added.` to the closing `"""`) with:

```python
        delete lines nobody added. The new blocks come first, so only the first `len(batch)`
        results can be ours; `known`, the ids the caller read from the page, is taken out of
        those as a second check. It used to be the only check, and a page read is capped:
        past the cap every following sibling looked new, and Undo deleted the user's own
        blocks from there to the end of the page."""
```

and replace:

```python
                seen = known | set(ids)
                fresh = ([b for b in got if b not in seen] if known
                         else got[:len(batch)])
```

with:

```python
                seen = known | set(ids)
                fresh = [b for b in got[:len(batch)] if b not in seen]
```

In `_apply`, replace:

```python
        # Bottom-up, and in reverse for several insertions after one line, so they land in the
        # order the model wrote them.
        for edit in sorted([e for e in edits if e.op in ("insert", "image")],
                           key=lambda e: e.at, reverse=True):
```

with:

```python
        # Bottom-up, and in reverse for several insertions after one line, so they land in the
        # order the model wrote them: each goes right after the same block, so the last one
        # written has to go in first. (`sorted` is stable, reversed or not — the list itself
        # has to be turned round.)
        for edit in sorted(reversed([e for e in edits if e.op in ("insert", "image")]),
                           key=lambda e: e.at, reverse=True):
```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass (`test_undoing_an_added_line_does_not_delete_the_lines_after_it` still does).

- [ ] **Step 6: Commit**

```bash
git add app/commands/executor.py tests/fakes.py tests/test_edits.py
git commit -m "fix(notion): undo of an added line never claims blocks past the page read" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: A list field answered by a button

**Files:**
- Modify: `app/conversation/resolver.py` (`_rebuild_value`)
- Test: `tests/test_resolver.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_resolver.py`:

```python


def test_a_list_field_answered_by_a_button_survives_the_rebuild():
    """A required multi-select answered by pressing one option: the session stores that one
    option, and rebuilding the field used to iterate over it as if it were a list — a
    TypeError on every press until the question expired."""
    snap = sample_snapshot()
    todo = next(t for t in snap.targets if t.id == "ds-todo")
    todo.fields[:] = [replace(f, required=True) if f.id == "tags" else f for f in todo.fields]
    ctx = ContextBuilder("Europe/Tallinn").build(snap, now=SAMPLE_NOW)
    tk = ctx.target_key("ds-todo")
    interp = make_interp("create", cand(ctx, tk, 0.95, fields={
        f"{tk}.f1": val("Документы"), ctx.field_key("ds-todo", "prio"): val(
            ctx.option_key("ds-todo", "prio", "o-A"))}))
    result = SemanticValidator().validate(interp, ctx, snap)
    decision = Policy(T).evaluate(result)
    q = decision.questions[0]
    assert q.type == "field_required" and q.field_name == "Теги"
    options = options_for(q, decision.candidate, result, ctx)
    session = session_from_decision(chat_id=42, event_id=1, text="msg", result=result,
                                    decision=decision, options=options, now=NOW, ttl_s=600,
                                    asked=[])

    answered, verb = apply_answer(session, options[0].id)
    assert verb is None

    rebuilt = result_from_session(answered, snap, ctx)
    tags = next(f for f in rebuilt.candidates[0].fields.values() if f.field.id == "tags")
    assert tags.status == "value" and [o.name for o in tags.value] == ["дом"]
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_resolver.py -q -k list_field`
Expected: FAIL with `TypeError: string indices must be integers`.

- [ ] **Step 3: The fix**

In `app/conversation/resolver.py`, `_rebuild_value`, replace:

```python
    if ftype in _LIST_OPTION_TYPES:
        return [_rebuild_option(ctx, target_id, field_id, x) for x in raw]
```

with:

```python
    if ftype in _LIST_OPTION_TYPES:
        # A button answer stores the one option that was pressed, not a list of one.
        chosen = raw if isinstance(raw, list) else [raw]
        return [_rebuild_option(ctx, target_id, field_id, x) for x in chosen]
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/conversation/resolver.py tests/test_resolver.py
git commit -m "fix: a required multi-select answered by a button no longer crashes the rebuild" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: The staged reader asks the target alongside the intent, and logs its timing

**Files:**
- Modify: `app/vault/staged.py` (imports, `StagedFiler.file`, `_Run.__init__/_ask/read`, new `_intent`/`_target`, `_add(text, key)`)
- Test: `tests/test_vault_staged.py`

**Interfaces:**
- Produces: `_Run.took: list[float]`; log line `staged <model>: <trail> | N call(s), a.a + b.b + c.c = t.ts`. `_Run._add(text: str, key: str)` takes the target key; `_Run._target(text) -> str` asks for it.

- [ ] **Step 1: Update the harness and the pins, add the tests**

In `tests/test_vault_staged.py`, add `import asyncio` above `import pytest`. In `Script.__call__`, after `self.asked.append((name, schema, content))` insert:

```python
        if name == "TARGET_PROMPT" and name not in self.answers:
            # Asked alongside the intent and thrown away for anything but an "add".
            await asyncio.sleep(0)
            raise KeyError(name)
```

After the `stages` property add:

```python

    @property
    def details(self) -> list[str]:
        """The stages after intent and target, which are always asked (together)."""
        return [s for s in self.stages if s not in ("INTENT_PROMPT", "TARGET_PROMPT")]
```

Change the three pins `assert script.stages == ["INTENT_PROMPT"]` (in `test_nonsense_is_kept_in_the_inbox_without_another_question`, `test_a_note_is_offered_only_when_the_guide_links_it_or_the_message_names_it` — the `and script.stages == ["INTENT_PROMPT"]` clause — and the link-answer test near the end) to `script.details == []`.

Append:

```python


async def test_the_target_is_asked_alongside_the_intent_and_dropped_for_other_intents():
    """Two of the three calls of an ordinary message run at the same time, so the answer
    comes one call sooner. A message that is not an "add" has still asked, and the answer
    is thrown away: no details stage runs on it."""
    order: list[str] = []

    class Slow(Script):
        async def __call__(self, system, schema, content, max_tokens=0):
            name = "intent" if system == P.INTENT_PROMPT else "other"
            order.append(f"{name} asked")
            if name == "intent":
                await asyncio.sleep(0.01)  # the target question is in flight meanwhile
            order.append(f"{name} answered")
            return await super().__call__(system, schema, content, max_tokens)

    script = Slow(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                  GROCERY_PROMPT={"names": ["молоко"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("купи молоко", ctx())
    assert [a["action"] for a in actions] == ["grocery"]
    assert order.index("other asked") < order.index("intent answered")
    assert script.details == ["GROCERY_PROMPT"]

    script = Script(INTENT_PROMPT={"intent": "done"}, TARGET_PROMPT={"target": "t"},
                    DONE_PROMPT={"target": "o1"})
    actions, _, _ = await StagedFiler(script, "haiku").file("забрал посылку", ctx())
    assert actions[0]["action"] == "update" and script.details == ["DONE_PROMPT"]


async def test_a_target_call_that_failed_is_asked_again_rather_than_lost():
    calls = {"n": 0}

    class Flaky(Script):
        async def __call__(self, system, schema, content, max_tokens=0):
            if system == P.TARGET_PROMPT and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("connection reset")
            return await super().__call__(system, schema, content, max_tokens)

    script = Flaky(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                   GROCERY_PROMPT={"names": ["молоко"]})
    actions, _, _ = await StagedFiler(script, "haiku").file("купи молоко", ctx())
    assert [a["action"] for a in actions] == ["grocery"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_vault_staged.py -q`
Expected: the two new tests FAIL (the target is asked only after the intent answered; the flaky target call raises out of `file`).

- [ ] **Step 3: `staged.py`**

Change the imports at the top to:

```python
import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
```

In `StagedFiler.file`, replace the body with:

```python
        run = _Run(self._ask_model, message, ctx, lookup=self._lookup)
        started = monotonic()
        actions = await run.read()
        # Each call's seconds, in the order they finished: what a slow turn was spent on.
        log.info("staged %s: %s | %d call(s), %s = %.1fs", self.model,
                 " > ".join(run.trail) or "-", run.calls,
                 " + ".join(f"{t:.1f}" for t in run.took) or "0",
                 monotonic() - started)
        return actions, run.prompt_tokens, run.output_tokens
```

In `_Run.__init__`, after `self.calls = 0` add `self.took: list[float] = []  # seconds per call, for the log`.

In `_Run._ask`, replace:

```python
        data, prompt_tokens, output_tokens = await (model or self._ask_model)(
            system, schema, content, max_tokens)
        self.prompt_tokens += prompt_tokens
```

with:

```python
        started = monotonic()
        data, prompt_tokens, output_tokens = await (model or self._ask_model)(
            system, schema, content, max_tokens)
        self.took.append(monotonic() - started)
        self.prompt_tokens += prompt_tokens
```

Replace `read` with:

```python
    async def read(self) -> list[dict]:
        # The target question does not depend on the intent, and nearly every message adds
        # something: asked together with the intent, the answer is ready one call sooner —
        # a second or two of every turn. For the other intents that answer is thrown away:
        # one small call, which is what a second of waiting on every message is worth.
        intent_task = asyncio.create_task(self._intent())
        target_task = asyncio.create_task(self._target(self.message))
        try:
            intent = await intent_task
        except BaseException:
            target_task.cancel()
            raise
        self.trail.append(intent or "?")
        if intent == "add" or intent not in INTENTS:
            try:
                key = await target_task
            except Exception as e:  # the intent is known; a lost target call is one retry
                log.info("target call failed (%s); asking again", type(e).__name__)
                key = await self._target(self.message)
            return await self._add(self.message, key) or [self._inbox(self.message)]
        target_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await target_task
        if intent == "unclear":
            return [self._inbox(self.message)]
        handler = {"done": self._done, "change": self._change, "move": self._move,
                   "ask": self._question}[intent]
        actions = await handler(self.message)
        return actions or [self._inbox(self.message)]

    async def _intent(self) -> str:
        answer = await self._ask(P.INTENT_PROMPT, _obj({"intent": _enum(INTENTS)}),
                                 P.message(text=self.message))
        return str(answer.get("intent", ""))
```

Replace the head of `_add` (from `async def _add(self, text: str)` through `self.trail.append(key or "?")`) with:

```python
    async def _target(self, text: str) -> str:
        """One target for the whole of `text`.

        One, always. Three ways of letting a message go to more than one place were tried —
        a target per item, a "several" key here, a leftover handed back by the details
        stage — and each was used on plain messages far more often than on mixed ones: a
        list of chores scattered over three places, a task returned whole as its own
        leftover. A message that really is two notes for two places lands in the first."""
        places = self._places()
        answer = await self._ask(
            P.TARGET_PROMPT, _obj({"target": _enum(places)}),
            P.message(self._guide(), P.section(P.H_PLACES, P.keyed(places)), text=text))
        return str(answer.get("target", ""))

    async def _add(self, text: str, key: str) -> list[dict]:
        """The details of the place `key`, the target stage's answer."""
        self.trail.append(key or "?")
```

(the `if key == TASKS:` chain below it is unchanged).

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass — the existing `stages == ["INTENT_PROMPT", "TARGET_PROMPT", "FOLDER_PROMPT"]` pins hold because the two tasks start in that order, and the `prompt_tokens == 300` pins hold because a used target call is counted exactly once. `tools/benchmark_filer.py` needs no change: it goes through `StagedFiler.file`.

- [ ] **Step 5: Commit**

```bash
git add app/vault/staged.py tests/test_vault_staged.py
git commit -m "perf(vault): the staged reader asks the target alongside the intent and logs each call's time" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: The admin page's POST is the admin page's

**Files:**
- Modify: `app/admin/server.py` (`_own_page`, `do_POST`)
- Modify: `documentation/ARCHITECTURE.md` §12 (one paragraph)
- Test: `tests/test_admin.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_admin.py`:

```python


def _post_as(server, headers: dict, payload: dict):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.request("POST", "/api/descriptions", body=json.dumps(payload).encode(),
                     headers={"Host": "127.0.0.1", **headers})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def test_a_post_from_another_site_is_refused(server, descriptions):
    """Loopback keeps other machines out, not other *pages*: a site open in the user's own
    browser could POST here and flip the switches or rewrite the research instructions. The
    page sends JSON, which a foreign origin cannot send without a preflight this server
    does not answer; anything else, or a foreign Origin, is refused before it is read."""
    payload = {"targets": {}}
    assert _post_as(server, {"Content-Type": "text/plain"}, payload)[0] == 403
    assert _post_as(server, {"Content-Type": "application/json",
                             "Origin": "https://evil.example"}, payload)[0] == 403
    assert _post_as(server, {"Content-Type": "application/json",
                             "Origin": f"http://127.0.0.1:{server.port}"}, payload)[0] == 200
    assert _post_as(server, {"Content-Type": "application/json; charset=utf-8"},
                    payload)[0] == 200
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_admin.py -q -k another_site`
Expected: FAIL — the `text/plain` POST is answered 200.

- [ ] **Step 3: The guard**

In `app/admin/server.py`, above `def _targets_payload(` add:

```python
def _own_page(headers) -> bool:
    """Is this POST the admin page's own? Binding to loopback keeps other machines out, but a
    page on any other site can have the user's own browser send a POST here, and the socket
    would not know the difference. Two things tell it apart: the page sends JSON — a content
    type another origin can only send after a CORS preflight this server never answers — and a
    browser names the origin a cross-site request comes from."""
    ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return False
    origin = headers.get("Origin")
    if origin is None:
        return True  # not a browser's cross-site request
    netloc = origin.split("://", 1)[-1].split("/", 1)[0]
    return _loopback_host(netloc)


```

In `do_POST`, directly before `length = _parse_content_length(self.headers.get("Content-Length"))` (the second occurrence, after the `/api/descriptions` path check) insert:

```python
        if not _own_page(self.headers):
            log.warning("rejected POST /api/descriptions: not from the admin page")
            self._drop_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_origin"})
            return
```

- [ ] **Step 4: The document**

In `documentation/ARCHITECTURE.md` §12, after the **Host loopback guard** paragraph add:

```markdown
**Own-page guard on `POST`.** The `Host` header stops rebinding; it does not stop a page on
another site, open in the user's browser, from making that browser POST here — the socket is
loopback either way. So `POST /api/descriptions` also requires `Content-Type: application/json`
(a foreign origin can only send that after a CORS preflight this server never answers) and
refuses an `Origin` header that is not loopback (`403 forbidden_origin`, `_own_page`).
```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass (the page itself sends `Content-Type: application/json`, `app/admin/page.html`).

- [ ] **Step 6: Commit**

```bash
git add app/admin/server.py documentation/ARCHITECTURE.md tests/test_admin.py
git commit -m "fix(admin): a POST from another site is refused" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: Documentation in step with the code

**Files:**
- Modify: `documentation/ARCHITECTURE.md` §14a (the rules list and the undo paragraph)
- Modify: `README.md` (the Undo paragraph)

- [ ] **Step 1: §14a rules**

In `documentation/ARCHITECTURE.md`, replace these three bullets of the "Rules that hold here" list:

```markdown
* **It never asks a question.** Anything unclear becomes a line in the inbox note.
```

with:

```markdown
* **It never asks a question.** Anything unclear becomes a line in the inbox note — and so does
  the whole message when the model cannot be asked at all (Claude down, out of credit, an
  answer that is not JSON): written as it is, kind `kept`, no model needed, with the reply
  saying why it was not filed.
```

and:

```markdown
* **One Undo for both stores.** `UndoRecord.vault` carries the files to put back; when Notion
  wrote nothing, the vault's undo gets its own `executions` row with `kind: "vault"`, which
  `/undo` reaches. `Orchestrator._finish_vault` is the one place the two sides meet.
* **Never deletes.** Undo moves a created note to the vault's `.trash`; everything else is a
  restore of the previous text.
```

with:

```markdown
* **One Undo for both stores.** `UndoRecord.vault` carries the writes to take back; when Notion
  wrote nothing, the vault's undo gets its own `executions` row with `kind: "vault"`, and the
  reply carries the same Undo button a Notion write has. `Orchestrator._finish_vault` is the
  one place the two sides meet.
* **Undo takes back the write, not the file.** `VaultUndo` keeps the text before and after the
  write, and `app/vault/revert.py` removes exactly that change from whatever the file holds by
  then — a task added by the next message, or the links the linker put in, stay. A file whose
  lines the write touched were changed since is left alone and the reply names it
  (`texts.VAULT_UNDO_LEFT`). A note the write created goes to `.trash` only when nothing else
  was written into it meanwhile. Nothing is ever deleted outright.
* **One lock, one text.** Every write and undo takes `VaultWriter._lock`; the linker, which
  runs behind the reply, amends a note from the text it holds *now* (`VaultWriter.amend`),
  never from the text it read before its model call. A multi-action message whose third write
  fails keeps the first two and their undo; the reply counts what failed.
```

- [ ] **Step 2: README**

In `README.md`, replace:

```markdown
Every write comes back with an **Undo** button that works for a few minutes
(`UNDO_WINDOW_S`, see below). The bot never deletes or bulk-edits anything, and it never sees or
uses your Notion access token for anything other than talking to the Notion API.
```

with:

```markdown
Every write comes back with an **Undo** button that works for a few minutes
(`UNDO_WINDOW_S`, see below). Undo takes back that one message's change, not the whole file: a
note you added after it stays, and if you edited the very lines it wrote the bot leaves them
alone and says so. The bot never deletes or bulk-edits anything, and it never sees or
uses your Notion access token for anything other than talking to the Notion API.
```

- [ ] **Step 3: Run the suite one last time and commit**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

```bash
git add documentation/ARCHITECTURE.md README.md
git commit -m "docs: undo semantics, the kept inbox line and the admin guard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Do not merge yet: Part 2 continues on the same branch.

---

## Part 2: Fixing part of a vault write (Tasks 17–21)

**Spec:** `docs/superpowers/specs/2026-10-06-vault-fix-design.md`. Needs Tasks 2, 3 and 5 of Part 1. Prototyped on the same throwaway clone on top of Part 1 (1 372 tests, ruff clean) and checked live: seven real corrections with Haiku against a copy of the vault, all right after the drop guard in Task 19 (without it, «ну это» dropped the task it was shown). A fix costs one call of about 1 550 prompt tokens; a move into a folder adds that folder's question (about 4 000 in all).

### Review Focus (Part 2)

1. **A correction that says nothing** («ну это») must change nothing, whatever the model answers — `test_nothing_is_dropped_unless_the_correction_says_so`.
2. **One product of three dropped** from a grocery write that is one file change — the other two stay unticked, the dropped one is ticked again as before: `test_one_product_of_three_is_dropped_and_the_others_stay`.
3. **A fix on a turn where one of two lines was edited by hand** — nothing at all is touched, the note is named: `test_a_line_edited_by_hand_since_leaves_every_file_untouched`, `test_replace_touches_nothing_when_one_line_was_edited_since`.
4. **A reply to an expired write** — "too late", nothing written, no model call: `test_a_reply_to_an_expired_write_is_too_late_and_writes_nothing`.
5. **Fix, fix again, Undo** — nothing of any version is left, including a page the fix itself created: `test_a_reply_to_the_writes_message_is_a_fix_and_undo_then_removes_everything`.

---

### Task 17: A one-letter word no longer vouches for every word it begins

Found while building the fix: the staged gate's `_close` let «и» ("and") match any word starting with и, so a product nobody named («икра») passed as said. Fresh messages have the same hole.

**Files:**
- Modify: `app/vault/staged.py` (`_close`)
- Test: `tests/test_vault_staged.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_vault_staged.py`:

```python


def test_a_one_letter_word_does_not_vouch_for_every_word_it_begins():
    """«и» (and) began «икра»: a product nobody named passed the gate as said. A short word
    still matches its own longer form: «сыр» and «сыра»."""
    gate = _Gate("купить молоко и яйца", "")
    assert not gate.said("икра")
    assert gate.said("яйца")
    assert _Gate("купить сыр", "").said("сыра")
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_vault_staged.py -q -k one_letter`
Expected: FAIL on `assert not gate.said("икра")`.

- [ ] **Step 3: The fix**

In `app/vault/staged.py`, `_close`, replace:

```python
    short, other = sorted((one, two), key=len)
    return len(short) < 4 and other.startswith(short)
```

with:

```python
    short, other = sorted((one, two), key=len)
    # Three letters at least: a one-letter word ("and") began every word that shares its
    # letter, so a product nobody named passed as said.
    return 3 <= len(short) < 4 and other.startswith(short)
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/vault/staged.py tests/test_vault_staged.py
git commit -m "fix(vault): a one-letter word no longer passes the gate for every word it begins" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 18: Each write remembers what it did; a write is found by its message

**Files:**
- Modify: `app/vault/writer.py` (`VaultUndo.action`, `_as_written`, `run`, new `replace_writes`)
- Modify: `app/audit/store.py` (`execution_by_reply`)
- Test: `tests/test_vault_writer.py`, `tests/test_audit_store.py`

**Interfaces:**
- Consumes: `VaultWriter._lock`, `undo`, `revert.take_back` (Part 1, Tasks 1–2).
- Produces: `VaultUndo.action: dict | None` (a `VaultAction.model_dump(exclude_defaults=True)`); `VaultWriter.replace_writes(old: list[VaultUndo], new: list[VaultAction]) -> tuple[list[VaultWrite], list[str], list[str]]` — (writes, failed, left); `AuditStore.execution_by_reply(chat_id: int, reply_message_id: int) -> dict | None` (expired rows included).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_vault_writer.py`:

```python


# ---- each write remembers what it did --------------------------------------------------------

def test_a_write_remembers_the_action_it_carried_out(writer):
    write = writer.run(VaultAction(action="task", text="лампочки", heading="дом", due="2026-10-09"))
    assert write.undo.action == {"action": "task", "text": "лампочки", "heading": "дом",
                                 "due": "2026-10-09"}


def test_an_append_that_became_an_inbox_line_remembers_the_inbox_line(writer):
    write = writer.run(VaultAction(action="append", note="нет такой", body=["строка"]))
    assert write.kind == "inbox"
    assert write.undo.action == {"action": "inbox", "text": "строка"}


def test_a_grocery_write_remembers_its_products(writer):
    write = writer.run(VaultAction(action="grocery", body=["молоко", "хлеб"]))
    assert write.undo.action == {"action": "grocery", "body": ["молоко", "хлеб"]}


def test_replace_takes_back_old_writes_and_makes_new_ones_as_one_step(writer, index):
    old = writer.run(VaultAction(action="task", text="молоко", heading="дом"))
    writes, failed, left = writer.replace_writes(
        [old.undo], [VaultAction(action="grocery", body=["молоко"])])

    assert left == [] and failed == [] and [w.kind for w in writes] == ["grocery"]
    assert "молоко" not in index.read(old.path)
    assert "- [ ] молоко" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")


def test_replace_touches_nothing_when_one_line_was_edited_since(writer, index, vault):
    a = writer.run(VaultAction(action="task", text="молоко", heading="дом"))
    b = writer.run(VaultAction(action="task", text="позвонить", heading="дом"))
    edited = index.read(b.path).replace("- [ ] позвонить", "- [x] позвонить")
    (vault / b.path).write_text(edited, encoding="utf-8", newline="\n")

    writes, failed, left = writer.replace_writes(
        [a.undo, b.undo], [VaultAction(action="log", text="сходил")])

    assert writes == [] and left == [texts.VAULT_TASKS_NOTE]
    assert index.read(a.path) == edited  # the milk task is still there too
    assert not (vault / texts.VAULT_DAILY_DIR).exists()  # and nothing new was written
```

Append to `tests/test_audit_store.py`:

```python


def test_a_write_is_found_by_the_message_that_reported_it(tmp_path):
    from datetime import UTC, datetime, timedelta

    from app.audit.store import AuditStore

    store = AuditStore(tmp_path / "bot.sqlite")
    store.migrate()
    then = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    event = store.new_event(telegram_user_id=1, chat_id=7, kind="text")
    first = store.add_execution(event, 7, None, "{}", then + timedelta(minutes=5))
    store.set_reply_message_id(first, 501)

    assert store.execution_by_reply(7, 501)["id"] == first
    assert store.execution_by_reply(8, 501) is None  # another chat's message
    assert store.execution_by_reply(7, 502) is None
    store.close()
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_vault_writer.py tests/test_audit_store.py -q`
Expected: the six new tests FAIL (`action` is not a field; no `replace_writes`; no `execution_by_reply`).

- [ ] **Step 3: The writer**

In `app/vault/writer.py`, add a field at the end of `VaultUndo`:

```python
    # What the write did, as a VaultAction dump, so a later message can fix part of it
    # (app/vault/fix.py) without the model reading the message again. None on old records.
    action: dict | None = None
```

Above `class VaultWriter:` add:

```python
def _as_written(action: VaultAction, write: VaultWrite) -> VaultAction:
    """The action the write really carried out. A note that was not there turns an append or
    an update into an inbox line; a fix must see that line, not the append that never
    happened."""
    if write.kind.split("_")[0] == action.action:
        return action
    words = action.text or action.title or " ".join(action.body)
    return VaultAction(action="inbox", text=words.strip()[:MAX_LINE])


```

Replace `run` (the two-line version from Part 1, Task 2) with:

```python
    def run(self, action: VaultAction) -> VaultWrite:
        with self._lock:
            write = self._run(action)
        if write.undo is not None:
            write.undo.action = _as_written(action, write).model_dump(exclude_defaults=True)
        return write
```

and directly above it add:

```python
    def replace_writes(self, old: list[VaultUndo], new: list[VaultAction]
                       ) -> tuple[list[VaultWrite], list[str], list[str]]:
        """Take `old` writes back and write `new` in their place, as one step: a fix.

        First a dry run of every take-back on the files as they are now. If any write's lines
        were changed by hand since, nothing at all is touched and their notes are returned —
        half a fix would leave the old and the new side by side. Returns (writes, failed,
        left): the new writes, the error of each new action that could not be written, and
        the notes left alone."""
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
                return [], [], list(dict.fromkeys(left))
            for undo in reversed(old):
                self.undo(undo)
            writes: list[VaultWrite] = []
            failed: list[str] = []
            for action in new:
                try:
                    writes.append(self.run(action))
                except (OSError, ValueError) as e:
                    log.warning("fix write failed (%s): %s", action.action, e)
                    failed.append(type(e).__name__)
            return writes, failed, []

```

- [ ] **Step 4: The store**

In `app/audit/store.py`, directly above `def latest_execution` add:

```python
    def execution_by_reply(self, chat_id: int, reply_message_id: int) -> dict | None:
        """The write whose reply is this Telegram message, expired or not: the caller says
        "too late" for an expired one, rather than treating the reply as a new message."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE chat_id = ? AND reply_message_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_id, reply_message_id),
            ).fetchone()
            return dict(row) if row else None

```

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/vault/writer.py app/audit/store.py tests/test_vault_writer.py tests/test_audit_store.py
git commit -m "feat(vault): each write remembers its action; a write is found by its reply message" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 19: The fix stage and applying a fix

**Files:**
- Create: `app/vault/fix.py`
- Modify: `app/llm/staged_prompts.py` (`FIX_PROMPT`, `FIX_LINES`, line suffixes, two headers)
- Modify: `app/texts.py` (`BTN_FIX`, the `FIX_*` strings, `FIX_DROP_WORDS`, `FIX_DROP_STEMS`)
- Modify: `app/vault/staged.py` (`StagedFiler.fix`, `_Run.patch`, imports)
- Modify: `app/vault/pipeline.py` (`FixTurn`, `VaultPipeline.fix`)
- Create: `tests/test_vault_fix.py`

**Interfaces:**
- Consumes: `VaultUndo.action`, `VaultWriter.replace_writes` (Task 18); `staged._Gate`, `_Run._places`, `_notes`, `_add_notes`, `_change_note`; `filer.check`; `VaultPipeline._rewritten`, `_link_later`.
- Produces: `StagedFiler.fix(original: str, correction: str, previous: list[VaultAction], ctx: VaultContext) -> tuple[FixPlan, int, int]`; `VaultPipeline.fix(original: str, correction: str, undos: list[VaultUndo]) -> FixTurn` with `applied: bool`, `undos` (untouched + new), `reply_line()`, `model`, `prompt_tokens`, `output_tokens`, `writes`, `error`. Task 20 calls `VaultPipeline.fix`; Task 20's tests import `Script` from `tests/test_vault_fix.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vault_fix.py`:

```python
"""Fixing part of what a turn wrote (app/vault/fix.py): one question about the difference, code
that turns the answer into writes to take back and writes to make, and a gate on every value."""

from __future__ import annotations

from datetime import datetime

import pytest

from app import texts
from app.llm import staged_prompts as P
from app.vault.filer import context
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultAction, VaultWriter

NOW = datetime(2026, 10, 6, 18, 30)  # a Tuesday


class Script:
    """Answers each stage by its prompt, and records what each was shown."""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict, str]] = []

    async def __call__(self, system: str, schema: dict, content: str, max_tokens: int = 0):
        name = next(k for k, v in vars(P).items() if k.endswith("_PROMPT") and v == system)
        self.asked.append((name, schema, content))
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer, 100, 10

    @property
    def stages(self) -> list[str]:
        return [name for name, _, _ in self.asked]


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Книги").mkdir()
    for name, status in (("Дюна", "Read"), ("Солярис", "To read")):
        (tmp_path / f"Книги/{name}.md").write_text(
            f"---\nstatus: {status}\nauthor: x\n---\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(
        "## дом\n\n- [ ] счета #home\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_GROCERIES_NOTE}.md").write_text(
        "- [x] молоко\n- [x] хлеб\n- [x] яйца\n", encoding="utf-8")
    (tmp_path / "_bot.md").write_text("## Области\n- [[дом]] `#home`\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def index(vault):
    index = VaultIndex(vault)
    index.refresh()
    return index


def pipe(index, script: Script) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index, now=lambda: NOW),
                         StagedFiler(script, "haiku"), now=lambda: NOW)


def write(index, *actions: VaultAction):
    writer = VaultWriter(index, now=lambda: NOW)
    return [writer.run(a).undo for a in actions]


# ---- the question ------------------------------------------------------------------------------

async def test_the_model_is_shown_what_was_written_and_the_places_but_not_the_guide(index):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    script = Script(FIX_PROMPT={"changes": [], "add": [], "unclear": True})
    turn = await pipe(index, script).fix("молоко", "в продукты", undos)

    [(name, schema, content)] = script.asked
    assert name == "FIX_PROMPT"
    assert "a1: " + P.FIX_LINES["task"].format(words="молоко", note=texts.VAULT_TASKS_NOTE,
                                              folder="") in content
    assert texts.VAULT_GROCERIES_NOTE in content  # the place list
    assert "Области" not in content  # the guide is not sent
    assert schema["properties"]["changes"]["items"]["properties"]["key"]["enum"] == ["a1"]
    assert not turn.applied and texts.FIX_UNCLEAR in turn.reply_line()


# ---- applying it -------------------------------------------------------------------------------

async def test_a_task_moved_to_groceries(index):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))
    script = Script(FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "",
                                             "prop": "", "value": "", "to": "g"}],
                                "add": [], "unclear": False})
    turn = await pipe(index, script).fix("надо молоко", "не в задачи, а в продукты", undos)

    assert turn.applied and script.stages == ["FIX_PROMPT"]  # one call, nothing else
    assert "молоко" not in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] молоко" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert turn.reply_line() == texts.FIX_DONE.format(what=texts.FIX_MOVED.format(
        item="молоко", place=texts.VAULT_GROCERIES_NOTE))


async def test_a_due_date_is_set_only_when_the_correction_names_a_day(index):
    undos = write(index, VaultAction(action="task", text="позвонить маме", heading="дом"))
    change = {"key": "a1", "op": "set", "field": "due", "prop": "", "value": "2026-10-09",
              "to": ""}
    script = Script(FIX_PROMPT={"changes": [change], "add": [], "unclear": False})
    turn = await pipe(index, script).fix("позвонить маме", "на пятницу", undos)
    assert turn.applied
    assert "- [ ] позвонить маме 📅 2026-10-09" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")

    undos = write(index, VaultAction(action="task", text="полить цветы", heading="дом"))
    turn = await pipe(index, Script(FIX_PROMPT={"changes": [change], "add": [],
                                                "unclear": False})).fix(
        "полить цветы", "это важно", undos)
    assert not turn.applied  # no day was said: the date has no source
    assert "- [ ] полить цветы\n" in index.read(f"{texts.VAULT_TASKS_NOTE}.md") + "\n"


async def test_one_product_of_three_is_dropped_and_the_others_stay(index):
    undos = write(index, VaultAction(action="grocery", body=["молоко", "хлеб", "яйца"]))
    assert "- [ ] хлеб" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    script = Script(FIX_PROMPT={"changes": [{"key": "a2", "op": "drop", "field": "",
                                             "prop": "", "value": "", "to": ""}],
                                "add": [], "unclear": False})
    turn = await pipe(index, script).fix("купить молоко хлеб яйца", "хлеб не надо", undos)

    page = index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert "- [x] хлеб" in page  # back as it was before the first message
    assert "- [ ] молоко" in page and "- [ ] яйца" in page
    assert turn.reply_line() == texts.FIX_DONE.format(
        what=texts.FIX_DROPPED.format(item="хлеб"))


async def test_an_item_is_added_in_the_same_place(index):
    undos = write(index, VaultAction(action="grocery", body=["молоко"]))
    script = Script(FIX_PROMPT={"changes": [], "add": ["яйца", "икра"], "unclear": False})
    turn = await pipe(index, script).fix("купить молоко", "и ещё яйца", undos)

    page = index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert "- [ ] яйца" in page and "икра" not in page  # nobody said caviar
    assert turn.applied and turn.kept == undos  # the milk write was not touched


async def test_a_task_moved_into_a_folder_asks_that_folders_question_once(index):
    undos = write(index, VaultAction(action="task", text="Пикник на обочине", heading="дом"))
    books = next(k for k, f in enumerate(context(index, "", NOW).folders, start=1)
                 if f == "Книги")
    script = Script(
        FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "", "prop": "",
                                 "value": "", "to": f"f{books}"}], "add": [],
                    "unclear": False},
        FOLDER_PROMPT={"items": [{"title": "Пикник на обочине", "body": [], "tags": [],
                                  "props": [{"name": "status", "value": "To read"}]}],
                       "lookup": "", "web": "", "media": "text"})
    turn = await pipe(index, script).fix("надо Пикник на обочине", "это книга", undos)

    assert script.stages == ["FIX_PROMPT", "FOLDER_PROMPT"]
    assert turn.applied
    assert "Пикник" not in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    note = index.by_name("Пикник на обочине")
    assert note is not None and note.folder == "Книги"


async def test_a_line_edited_by_hand_since_leaves_every_file_untouched(index):
    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"),
                  VaultAction(action="task", text="позвонить", heading="дом"))
    path = index.root / f"{texts.VAULT_TASKS_NOTE}.md"
    edited = path.read_text(encoding="utf-8").replace("- [ ] молоко", "- [x] молоко")
    path.write_text(edited, encoding="utf-8", newline="\n")
    script = Script(FIX_PROMPT={"changes": [{"key": "a1", "op": "move", "field": "",
                                             "prop": "", "value": "", "to": "g"}],
                                "add": [], "unclear": False})
    turn = await pipe(index, script).fix("молоко, позвонить", "молоко в продукты", undos)

    assert not turn.applied and turn.left == [texts.VAULT_TASKS_NOTE]
    assert path.read_text(encoding="utf-8") == edited
    assert "- [ ] молоко" not in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")


async def test_nothing_is_dropped_unless_the_correction_says_so(index):
    """Found live: «ну это» made the model drop the task it was shown. A drop loses words, so
    it takes a word of the user's that asks for it."""
    drop = {"changes": [{"key": "a1", "op": "drop", "field": "", "prop": "", "value": "",
                         "to": ""}], "add": [], "unclear": False}
    undos = write(index, VaultAction(action="task", text="забрать посылку", heading="дом"))
    turn = await pipe(index, Script(FIX_PROMPT=drop)).fix("забрать посылку", "ну это", undos)
    assert not turn.applied
    assert "- [ ] забрать посылку" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")

    turn = await pipe(index, Script(FIX_PROMPT=drop)).fix("забрать посылку", "убери это",
                                                         undos)
    assert turn.applied
    assert "забрать посылку" not in index.read(f"{texts.VAULT_TASKS_NOTE}.md")


def test_the_words_that_may_drop_something():
    from app.vault.fix import may_drop

    for said in ("кефир не надо", "нет, без хлеба", "убери кефир", "удали это",
                 "лишнее", "отмени вторую"):
        assert may_drop(said), said
    for said in ("ну это", "на пятницу", "в продукты", "нечто другое"):
        assert not may_drop(said), said


async def test_a_rewritten_note_is_changed_again_rather_than_patched(index, vault):
    (vault / "Идеи.md").write_text("старый текст\n", encoding="utf-8")
    index.refresh()
    undos = write(index, VaultAction(action="rewrite", note="Идеи", text="перепиши",
                                     body=["новый текст"]))
    script = Script(CHANGE_NOTE_PROMPT={"kind": "rewrite", "props": [], "heading": "",
                                        "web": "", "media": "text"})
    plan, _, _ = await StagedFiler(script, "haiku").fix(
        "перепиши идеи", "верни первую строку", [VaultAction(**undos[0].action)],
        context(index, "идеи", NOW))

    assert script.stages == ["CHANGE_NOTE_PROMPT"]
    assert plan.take_back == [] and plan.write[0]["action"] == "rewrite"
    assert plan.write[0]["note"] == "Идеи"


async def test_claude_down_changes_nothing_and_says_why(index):
    import anthropic

    from app.vault.staged import claude_ask

    undos = write(index, VaultAction(action="task", text="молоко", heading="дом"))

    class Down:
        def __init__(self) -> None:
            self.messages = self

        async def create(self, **kwargs):
            raise anthropic.APIError("down", request=None, body=None)  # type: ignore[arg-type]

    filer = StagedFiler(claude_ask(Down(), "haiku"), "haiku")  # type: ignore[arg-type]
    turn = await VaultPipeline(index, VaultWriter(index), filer, now=lambda: NOW).fix(
        "молоко", "в продукты", undos)
    assert not turn.applied and turn.error
    assert "- [ ] молоко" in index.read(f"{texts.VAULT_TASKS_NOTE}.md")
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_vault_fix.py -q`
Expected: ERROR at collection — `FIX_LINES` / `VaultPipeline.fix` do not exist.

- [ ] **Step 3: The texts**

In `app/texts.py`, after `BTN_UNDO_ALL = "Отменить всё"` add `BTN_FIX = "Поправить"`. Directly above `VAULT_WHAT = {` add:

```python
# Fixing part of what was just written (app/vault/fix.py).
FIX_ASK = "✏️ Что поправить? Напишите или надиктуйте."
FIX_EXPIRED = "Поправить уже нельзя (прошло больше {minutes} минут)."
FIX_UNCLEAR = "не понял, что поправить — ничего не менял"
FIX_DONE = "✏️ Obsidian — поправлено: {what}"
FIX_FAILED = "⚠️ Obsidian — не поправил: {error}"
FIX_MOVED = "«{item}» → «{place}»"
FIX_SET = "«{item}» — {value}"
FIX_DROPPED = "«{item}» убрано"
FIX_ADDED = "+ «{item}»"
FIX_NOT_MOVED = "«{item}» не перенёс"
FIX_NOTE = "«{note}» ещё раз"
# A fix may drop something only when the correction says to: whole words, and word starts.
# Found live: «ну это» made the model drop the task it was shown.
FIX_DROP_WORDS = ("не", "нет", "без", "ненадо")
FIX_DROP_STEMS = ("убер", "убра", "удал", "лишн", "отмен", "ненуж", "выкин", "вычеркн")
```

- [ ] **Step 4: The prompt**

In `app/llm/staged_prompts.py`, directly above `def section(` add:

```python
FIX_PROMPT = f"""Пользователь поправляет то, что ассистент только что записал. {DICTATED}

Даны исходное сообщение, записанное (ключи a1, a2 …), список мест и поправка. Верни changes —
только для записанного, что поправка меняет; остальное не упоминай:
- drop — убрать совсем («хлеб не надо», «это не надо было»);
- set — поменять одно поле. field: text — сами слова (для заметки — её название), due — срок \
ГГГГ-ММ-ДД (считай от «сегодня»), repeat — правило повтора по-английски ("every week"), \
heading — раздел, tag — тэг без решётки, prop — свойство заметки (prop — его имя, value — \
значение). value — новое значение;
- move — перенести в другое место: to — ключ места из списка.
add — новые вещи того же рода и туда же, если поправка их добавляет («и ещё яйца» → \
["яйца"]), каждая отдельно, словами пользователя.
unclear — true, если непонятно, что именно поправить: поправка не говорит, что не так \
(«ну это», «не то»), — тогда ничего не меняй."""

# One line per written thing, as the fix stage shows it.
FIX_LINES = {
    "task": "задача «{words}» в «{note}»",
    "grocery": "продукт «{words}» в «{note}»",
    "note": "заметка «{words}» в папке «{folder}»",
    "append": "дописано в «{note}»: {words}",
    "log": "запись в дневнике: {words}",
    "inbox": "строка в «{note}»: {words}",
    "update": "изменено «{note}»: {words}",
    "rewrite": "переписано «{note}»",
}
FIX_DUE = ", срок {due}"
FIX_HEADING = ", раздел «{heading}»"
FIX_TAGS = ", тэги {tags}"
FIX_PROPS = ", свойства: {props}"
H_FIX_DONE = "Что записано"
H_ORIGINAL = "Исходное сообщение"


```

- [ ] **Step 5: The fix module**

Create `app/vault/fix.py` (no Cyrillic: `tests/test_reply.py` checks `app/` outside the four text modules):

```python
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
            moved = await move(its[i].action, key)
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
```

- [ ] **Step 6: The staged reader asks it**

In `app/vault/staged.py`, add to the imports (ruff orders them: `from app.vault import fix as fixing` before the `filer` import, `from app.vault.writer import VaultAction` after the `index` import):

```python
from app.vault import fix as fixing
from app.vault.writer import VaultAction
```

In `StagedFiler`, after `aclose` add:

```python
    async def fix(self, original: str, correction: str, previous: list[VaultAction],
                  ctx: VaultContext) -> tuple[fixing.FixPlan, int, int]:
        """What a correction changes in what a turn wrote (app/vault/fix.py)."""
        run = _Run(self._ask_model, f"{original}\n{correction}", ctx, lookup=self._lookup)
        started = monotonic()
        rewritten = next((a for a in previous if a.action == "rewrite"), None)
        if rewritten is not None:
            # A note that was rewritten or moved from is changed again, from what it says
            # now: the edit is not something a field can describe.
            raw = await run._change_note(correction, rewritten.note)
            plan = fixing.FixPlan(write=[a for a in raw if a.get("action") != "inbox"],
                                  said=[texts.FIX_NOTE.format(note=rewritten.note)])
            plan.unclear = not plan.write
        else:
            plan = await run.patch(original, correction, previous)
        log.info("fix %s: %s | %d call(s), %.1fs", self.model, " > ".join(run.trail) or "-",
                 run.calls, monotonic() - started)
        return plan, run.prompt_tokens, run.output_tokens
```

In `_Run`, directly above `def _tasks(self)` add:

```python
    async def patch(self, original: str, correction: str,
                    previous: list[VaultAction]) -> fixing.FixPlan:
        """One question about the difference: which written thing changes, and how."""
        ctx = self.ctx
        places = self._places()
        its = fixing.items(previous)
        keyed = {f"a{i}": fixing.describe(item.action, ctx.tasks_note)
                 for i, item in enumerate(its, start=1)}
        answer = await self._ask(
            P.FIX_PROMPT, fixing.schema(list(keyed), list(places)),
            P.message(self._today(), P.section(P.H_ORIGINAL, original),
                      P.section(P.H_FIX_DONE, P.keyed(keyed)),
                      P.section(P.H_PLACES, P.keyed(places)), text=correction))
        self.trail.append("fix")
        if answer.get("unclear") and not answer.get("changes") and not answer.get("add"):
            return fixing.FixPlan(unclear=True)
        # What was written already passed the gate once: its words are a source too.
        written = "\n".join(fixing.words(item.action) for item in its)
        gate = _Gate(f"{self.message}\n{written}", ctx.guide, self._pages())
        notes = self._notes()
        folders = {f"f{i}": folder for i, folder in enumerate(ctx.folders, start=1)}
        names = {TASKS: ctx.tasks_note, GROCERIES: texts.VAULT_GROCERIES_NOTE,
                 DIARY: texts.VAULT_DAILY_DIR, INBOX: texts.VAULT_INBOX_NOTE,
                 **folders, **notes}

        async def move(action: VaultAction, key: str) -> list[dict]:
            simple = fixing.simple_move(action, key, tasks=TASKS, groceries=GROCERIES,
                                        diary=DIARY, inbox=INBOX, notes=notes)
            if simple is not None:
                return simple
            if key in folders:
                # A book note needs what a task never had: that folder's own question, for
                # this one thing, in its own words and the correction's.
                return await self._add_notes(f"{fixing.words(action)}\n{correction}",
                                             folders[key])
            return []

        return await fixing.plan(previous, answer, gate, tags=ctx.tags, places=places,
                                 place_names=names, move=move,
                                 dropping=fixing.may_drop(correction))

```

- [ ] **Step 7: The pipeline applies it**

In `app/vault/pipeline.py`, directly above `class VaultPipeline:` add:

```python
@dataclass
class FixTurn:
    """What a fix did: the writes it made, the turn's writes it left as they were, and the
    phrases the reply is made of."""

    writes: list[VaultWrite] = field(default_factory=list)
    kept: list[VaultUndo] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    # Something was actually changed on disk: the old row is done with.
    applied: bool = False
    error: str = ""
    reason: str = ""
    left: list[str] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def undos(self) -> list[VaultUndo]:
        return [*self.kept, *(w.undo for w in self.writes if w.undo is not None)]

    def reply_line(self) -> str:
        if self.left:
            notes = ", ".join(f"«{name}»" for name in self.left)
            return texts.VAULT_UNDO_LEFT.format(notes=notes)
        if not self.applied:
            why = texts.LLM_DOWN_SHORT.get(self.reason) or self.error or texts.FIX_UNCLEAR
            return texts.FIX_FAILED.format(error=why)
        what = "; ".join(self.said) or ", ".join(w.what for w in self.writes)
        if self.error:
            what += f" — {self.error}"
        return texts.FIX_DONE.format(what=what)


```

In `VaultPipeline`, directly above `async def _kept(` (Part 1, Task 7) add:

```python
    async def fix(self, original: str, correction: str,
                  undos: list[VaultUndo]) -> FixTurn:
        """Change part of what one turn wrote (app/vault/fix.py). `undos` are that turn's
        writes, each carrying the action it did. Never raises, never asks."""
        turn = FixTurn(model=self._filer.model)
        fix = getattr(self._filer, "fix", None)
        previous = [VaultAction(**u.action) for u in undos if u.action is not None]
        if fix is None or len(previous) != len(undos) or not previous:
            turn.error = texts.FIX_UNCLEAR
            return turn
        try:
            await asyncio.to_thread(self._index.refresh)
            ctx = context(self._index, f"{original}\n{correction}", self._now())
            plan, turn.prompt_tokens, turn.output_tokens = await fix(
                original, correction, previous, ctx)
        except FilerError as e:
            log.warning("fix failed: %s", e)
            turn.error, turn.reason = str(e), e.reason
            return turn
        except OSError as e:
            log.warning("vault unreadable: %s", e)
            turn.error = type(e).__name__
            return turn
        if plan.unclear:
            return turn
        # What the fix writes is checked like any answer; a web search is not part of a fix.
        actions = [a.model_copy(update={"research": "", "media": ""})
                   for a in check(plan.write, self._index, correction)]
        scratch = VaultTurn()
        prepared: list[VaultAction] = []
        for action in actions:
            if action.action == "rewrite":
                rewritten = await self._rewritten(action, scratch)
                prepared += [rewritten] if rewritten is not None else []
            else:
                prepared.append(action)
        if not prepared and not plan.take_back:
            turn.error = scratch.error
            return turn
        old = [undos[i] for i in plan.take_back]
        turn.writes, failed, turn.left = await asyncio.to_thread(
            self._writer.replace_writes, old, prepared)
        if turn.left:
            return turn
        turn.applied = True
        turn.kept = [u for i, u in enumerate(undos) if i not in set(plan.take_back)]
        turn.said = plan.said
        if failed:
            turn.error = texts.VAULT_SOME_FAILED.format(n=len(failed))
        log.info("vault fix %s: took back %d, wrote %d", turn.model, len(old),
                 len(turn.writes))
        self._link_later(turn.writes)
        return turn

```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_vault_fix.py -q`
Expected: 11 passed.

- [ ] **Step 9: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass (`test_no_stray_cyrillic_outside_texts_and_llm_modules` included).

- [ ] **Step 10: Commit**

```bash
git add app/vault/fix.py app/vault/staged.py app/vault/pipeline.py app/llm/staged_prompts.py app/texts.py tests/test_vault_fix.py
git commit -m "feat(vault): a correction changes part of what a turn wrote, in one small call" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 20: The Поправить button and the reply gesture

**Files:**
- Modify: `app/conversation/orchestrator.py` (`_vault_buttons`, `_fixing`, `handle_text(reply_to=)`, `_message`, `_callback`, `_fixable`, `_fix_too_late`, `_fix_ask`, `_fix`, `_undo`, `_cancel`, `_finish_vault`)
- Modify: `app/telegram/handlers.py` (`_replied_to`, `_on_text`, `_on_voice`)
- Test: `tests/test_vault_orchestrator.py`, `tests/test_handlers.py`

**Interfaces:**
- Consumes: `VaultPipeline.fix` (Task 19), `AuditStore.execution_by_reply` (Task 18), `texts.BTN_FIX` / `FIX_ASK` / `FIX_EXPIRED`.
- Produces: callback `f:<execution_id>`; `Orchestrator.handle_text(..., reply_to: int | None = None)`; audit decisions `FIX_ASK` and `FIX` (error `FIX_EXPIRED`).

- [ ] **Step 1: Update the pins and write the failing tests**

In `tests/test_vault_orchestrator.py`, change the import `from datetime import datetime` to `from datetime import datetime, timedelta`. In `test_notion_off_leaves_a_working_obsidian_bot` (as changed in Part 1, Task 3), replace:

```python
    # No question, so the only button is Undo for the vault's own write.
    assert [[b.id for b in r] for r in reply.buttons] == [[f"u:{row['id']}"]]
```

with:

```python
    # No question, so the buttons are Undo and Fix for the vault's own write.
    assert [[b.id for b in r] for r in reply.buttons] == [[f"u:{row['id']}", f"f:{row['id']}"]]
```

Append to `tests/test_vault_orchestrator.py`:

```python


# ---- fixing part of a write ---------------------------------------------------------------------

@pytest.fixture
def staged_bot(bot, tmp_path):
    """The same bot with the staged reader, scripted per stage, and Notion off."""
    from app.vault.staged import StagedFiler
    from tests.test_vault_fix import Script

    script = Script()
    bot.vault._filer = StagedFiler(script, "haiku")
    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
    bot.script = script
    return bot


def _milk_task(script) -> None:
    script.answers.update(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                          TASKS_PROMPT={"items": [{"text": "молоко", "due": "", "repeat": "",
                                                   "heading": "дом", "tag": "",
                                                   "countdown": False}], "lookup": ""})


MOVE_TO_GROCERIES = {"changes": [{"key": "a1", "op": "move", "field": "", "prop": "",
                                  "value": "", "to": "g"}], "add": [], "unclear": False}


async def test_the_fix_button_asks_then_the_next_message_moves_the_task(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")

    asked = await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")
    assert asked.text == texts.FIX_ASK
    bot.script.answers["FIX_PROMPT"] = MOVE_TO_GROCERIES
    fixed = await bot.orch.handle_text(CHAT, USER, "не в задачи, а в продукты")

    assert fixed.text.startswith(texts.FIX_DONE.split("{")[0])
    assert "молоко" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert [b.id for b in fixed.buttons[0]] == [f"u:{fixed.undo_id}", f"f:{fixed.undo_id}"]
    # The first message's buttons are done with.
    stale = await bot.orch.handle_callback(CHAT, USER, f"u:{first.undo_id}")
    assert stale.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)


async def test_a_reply_to_the_writes_message_is_a_fix_and_undo_then_removes_everything(
        staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.store.set_reply_message_id(first.undo_id, 5001)  # what the transport records

    bot.script.answers["FIX_PROMPT"] = MOVE_TO_GROCERIES
    fixed = await bot.orch.handle_text(CHAT, USER, "в продукты", reply_to=5001)
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")

    bot.script.answers["FIX_PROMPT"] = {"changes": [{"key": "a1", "op": "set", "field": "text",
                                                     "prop": "", "value": "молоко овсяное",
                                                     "to": ""}], "add": [], "unclear": False}
    again = await bot.orch.handle_callback(CHAT, USER, f"f:{fixed.undo_id}")
    assert again.text == texts.FIX_ASK
    twice = await bot.orch.handle_text(CHAT, USER, "молоко овсяное")
    assert "- [ ] молоко овсяное" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{twice.undo_id}")
    assert undone.text == texts.UNDONE
    assert "молоко" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    # The grocery page did not exist before the first message: the fix made it, and Undo
    # takes it to the trash with everything else.
    assert not (bot.dir / f"{texts.VAULT_GROCERIES_NOTE}.md").exists()


async def test_a_reply_to_an_expired_write_is_too_late_and_writes_nothing(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.store.set_reply_message_id(first.undo_id, 5001)
    bot.orch._clock = Clock(NOW + timedelta(minutes=6))

    reply = await bot.orch.handle_text(CHAT, USER, "в продукты", reply_to=5001)

    assert reply.text == texts.FIX_EXPIRED.format(minutes=5)
    assert "FIX_PROMPT" not in bot.script.stages
    assert "в продукты" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_a_reply_to_a_message_that_reported_no_write_is_an_ordinary_message(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    reply = await bot.orch.handle_text(CHAT, USER, "надо молоко", reply_to=9999)
    assert reply.text.startswith("✅ Obsidian —")
    assert "FIX_PROMPT" not in bot.script.stages


async def test_a_fix_the_model_cannot_place_changes_nothing_and_can_be_tried_again(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.script.answers["FIX_PROMPT"] = {"changes": [], "add": [], "unclear": True}
    await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")

    reply = await bot.orch.handle_text(CHAT, USER, "ну это")

    assert reply.text == texts.FIX_FAILED.format(error=texts.FIX_UNCLEAR)
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert (await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")).text \
        == texts.FIX_ASK


async def test_cancel_forgets_a_fix_that_was_asked_for(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")
    await bot.orch.cancel(CHAT)

    bot.script.answers.update(TARGET_PROMPT={"target": "g"},
                              GROCERY_PROMPT={"names": ["хлеб"]})
    reply = await bot.orch.handle_text(CHAT, USER, "купить хлеб")
    assert "FIX_PROMPT" not in bot.script.stages
    assert reply.text.startswith("✅ Obsidian —")
```

In `tests/test_handlers.py`, change `FakeOrchestrator.handle_text` to accept and record `reply_to`:

```python
    async def handle_text(self, chat_id, user_id, text, *, kind="text", transcript=None,
                          progress=None, reply_to=None):
        self.progress = progress
        self.reply_to = reply_to
```

(the rest of the method unchanged), and append:

```python


async def test_a_reply_to_one_of_the_bots_messages_names_that_message():
    hs = build()
    original = Message(message_id=4242, date=datetime.datetime.now(UTC), chat=_chat(),
                       from_user=User(id=777, is_bot=True, first_name="bot"),
                       text="✅ Obsidian — задача")
    message = _bound(Message(message_id=9, date=datetime.datetime.now(UTC), chat=_chat(),
                             from_user=_user(ALLOWED_USER), text="в продукты",
                             reply_to_message=original), hs.bot)
    update = Update(update_id=9, message=message)

    assert await dispatch(hs.app, update, hs.context)
    assert hs.orch.reply_to == 4242

    plain = text_update(ALLOWED_USER, "купить хлеб", hs.bot, update_id=10)
    assert await dispatch(hs.app, plain, hs.context)
    assert hs.orch.reply_to is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/test_vault_orchestrator.py tests/test_handlers.py -q`
Expected: the new tests and the changed pin FAIL (`handle_text() got an unexpected keyword argument 'reply_to'`, no Fix button).

- [ ] **Step 3: The orchestrator**

In `app/conversation/orchestrator.py`, after `_undo_buttons` add:

```python


def _vault_buttons(execution_id: int) -> list[list[Button]]:
    """Undo, and Fix: a vault-only write can be changed in part (app/vault/fix.py)."""
    return [[Button(f"u:{execution_id}", texts.BTN_UNDO),
             Button(f"f:{execution_id}", texts.BTN_FIX)]]
```

In `Orchestrator.__init__`, after `self._waiting: dict[int, int] = {}` add:

```python
        # Chats whose next message is a fix, after the Fix button: chat -> execution id. In
        # memory on purpose: a restart forgets it, and pressing the button again costs nothing.
        self._fixing: dict[int, int] = {}
```

Replace `handle_text` with:

```python
    async def handle_text(
        self, chat_id: int, user_id: int, text: str, *, kind: str = "text",
        transcript: str | None = None, progress: Callable[[Reply], Awaitable[None]] | None = None,
        reply_to: int | None = None,
    ) -> Reply:
        """`reply_to` is the id of the bot's message this one replies to in Telegram, if any:
        a reply to a write's message is a fix of that write."""
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, user_id, kind,
                                    lambda t: self._message(t, text, reply_to),
                                    progress=progress, raw_input=text, transcription=transcript)
```

Directly above `async def _text(` add:

```python
    async def _message(self, turn: _Turn, text: str, reply_to: int | None) -> Reply:
        """A fix, when the Fix button was pressed or the message replies to a write's reply;
        otherwise an ordinary message."""
        execution_id = self._fixing.pop(turn.chat_id, None)
        if execution_id is None and reply_to is not None:
            row = self._store.execution_by_reply(turn.chat_id, reply_to)
            execution_id = row["id"] if row is not None else None
        if execution_id is not None:
            return await self._fix(turn, text, execution_id)
        return await self._text(turn, text)

```

In `_callback`, replace its first two lines and the `u` branch head:

```python
    async def _callback(self, turn: _Turn, data: str) -> Reply:
        prefix, _, rest = data.partition(":")
        if prefix == "u" and rest.isdigit():
```

with:

```python
    async def _callback(self, turn: _Turn, data: str) -> Reply:
        prefix, _, rest = data.partition(":")
        # Any button other than Fix means the chat has moved on from a fix it asked for.
        self._fixing.pop(turn.chat_id, None)
        if prefix == "f" and rest.isdigit():
            return self._fix_ask(turn, int(rest))
        if prefix == "u" and rest.isdigit():
```

Directly above `async def _undo(` add:

```python
    # ---- fixing part of a vault write -----------------------------------------------------

    def _fixable(self, turn: _Turn, execution_id: int) -> tuple[dict, UndoRecord] | None:
        """The row and its record, when that write can still be fixed: this chat's, within the
        undo window, not undone, the vault's alone, and every write knowing what it did."""
        row = self._store.get_execution(execution_id, turn.now)
        if row is None or row["undone"] or row["chat_id"] != turn.chat_id:
            return None
        record = UndoRecord.model_validate_json(row["undo"])
        if (record.kind != "vault" or not record.vault
                or any(u.action is None for u in record.vault) or not self._vault_on()):
            return None
        return row, record

    def _fix_too_late(self, turn: _Turn) -> Reply:
        turn.audit(decision=_kind("FIX"), error="FIX_EXPIRED")
        return Reply(texts.FIX_EXPIRED.format(minutes=max(1, self._s.undo_window_s // 60)))

    def _fix_ask(self, turn: _Turn, execution_id: int) -> Reply:
        if self._fixable(turn, execution_id) is None:
            return self._fix_too_late(turn)
        self._fixing[turn.chat_id] = execution_id
        turn.audit(decision=_kind("FIX_ASK"))
        return Reply(texts.FIX_ASK)

    async def _fix(self, turn: _Turn, text: str, execution_id: int) -> Reply:
        """Change part of what a turn wrote. The old row is done with once anything changed;
        the new one carries the untouched writes and the new ones, so its Undo takes the whole
        thing back to before the first message."""
        found = self._fixable(turn, execution_id)
        if found is None:
            return self._fix_too_late(turn)
        row, record = found
        assert self._vault is not None
        turn.audit(decision=_kind("FIX"))
        turn.source_text = text
        event = self._store.get_event(row["event_id"]) or {}
        original = (event.get("raw_input") or event.get("transcription") or "").strip()
        result = await self._vault.fix(original, text, record.vault)
        if result.model:
            turn.call(result.model, "fix", fixed=execution_id, writes=len(result.writes) or None,
                      error=result.error or None, prompt_tokens=result.prompt_tokens,
                      output_tokens=result.output_tokens)
        line = result.reply_line()
        if not result.applied:
            return Reply(line)
        self._store.mark_undone(execution_id)
        undos = result.undos
        if not undos:  # everything was dropped: nothing is left to take back
            return Reply(line)
        turn.execution_id = self._store.add_execution(
            turn.event_id, turn.chat_id, None,
            UndoRecord(kind="vault", vault=undos).model_dump_json(),
            self._clock() + timedelta(seconds=self._s.undo_window_s))
        return Reply(line, _vault_buttons(turn.execution_id), undo_id=turn.execution_id)

```

At the top of `_undo`'s body add `self._fixing.pop(turn.chat_id, None)`; at the top of `_cancel`'s body add the same line.

In `_finish_vault`, in the branch from Part 1, Task 3, replace:

```python
                # Notion wrote nothing, or is switched off: the vault's write is the only
                # thing to take back, and it gets the same button a Notion write has. A reply
                # that already carries buttons is a question; /undo still reaches the row.
                reply = replace(reply, buttons=_undo_buttons(turn.execution_id),
                                undo_id=turn.execution_id)
```

with:

```python
                # Notion wrote nothing, or is switched off: the vault's write is the only
                # thing to take back or fix, and it gets the buttons for both. A reply that
                # already carries buttons is a question; /undo still reaches the row.
                reply = replace(reply, buttons=_vault_buttons(turn.execution_id),
                                undo_id=turn.execution_id)
```

- [ ] **Step 4: The handler**

In `app/telegram/handlers.py`, directly above `async def _on_text(` add:

```python
def _replied_to(update: Update) -> int | None:
    """The id of the message this one replies to: a reply to the bot's report of a write is a
    fix of that write (the orchestrator decides which messages are such reports)."""
    replied = update.message.reply_to_message if update.message is not None else None
    return replied.message_id if replied is not None else None


```

In `_on_text`, change `progress=_progress(update, context),` to `progress=_progress(update, context), reply_to=_replied_to(update),`. In `_on_voice`, after `kind="voice", transcript=transcript, progress=_progress(update, context),` add a line `reply_to=_replied_to(update),`.

- [ ] **Step 5: Run the suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/conversation/orchestrator.py app/telegram/handlers.py tests/test_vault_orchestrator.py tests/test_handlers.py
git commit -m "feat: Поправить button and reply-to-fix for vault writes" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 21: Documentation, the live check, and the merge

**Files:**
- Modify: `documentation/ARCHITECTURE.md` §14a (one paragraph after the Undo rules from Task 16)
- Modify: `README.md` (one paragraph after the Undo paragraph from Task 16)
- Create (scratchpad, not committed): `live_fix.py`

- [ ] **Step 1: ARCHITECTURE.md**

After the "**One lock, one text.**" bullet added in Task 16, add:

```markdown
* **A write can be fixed in part** (`app/vault/fix.py`, spec
  `docs/superpowers/specs/2026-10-06-vault-fix-design.md`). Each `VaultUndo` keeps the action
  it carried out. The **Поправить** button, or a Telegram reply to the write's message, makes
  the next message a correction: one call to the filer model sees the written actions as keyed
  lines, the place list and the correction, and answers drop / set / move / add. Values pass
  the staged gate over the original message, the correction and the written words; a drop
  needs a word of the user's that asks for it (`texts.FIX_DROP_WORDS`). Code takes back the
  changed writes and writes the new ones as one step (`VaultWriter.replace_writes`, nothing
  touched if a line was edited by hand), and the new row's Undo removes everything back to
  before the first message. Fix and Undo share one window and one expiry.
```

- [ ] **Step 2: README.md**

After the Undo paragraph (Task 16), add:

```markdown
Next to Undo, an Obsidian reply has **Поправить**: press it (or reply to the bot's message)
and say what is wrong — «не в задачи, а в продукты», «на пятницу», «кефир не надо», «и ещё
яйца». Only that part changes; one small model call, and Undo then takes the whole thing back.
```

- [ ] **Step 3: Run the suite and commit**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

```bash
git add documentation/ARCHITECTURE.md README.md
git commit -m "docs: fixing part of a vault write" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: The live check**

Write this script to your scratchpad directory (not the repo) and run it from the repo root with `uv run python <scratchpad>/live_fix.py`. It never touches the real vault (a temp copy) and never prints the key. Use the editor tool to create it, not a heredoc.

```python
"""Live check of the fix stage: real Haiku, a throwaway copy of the vault."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import texts
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultWriter

SOURCE = Path("C:/data/obsidian")


class Keys(BaseSettings):
    model_config = SettingsConfigDict(env_file="C:/apps/ai_assistant/.env",
                                      env_file_encoding="utf-8", extra="ignore")
    anthropic_api_key: SecretStr = SecretStr("")


CASES = [
    ("надо купить молоко", "не в задачи, а в продукты"),
    ("позвонить маме", "на пятницу"),
    ("купить хлеб, кефир и сыр", "кефир не надо"),
    ("купить хлеб", "и ещё яйца"),
    ("надо прочитать Пикник на обочине", "это книга, добавь в книги"),
    ("записаться к стоматологу", "не к стоматологу, а к окулисту"),
    ("надо забрать посылку", "ну это"),
]


def lines_with(path: Path, word: str) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if word.casefold() in ln.casefold()]


async def main() -> None:
    key = Keys().anthropic_api_key.get_secret_value()
    if not key:
        raise SystemExit("no key")
    for first, correction in CASES:
        root = Path(tempfile.mkdtemp()) / "vault"
        shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns(
            ".obsidian", ".trash", ".stversions", "Вложения"))
        index = VaultIndex(root)
        index.refresh()
        filer = StagedFiler.claude(key, "claude-haiku-4-5", lookup_model="claude-sonnet-5")
        pipe = VaultPipeline(index, VaultWriter(index), filer, now=datetime.now)
        turn = await pipe.handle(first)
        print(f"\n=== «{first}» → {turn.reply_line()}")
        fixed = await pipe.fix(first, correction, turn.undos)
        print(f"    «{correction}» → {fixed.reply_line()}  "
              f"[{fixed.prompt_tokens}+{fixed.output_tokens} tok]")
        for word in (first.split()[-1], correction.split()[-1]):
            for name in (texts.VAULT_TASKS_NOTE, texts.VAULT_GROCERIES_NOTE):
                for line in lines_with(root / f"{name}.md", word[:4]):
                    print(f"      {name}: {line}")
        for note in [n for n in index.notes if n.folder == "Книги" and "икник" in n.name]:
            print(f"      Книги: {note.name} {note.props}")
        await filer.aclose()
        shutil.rmtree(root.parent, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
```

Cases and what each must show:

| first message | correction | expected |
|---|---|---|
| надо купить молоко | не в задачи, а в продукты | молоко unticked in Продукты, not in Задачи |
| позвонить маме | на пятницу | 📅 on Friday's date |
| купить хлеб, кефир и сыр | кефир не надо | кефир back as it was; хлеб, сыр stay |
| купить хлеб | и ещё яйца | яйца added, хлеб untouched |
| надо прочитать Пикник на обочине | это книга, добавь в книги | a note in Книги, nothing in Задачи |
| записаться к стоматологу | не к стоматологу, а к окулисту | the task's text changed |
| надо забрать посылку | ну это | nothing changed, «не понял, что поправить» |

Run it and report the table of results to the user. Any case that goes wrong is fixed and pinned with a test before the merge.

- [ ] **Step 5: Merge and push**

Merge `reliability-fixes` into `main` and push (standing instruction: finished green branches are merged and pushed without asking; the release is the user's).
