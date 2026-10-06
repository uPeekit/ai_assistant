# Fixing part of what the bot just did — design

Decided with the user on 2026-10-06, as an addition to the reliability work
(`2026-10-06-reliability-fixes-design.md`, plan `../plans/2026-10-06-reliability-fixes.md`).
It builds on that plan's change-based Undo (Tasks 1–2), the Undo button on vault-only replies
(Task 3) and partial-write reporting (Task 5).

## Purpose

Sometimes the bot did almost the right thing: the right words in the wrong place, a missing
date, one item of three that should not be there. Today the only way back is Undo and saying it
all again. A **fix** is a follow-up message that changes part of what a turn wrote and leaves the
rest. It costs less than a fresh message, because the bot already knows what it did and asks the
model only about the difference.

## Decisions

| Question | Decision |
|---|---|
| Which store | The Obsidian side only. Prod runs with Notion off; a turn that also wrote to Notion gets no Fix button and a reply to it is an ordinary message. |
| How a message becomes a fix | Explicitly, never by a model's guess: the **Поправить** button next to Отменить, or a Telegram **reply** to the bot's message about the write. |
| How long | The same window as Undo (`UNDO_WINDOW_S`, 5 minutes), one expiry for both buttons. |
| Undo after a fix | Removes everything: the state before the original message. A fix is the original reshaped, not a second layer on it. |
| How the fix is computed | One small model call that patches the earlier actions (approach A). A moved action whose new place needs details gets one more call for that action alone. Nothing the user did not ask to change is re-read or rewritten. |

## What is stored

`VaultUndo` (app/vault/writer.py) gains `action: dict | None` — the `VaultAction` the write
carried out, as written (place, text, due date, tags, properties, body). The execution row
already holds the chat, the expiry, the undo record and the Telegram message id
(`reply_message_id`). No new table, no migration. Rows made before this have `action = None`
and cannot be fixed (the reply says so as for an expired one).

## The two ways in

**The button.** A vault-only reply carries `[Отменить] [Поправить]`; Поправить is `f:<execution_id>`.
On a press the orchestrator checks the row (same chat, not undone, not expired, every vault
entry has an `action`) and answers `texts.FIX_ASK` («Что поправить?»). It remembers, in memory,
that this chat's next text or voice message is a fix to that row. Any other button, `/cancel`,
`/undo`, or the row's expiry forgets it. A restart forgets it too; pressing again costs nothing.

**A reply.** The Telegram handler passes `reply_to_message.message_id` to
`Orchestrator.handle_text`. `AuditStore.execution_by_reply(chat_id, message_id)` finds the row.

- No row (a reply to a question, an answer, a digest, a progress line): an ordinary message.
- A row that is expired, undone, mixed with Notion, or older than this feature:
  `texts.FIX_EXPIRED` («Поправить уже нельзя — прошло больше N минут»), nothing written. The
  text reads as a correction; filed as a new note it would be litter.
- A valid row: a fix.

A reply split over several Telegram messages records only the last one's id; a reply to an
earlier piece is an ordinary message. Vault replies are one or two lines.

## The fix question

New module `app/vault/fix.py`; its Russian text goes in `app/llm/staged_prompts.py`
(`FIX_PROMPT` and line labels). One call to the filer model (Haiku) with:

- the correction, in the user's words;
- the earlier actions, one line each, keyed `a1…aN` («a1: задача «молоко» → Задачи, срок —,
  тэги —»), at most the first three lines of a body;
- the place list the staged target stage builds (`_Run._places`), labels only — no guide;
- today's date and weekday, so «на пятницу» can be resolved.

Schema (structured output): `changes`: list of `{key: enum(a1…aN), op: enum(keep, drop, set,
move), field: enum(text, title, due, repeat, heading, tag, done, prop), prop: string, value:
string, to: enum(place keys)}`; `add`: list of strings; `unclear`: boolean.

## What the code does with the answer

- Every value passes `staged._Gate` built over the original message **plus** the correction: a
  date only when one of them names a time, a title or property only when its words are there, a
  heading only when the guide knows it, a tag only when the vault has it. A refused value is
  dropped, not guessed.
- `set` changes one field of a copy of the action. `drop` removes it. `keep` (and every action
  not named) is left exactly as written — its file is not touched.
- `move` between the simple places — task file, groceries, diary, inbox — carries the text
  over, no further call. A move into a folder or onto a note runs that place's details stage
  (`_add_notes` / `_append`) once, for that action's own words plus the correction.
- `add` makes new actions of the same kind and place as the turn's first action (the staged
  reader files a message to one place).
- A turn whose action was a `rewrite` or a `move` of a note is not patched: the correction goes
  to the existing change-a-note stage (`_change_note`) with that note as the target.
- The result goes through `filer.check` like any other answer.
- Nothing changed, or `unclear`: `texts.FIX_UNCLEAR` («не понял, что поправить — ничего не
  менял»). The row stays valid; the user can try again.

## Applying it

All under `VaultWriter`'s lock:

1. **Dry run.** For every write being changed or dropped, `revert.take_back` must succeed on the
   file as it is now. If any cannot — the user edited those lines since — nothing is touched and
   the reply names the note (`texts.VAULT_UNDO_LEFT` wording).
2. Take those writes back, newest first.
3. Write the new and changed actions (partial failures reported as in reliability Task 5).
4. The linker runs on the new writes as usual.

A new execution row: its undo holds the untouched writes' entries plus the new ones, its
expiry is a fresh `UNDO_WINDOW_S` from now. The old row is marked undone, so its buttons answer
as an expired Undo does. The reply is `texts.FIX_DONE` («✏️ Obsidian — поправлено: …», one
item per changed action, «молоко → продукты») with `[Отменить] [Поправить]`: a fix can be fixed.

## Failures

- Claude cannot be asked: «Поправить сейчас не могу: …» (`LLM_DOWN_SHORT` reason). Nothing
  written; the row stays valid.
- The details call for a moved action fails: that action stays where it was; the rest of the fix
  applies; the reply says which one did not move.
- The audit row gets `decision: FIX`, the model call(s) in `llm_response`, and the fixed
  execution id. The log line: `fix: a1 move g | 1 call, 0.9s`.

## Tests

- `app/vault/fix.py` with a scripted model: each op; the gate refusing a date or a name nobody
  said; `unclear`; a rewrite turn routed to the change stage.
- The pipeline on a temporary vault with the real writer: a task moved to groceries; a due
  date set; one item of three dropped; an item added; a fix refused because the line was edited
  by hand, leaving every file untouched.
- The orchestrator: the button flow; the reply flow; a reply to an expired row and to a message
  with no row; fix, fix again, then Undo leaves nothing of either.
- The handler passes the replied-to message id; a message that is not a reply passes `None`.
- Before merging: about six real corrections replayed on a copy of the vault with real Haiku
  (scratchpad script, as for the staged reader), results reported to the user.

## Out of scope

Fixing a Notion write; fixing a turn older than the Undo window; a model deciding on its own
that an unmarked message is a correction.
