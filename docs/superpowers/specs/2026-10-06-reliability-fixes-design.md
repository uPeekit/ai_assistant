# Reliability fixes after the 0.6.17 review — design and decisions

Source: an architecture review of v0.6.17 (2026-10-06), with every finding re-checked against
the code and the fixes prototyped on a throwaway clone (full suite green: 1 347 tests, ruff clean)
before this was written. Prod runs with `notion=off`, so the Obsidian side is what a user meets
every day; the Notion side is paused behind a switch, not retired.

## What is fixed, and why

| # | Problem (verified in code) | Decision |
|---|---|---|
| 1 | Vault Undo restores the file's whole previous text. Two messages a minute apart on the task file, Undo on the first: the second task is gone. A note the first message created and the second appended to is moved to the trash whole. | Undo takes back *the write's own change* (a line-level reverse diff, `app/vault/revert.py`), leaves a file alone when the lines it touched were changed since, and says so. `VaultUndo` gains `written`. |
| 2 | A multi-action message whose third write raises loses the first two writes' undo and reports "not written". | Each write is attempted; the ones on disk keep their undo and the reply names how many failed. |
| 3 | The linker reads a note, waits on a model call, then writes the stale text back — over a line the next message appended, or re-creating a note Undo just trashed. Vault writes also run in worker threads with no lock. | One lock in `VaultWriter`; the linker amends from the text as it is *now* (`VaultWriter.amend`). `VaultIndex` readers iterate a copy. |
| 4 | With Notion off (prod), no reply carries an Undo button — only `/undo` reaches the row. | The vault's own execution row gets the same button a Notion write has, when the reply has no question keyboard. |
| 5 | When Claude cannot be asked, the vault writes nothing. With Notion off, nothing keeps the message. | The words go to the inbox note as they are (no model needed), kind `kept`; the reply says both. |
| 6 | `Editor.plan` and `Rewriter.rewrite` cut their input at 20 000 characters and the callers write the answer back as the whole: text past the cut is deleted unread. Both stores. | A text over the cap is refused (`TooLong`); the vault reply says to name a section, Notion answers `REWRITE_TOO_LONG`. |
| 7 | Telegram takes 4 096 characters per message; a longer reply or digest is refused whole. The mail digest had already moved its bookmark. A button press that waited behind a long turn fails at `query.answer()` and is lost. | `app/telegram/sending.py`: `pieces` / `send_text`, used by replies and both digests. `answer()` failures are suppressed. |
| 8 | Unread mail beyond `MAIL_MAX_PER_RUN` is skipped for good: the newest are taken and the bookmark moves past the rest. A failed single fetch logs "kept for the next run" and is not kept. A non-JSON Ollama answer raises out of `MailService.run`. | The oldest `limit` go now and the bookmark stops at the last of them. Truthful log line. `ValueError` → `ClassifyError`. |
| 9 | The agenda is re-sent on any restart within two hours of its time. A time moved on the admin page applies only after the old time fires. | `LastRun` memory in `data/daily_state.json`; the wait re-reads the schedule every 60 s. |
| 10 | Notion: `_append` trusts every reported id not in `known`, but the page read is capped (100 for list matching, 300 for edits). On a longer page Undo deletes the user's blocks from the cap to the end. Two inserts after one line land in reverse order. | Only the first `len(batch)` results are ours, minus `known`. List matching reads the same span as edits. Inserts after one anchor are applied last-first. The fake honours `limit`. |
| 11 | Notion: a required multi-select/relation answered by a button crashes the rebuild (`TypeError`) on every press until the session expires. | The rebuild accepts the one stored option as a list of one. |
| 12 | Staged reader: three sequential Haiku calls per message (4–10 s in prod logs); no per-call timing in the log. | Per-call seconds logged. The target question is asked alongside the intent (it does not depend on it); for the ~1 in 9 messages that are not an "add" the answer is thrown away. |
| 13 | Admin `POST /api/descriptions` accepts any content type and no `Origin` check: a page on another site, open in the user's browser, could flip switches or rewrite the research instructions. | JSON content type required (a foreign origin cannot send it without a preflight the server does not answer) and a non-loopback `Origin` is refused. |

## Dropped, with the reason

- **Removing the single-reader path** (`Filer`, `FILER_PROMPT`, the strong second reading, the vault hand-over): staged has been the default for a week with nine prod messages. Keep the revert switch for a month, then delete.
- **`concurrent_updates` in PTB**: the per-chat lock already serialises everything one chat does, so a long turn still blocks that chat's `/cancel`; the gain is for a second user, the risk is a voice message and the text after it changing places. The lost button press is fixed on its own (item 7).
- **Research search budget per API request**: enforcing it per turn means changing the tool list between turns, which breaks the prompt cache that pays for the loop; worst case is about $0.40 on a rare operation.
- **DNS rebinding in the web reader**: the body of a page reached that way goes into the user's own note, not to an attacker; the fix is a pinned-IP transport. Not worth it for one user.
- **Notion snapshot staleness, list fields replaced rather than merged, partial Notion writes, titles page size, context growth, confidence thresholds, plan-step name matching**: real, but Notion is switched off and each is a design change rather than a guard. Revisit if Notion comes back.
- **Shared Anthropic client, unclosed clients at shutdown, events table growth, HTML parsing on the loop, dead settings, duplicated JSON wrappers**: no user-visible effect at this scale.
- **A grocery fast path with no model call, a reasoning field before the target enum, all open tasks shown to the `done` stage**: classification changes that need a measured run on the judged set first; the per-call timing from item 12 is the first step.

## Behaviour the user may want to veto

- Item 4 adds an Undo button to every vault-only reply (prod today). It was pinned absent by `test_notion_off_leaves_a_working_obsidian_bot` only as "no question keyboard".
- Item 5 writes an inbox line for every message while Claude is down, questions included.
- Item 12 sends one extra Haiku call for a message that is not an "add".
