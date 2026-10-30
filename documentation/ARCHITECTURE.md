# Architecture

This document records how the app is built and why. The bot once wrote to Notion as well; that
code is in the git history.

## 1. Decisions made after spec review

| Topic | Decision | Why |
|---|---|---|
| Writable scope | Everything under `OBSIDIAN_VAULT` is a target, except `.obsidian/` and the other folders in `index.SKIP_DIRS`. No allow/deny lists. | User request. The vault folder is the single access control for the owner; a group chat gets a scope of its own (§14a). |
| Where a message goes | The model picks one key from a list the app builds per request: the task file, the grocery page, the diary, a folder, a note the message names. The answer's schema is an `enum` of those keys, and `filer.check` re-checks what comes back. | "LLM as classifier": no invented places, no second fuzzy-match step, fewer tokens. |
| Confirmation | Nothing is confirmed first. A write happens, and its reply carries **Undo** and **Fix** for `UNDO_WINDOW_S`. | Minimal questions: a wrong write costs one press. |
| Anything unclear | A line in the inbox note (`texts.VAULT_INBOX_NOTE`), never dropped. A group chat has no inbox: it is told what it may not do here. | Nothing the pipeline gives up on should vanish, and a line to sort out later costs less than a question now. |
| Vault freshness | `VaultIndex` is refreshed by modification time on every message. | Always fresh: a `stat` per note and a read of only what changed. |
| The user's rules | `_bot.md` in the vault, the folders' own shape, and the admin page's fields. Nothing about one user's vault lives in code. | The repo is general logic; the rules live with the data. |
| Reply language | Russian. All user-facing strings live in one module. | MVP. |
| Admin UI stack | Stdlib `http.server` in a thread, one embedded HTML page, one JSON endpoint. | No extra dependencies for a rarely used page. |
| Remote AI later | Speech behind a protocol; `OLLAMA_BASE_URL` for a local mail model. | User may add a dedicated AI machine. |
| Package/tooling | `uv`, Python 3.12 (uv-managed), `ruff`, `pytest`, `pydantic-settings`. | 3.13 wheel coverage for ctranslate2 is uneven. |
| Model access | The Anthropic SDK directly, with structured output where an answer has to fit a schema. Claude is required: with no key the vault, web research and link answers stay off, and nothing falls back to a local model. | Claude reads messages noticeably better than any model an 8 GB laptop GPU can run. |

## 2. Runtime topology

Single `asyncio` process on the host machine, one event loop for the whole run
(python-telegram-bot's own — `Application.run_polling()`; see `app/main.py`'s module docstring for
why nothing here is ever given a second, throwaway loop of its own):

```text
python-telegram-bot (long polling), one asyncio event loop for the whole process
   │
   ├─ app.main.post_init_checks (runs once, inside this same loop, before polling starts):
   │     the "/" command menu, admin server start
   ├─ telegram/handlers.py: text, voice, callback, command handlers → Orchestrator
   ├─ speech: faster-whisper, loaded lazily on the first voice message, run via
   │     asyncio.to_thread (GPU if available, else CPU — see §10)
   ├─ vault: app/vault/ → markdown files under OBSIDIAN_VAULT
   ├─ llm: anthropic SDK → api.anthropic.com (ANTHROPIC_API_KEY)
   ├─ web: httpx → Jina Reader, or the page itself (public addresses only, §14a)
   ├─ mail: IMAP, read-only; a local mail model over httpx → Ollama (OLLAMA_BASE_URL)
   ├─ sqlite: data/bot.sqlite  (audit, undo records, reminders, chat memory)
   ├─ background tasks, started after a clean post_init: the morning agenda, the mail digest and
   │     the watched folders (DailyMessage), the Syncthing check (Sweeper), reminders
   │     (ReminderScheduler), and one warm-up of the Google copies
   └─ admin: stdlib http.server on a daemon thread, bound to 127.0.0.1:ADMIN_UI_PORT (§12)
```

No webhooks, no Docker, no public port. `app.main.build()` constructs every collaborator above
exactly once and wires the two Telegram lifecycle hooks: `post_init` (the async startup checks,
then the background tasks) and `post_shutdown` (stop the background tasks, stop the admin server,
close the sqlite connections) — see §15 and `documentation/ERRORS.md`'s "Startup checks" for the
exact, ordered list and exit codes.

## 3. Module layout

```text
ai_assistant/
├── app/
│   ├── config.py               Settings (pydantic-settings, .env)
│   ├── main.py                 wiring, startup checks, Sweeper, process entry point
│   ├── logging_setup.py        event_id-tagged structured logging; httpx/httpcore/ExtBot floor
│   ├── version.py              reads the running version (pyproject.toml, or a shipped VERSION)
│   ├── instance_lock.py        one bot per install (exit 5)
│   ├── texts.py                all Russian user-facing strings
│   ├── address.py              the bot's own name, recognised without a model
│   ├── pointing.py             messages that point back at something said before
│   ├── chats.py                group chats and their scopes (data/chats.json)
│   ├── daily.py                messages the bot sends on its own at set times
│   ├── switches.py             the admin page's on/off switches
│   ├── tuning.py               the admin page's vocabulary and wording
│   ├── telegram/
│   │   ├── handlers.py         message/voice/callback/command handlers → Orchestrator
│   │   ├── keyboards.py        Reply → telegram.InlineKeyboardMarkup
│   │   ├── auth.py             allowlist filter (declarative gate + the one manual callback check)
│   │   ├── sending.py          text cut to what Telegram will take
│   │   ├── recent.py           a listed group's recent unaddressed messages, in memory only
│   │   └── media.py            an Instagram post sent back as a reply to its link
│   ├── speech/
│   │   ├── base.py             SpeechToText protocol, SpeechEmpty/SpeechError
│   │   └── whisper_local.py    faster-whisper implementation (lazy load, CUDA→CPU fallback, §10)
│   ├── conversation/
│   │   ├── orchestrator.py     one entry point: handle_text/handle_callback/undo
│   │   ├── reply.py            Button/Reply model
│   │   ├── undo.py             UndoRecord: what one execution row takes back
│   │   └── memory.py           ChatMemory: each chat's recent exchanges
│   ├── vault/
│   │   ├── pipeline.py         VaultPipeline: a message end to end, plans, fix, undo
│   │   ├── index.py            VaultIndex: names, aliases, tags, properties, headings
│   │   ├── filer.py            Filer (the single reader), VaultContext, check, doubtful
│   │   ├── staged.py           StagedFiler (the default reader), its _Gate
│   │   ├── planner.py          VaultPlanner: several steps in one message
│   │   ├── fix.py              fixing part of a write
│   │   ├── writer.py           VaultWriter, VaultAction, VaultUndo, VaultWrite
│   │   ├── revert.py           one write taken back out of a file changed since
│   │   ├── mdedit.py           where a line goes, how a line changes, edit scripts
│   │   ├── frontmatter.py      note properties
│   │   ├── names.py            note and file names
│   │   ├── summary.py          the reply's account of one write
│   │   ├── search.py           finding things, with no model call
│   │   ├── omnisearch.py       Obsidian's own search, over its local HTTP server
│   │   ├── answer.py           VaultAnswerer: a question answered from what was found
│   │   ├── askers.py           who is who in the owner's records
│   │   ├── agenda.py           what is due, what to do next
│   │   ├── groceries.py        the grocery page, a registry
│   │   ├── linker.py           links between notes
│   │   └── scope.py            what one chat may see and do
│   ├── llm/
│   │   ├── prompts.py          prompts for the reader, research, links, edits
│   │   ├── staged_prompts.py   prompts of the staged reader and the planner
│   │   ├── edits.py            Editor: one place in a note, or a whole new text
│   │   ├── rewrite.py          Rewriter: a whole text, when no editor is wired
│   │   ├── answer.py           LinkAnswerer: a question about a linked page
│   │   ├── research.py         WebResearcher: the research loop
│   │   ├── image_search.py     pictures from Wikimedia Commons and cited pages
│   │   └── health.py           why Claude could not be used, said once
│   ├── web/
│   │   ├── reader.py           a page as text (Jina Reader, or a direct fetch)
│   │   ├── links.py            the links in a message, read before it is
│   │   ├── images.py           ImageCheck: is an image link real
│   │   ├── sites.py            the owner's site table (data/sites.json)
│   │   └── trim.py             a page cut down to what a lookup needs
│   ├── google/
│   │   ├── auth.py             the owner's sign-in, kept fresh
│   │   ├── client.py           read-only Drive, Docs, Sheets, Forms calls
│   │   ├── sources.py          which files for whom, local copies, finding what a question needs
│   │   ├── sections.py         documents as titled text sections
│   │   └── errors.py           the two kinds of Google failure
│   ├── mail/
│   │   ├── imap.py             the mailbox, read without changing anything
│   │   ├── classify.py         sorting mail with Claude
│   │   ├── local.py            sorting mail with a local Ollama model
│   │   ├── buckets.py          the buckets, the user's text
│   │   ├── marks.py            which letters each digest covered
│   │   └── service.py          one digest run
│   ├── reminders/
│   │   ├── store.py            reminders in SQLite
│   │   ├── scheduler.py        the loop that sends them
│   │   ├── timing.py           when one first fires, how the reply says it
│   │   ├── repeat.py           when a repeating one is due next
│   │   └── rules.py            the admin page's reminder rules
│   ├── watch/
│   │   ├── syncthing.py        Syncthing's health
│   │   └── folders.py          watched folders
│   ├── media/
│   │   └── instagram.py        an Instagram post as files Telegram can show
│   ├── audit/
│   │   ├── store.py            AuditStore: events and executions in SQLite
│   │   └── migrate.py          migration runner: discover/pending/apply, checksum guard
│   └── admin/
│       ├── server.py           stdlib HTTP server thread (§12)
│       └── page.html           the settings page
├── tools/
│   ├── migrate.py              apply/inspect migrations from the command line
│   ├── benchmark_filer.py      models scored on the vault reader's case file
│   ├── benchmark_research.py   the research loop, timed and priced on fixed questions
│   ├── regressions.py          real messages the bot once got wrong, through the real pipeline
│   ├── vault_cases.py          a case set built from messages the bot actually handled
│   ├── check_sites.py          every site in data/sites.json checked live
│   ├── google_login.py         the owner's Google sign-in
│   └── rename_tag.py           a tag renamed across the vault
├── migrations/                 NNNN_name.sql, applied only by tools/migrate.py or the installer
├── deploy/                     install.ps1, run.ps1, start.ps1, update.ps1, *_wizard.ps1 (RELEASE.md)
├── release.py, apply_update.py, release.cmd, update.cmd, start.cmd
├── tests/
├── data/                        runtime files (gitignored): bot.sqlite, the admin page's files
├── documentation/
├── pyproject.toml
└── .env.example
```

`app/speech/whisper_remote.py` (a remote-Whisper HTTP client) does not exist yet — remote speech
recognition is not built; today
`WHISPER_DEVICE=cpu` is the only way to run transcription without a GPU, not a way to move it to
another machine. `OLLAMA_BASE_URL` has no such limitation — it already works against Ollama
running on a different host (README.md).

## 4. Request pipeline

```text
Telegram update
  → auth (allowlist, or a listed group) → reject silently if not allowed
  → voice? download → SpeechToText → text (audited, not echoed)
  → Orchestrator.handle_text(chat_id, user_id, text, …)      (or handle_callback/undo)
      → the chat's lock, then the `events` row opened (§7)
      → a reply to a write's message, or a pending Fix?      → VaultPipeline.fix (§7)
      → no vault built, or switched off?                     → VAULT_NOT_SET, NOTHING_ENABLED
      → the bot's name taken off the message; the name alone, or a question about the bot
                                                             → texts.py, no model
      → links read (not in a scoped group)                   (§14a)
      → VaultPipeline.handle(text, …)                        (a scoped group: .scoped(scope))
            → the answer to «who are you in the records»?    (§7)
            → index refreshed
            → several steps?  a light question beside the reader → plan (§6)
            → the reader: raw actions                        (§6)
            → filer.check, then a group's gate()             (§8)
            → reminders set, web research run, a link question answered
            → rewrite and move read their note and become writes (§9)
            → agenda and search questions answered
            → the writer, one action at a time               (§9)
            → the linker, behind the reply
      → _finish_vault: the reply line, an `executions` row, [Отменить] [Поправить]
      → the Claude warning, at most once an hour             (§14a)
      → the `events` row closed
```

Callback payloads: `u:<execution_id>` (undo), `f:<execution_id>` (fix). The mail digest's own
`mr:`/`mu:` buttons are answered by a handler of their own and never reach the orchestrator. Any
other payload is a button from an older version of the bot and replies `BUTTON_EXPIRED`.

Two rules hold on every path through the orchestrator. **One audit row per handled message:**
`_open` inserts the `events` row, every stage writes its columns onto the in-memory `_Turn`, and
`_finish` flushes them once, the error exits included. **It never raises to its caller:** every
failure becomes a `Reply` carrying a message from `texts.ERRORS`, and `_turn` catches whatever
nobody expected and replies `INTERNAL`.

`Orchestrator._finish_vault` is the one place a vault turn becomes a reply. It waits for the
vault's task, says in one line what was done (`VaultTurn.reply_line`), and gives a write a row of
its own with the Undo and Fix buttons. The window is measured from the write, not from when the
message arrived: a web search can spend minutes before anything is written.

**Inbox note**: a message the pipeline could not otherwise place — an action that names a note
that is not there, a reader that answered nothing usable, a model that could not be asked at all,
a part of a plan that should have written and did not — is appended to the inbox note instead of
being dropped. Never for a group chat, which has no inbox: its reply says what it may not do here,
or that the bot did not understand. See §5.

## 5. The vault pipeline

`OBSIDIAN_VAULT` points at a folder of markdown files; `app/vault/` holds the whole pipeline:

```text
message ── app.vault.pipeline.VaultPipeline
               index.VaultIndex     what is in the vault (names, aliases, tags, headings),
                                    refreshed by mtime
               staged.StagedFiler   three small Haiku questions: message -> actions, on the
               (or filer.Filer)     user's own guide note (_bot.md), the folders and the tags
                                    (§6)
               filer.check          the deterministic gate: an unknown note or folder, a made-up
                                    date or repeat rule never reaches the writer (§8)
               edits.Editor         one Sonnet call for a note the user asked to change: the
                                    pipeline reads the note (or one section), the model returns
                                    edits or markdown, the writer writes it (§9)
               writer.VaultWriter   task / note / append / update / rewrite / log / grocery /
                                    inbox, atomic, with the text before and after kept
                                    for Undo
               linker.Linker        after the reply: names and aliases, then a Haiku pass for
                                    the links matching cannot see
               search.search        a question ("что у меня по дому?") looked up in the index
                                    and the notes, with no model call of its own
               answer.VaultAnswerer one call that reads what the search found and answers
               agenda               dates: the morning digest, "what is planned on X", and
                                    "what should I do now" — read from the files, never invented
```

Rules that hold here:

* **It does not ask about a message.** Anything unclear becomes a line in the inbox note — and so
  does the whole message when the model cannot be asked at all (Claude down, out of credit, an
  answer that is not JSON): written as it is, kind `kept`, no model needed, with the reply
  saying why it was not filed. The one question it holds open is who the asker is in the owner's
  records (§7). A reminder with no time, or a web question the research cannot run, gets a reply
  saying what to send; nothing waits for the answer.
* **Only a message is read.** A button press goes to `Orchestrator._callback`, and a fix to
  `VaultPipeline.fix`, which reads the difference rather than the message again.
* **One Undo per turn.** `UndoRecord.vault` carries the writes to take back and
  `UndoRecord.reminders` the reminders to cancel; together they are one `executions` row with
  `kind: "vault"`. `Orchestrator._finish_vault` writes it.
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
* **Stays inside the vault**, and never touches `.obsidian/`.
* A failure is one line in the reply, never a failed message. Only an exception nobody expected
  answers `INTERNAL` (ERRORS.md).

## 6. Reading a message

Two readers, one contract: both return the same raw actions, so `filer.check`, the writer and the
undo do not know which one ran.

**The staged reader** (`app/vault/staged.py`, the default since 0.6.10; `FILER_MODE=single`
brings the old one back). On the 53 real messages where the two readers disagreed, the user
judged the staged reader right 36 times, both fine 10, the single one right 4 and neither 3;
the seven losses were each fixed and replayed. The single
reader asks one question about everything — what is this, where does it go, what does it say
— and the light model answers that unreliably. The staged one asks three narrow ones:

1. *intent* — add / done / change / move / ask / remind / unclear;
2. *target* — exactly one key from a list the code builds: the task file, the grocery page,
   the diary, every folder (with the properties its notes share), the pages the user's guide
   links to, and any other note the message names word for word;
3. *details* — a small schema that belongs to that target (tasks; products; notes of that
   folder; lines to append), so a question about meetings cannot be answered with a book.

Every value is then checked against the message (`_Gate`, §8).

One target per message is a rule, not a simplification. Three ways of letting a message reach
several places were measured — a target per item, a "several" key, a leftover handed back by
the details stage — and each was used on plain messages far more often than on mixed ones (a
list of chores scattered over three places). A message that really is two notes for two
places lands in the first.

Measured on the dictated message that started this, against the live vault: single reader 0
of 6, staged 8 of 8, on the same model and about a fifth fewer tokens.

**The reader plans for itself** (since 0.6.9). A details stage may name a *lookup* — a set the
user did not list («все романы Пелевина», «все фильмы Нолана», «что взять в поход») — which the
lookup model (Sonnet) answers in that place's own shape, one item per note, line or task; or a
*web search*, which the pipeline runs and whose result becomes the note's body without ever
entering a prompt. Code iterates; no model is asked per item. Guards: a lookup never touches a
note that already exists (it knows the book, not what the user did with it) and matches names
with punctuation and ё folded; a lookup on the task file needs a word that asks for a set, or it
invents work; a web search needs one of the user's search words.

**A second reading** (`filer.doubtful`, since 0.6.7, `FILER_MODE=single` only): when the light
filer's answer has a defect code can see - a note with nothing in it, or an inbox line - the same
question goes once to `FILER_STRONG_MODEL` and that answer is checked and written instead.
Measured on a dictated message that named a list as its destination with the preposition
swallowed: Haiku 0 of 6 right even with a prompt rule written for that sentence, Sonnet 3 of 3,
the two together 4 of 5. It hides inside the turn's own wait, and never runs when the caller
supplies the text (an empty note is then about to be filled) or in a group. The staged reader
has no second reading: one Sonnet question about everything would undo its narrow ones.

**Plans** (`app/vault/planner.py`, `VaultPipeline._run_plan`; staged reader only, and in a group
only with the `ask` right). Beside the reader, every message gets one Haiku question: can it be
done as one step — one kind of action into one place, several items and one search included?
Neither waits for the other, so a one-step message costs no extra wait. Yes: the usual path,
unchanged (its stage prompts are byte-identical with and without the planner). No: the reader's
answer is dropped and Sonnet (thinking off) returns linked steps — `write`, `ask`, `read`,
`research`, `derive` — each a self-contained request, with `needs` for order and data and `reply`
on the step that is the chat answer. A plan that is one search feeding one write, or only
questions that hand nothing to each other, goes the usual way. Steps run in rounds (a round's
steps together); write and ask steps run through `handle(plain=True, inputs=...)` — the results
they need are a data section for the stages and a source for the gate, and found or derived text
reaches a write as content, written as it stands; a read step hands on the owner's records that
match, whole, as data; research steps run the web research with their own text; derive steps are
Sonnet over their inputs. Everything merges into one `VaultTurn(planned=True)`: one reply (short
when the plan wrote something), one Undo. A failed step is named in the reply and skips only what
needs it; if a write failed, the original message goes to the inbox once. Limits: 8 steps, 3
searches, 6000 characters per handed result.

## 7. Conversation state

There is no conversation to resume: a message is read on its own, with the chat's recent
exchanges beside it. What decides how a message is taken:

```text
message ─┬─ replies to a write's message, still fixable ─────▶ VaultPipeline.fix
         ├─ after [Поправить], write still fixable ──────────▶ VaultPipeline.fix
         ├─ after [Поправить], its window closed ────────────▶ FIX_EXPIRED, nothing written
         ├─ the asker's name, after «who are you» ───────────▶ the first question, answered now
         └─ anything else ───────────────────────────────────▶ VaultPipeline.handle (§4)
```

**One chat's turns are serialised.** Every entry point (`handle_text`, `handle_callback`, `undo`)
takes that chat's `asyncio.Lock` for the whole turn, so no two turns of one chat overlap;
different chats stay fully concurrent, and the lock is dropped again once no turn holds it. It is
not a nicety: the guard on `executions.undone` before an undo is a read, an `await`, then a write,
and a double-tapped Telegram button arrives as two callbacks ~100 ms apart in two concurrent
handlers, where both reads would see the state from before either write and the write would be
reverted twice.

What is kept between turns, and for how long:

| What | Where | Until |
|---|---|---|
| A pending Fix: (chat, presser) → (execution id, pressed at) | `Orchestrator._fixing`, in memory on purpose | that person's next message or another button; a restart forgets it, and pressing the button again costs nothing |
| The records question: (chat, user) → (question, asked at) | `Askers._waiting`, in memory | that person's next message; a reply past 10 minutes, longer than 4 words or with a «?» is a new message |
| The names a person goes by in the records | `data/askers.json` | kept |
| Each chat's recent exchanges | `chat_memory` (`ChatMemory`) | 2 hours |
| A listed group's unaddressed messages | `app/telegram/recent.py`, process memory only | the last 20 per group |

**A write can be fixed in part** (`app/vault/fix.py`). Each `VaultUndo` keeps the action
it carried out. The **Поправить** button, or a Telegram reply to the write's message, makes
the next message a correction; a reply to the write's message is itself the correction and
outranks a pending press. In a group a reply to someone else's write, or to one that can no longer
be fixed, is an ordinary message. A record without its actions (from before they were kept) and
a filer without `fix` (`FILER_MODE=single`) cannot be fixed. Usually one call to the filer
model (a move into a folder asks that folder's question too; a turn that rewrote a note goes
to the change-a-note stage) sees the written actions as keyed lines, the place list and the
correction, and answers drop / set / move / add. Values pass the staged gate over the
original message, the correction and the written words; a drop needs a word of the user's
that asks for it (`texts.FIX_DROP_WORDS` as whole words, `texts.FIX_DROP_STEMS` as word
starts, and «не» only as «не надо / не нужно»). Code takes back the changed writes and
writes the new ones as one step (`VaultWriter.replace_writes`); the take-back is
all-or-nothing — nothing is touched if a line was edited by hand — but not the whole fix: a
new write that fails is named in the reply. The new row's Undo removes everything back to
before the first message. Fix and Undo share one window and one expiry. A reminder the turn set
stays as it was (`texts.FIX_REMINDER_KEPT`), and Undo of the fixed turn still cancels it.

**The records question** (`app/vault/askers.py`). A question about oneself ("what did I rate it")
is answered from records that name people, where the asker may be under a short form of their
name. When the answerer cannot tell which entry is theirs, the bot asks, the same person's next
short message in that chat is the answer, and the question they asked first is answered then. In
a plan the whole message is what waits, so the name replays all of it.

**Chat answers and memory** (`app/conversation/memory.py`, `app/vault/pipeline.py`).
`ChatMemory` (`app/conversation/memory.py`, migration 0003) keeps each chat's exchanges; the
orchestrator writes the message and the reply after every text turn and reads the last 10 within
2 hours into `turn.history` and `turn.answers` (the bot's recent answers to questions, marks
stripped). The vault side gets them in `VaultContext.history/answers/pointed`: the intent stage
reads follow-ups with the history, and a «?» without a write word is `ask` even when the model
says `add`. The answerer returns `kind` — `notes` (🔍, with sources), `general` (💬, from the
model's knowledge), `web` (a query: «🔎 Ищу в интернете…», the web research, then
`from_web` → 🌐), `none` (only for questions about the user's own life) or `who` (the
records question above). «запиши это»: the
FOLDER/APPEND/TASKS stages may return `from` — a key of a recent answer (`a1` newest), the quoted
message (`q`) or a recent group message (`m1…`) — and the code writes that text verbatim. Groups:
`app/telegram/recent.py` keeps the last 20 unaddressed messages per listed group in process memory
only, handed over only when a message points back (`points_back`) or replies to one.

## 8. Gates

Deterministic, after every model answer: a model suggests, code decides what may reach a file.
There are no confidence numbers and no thresholds. Each gate below refuses a value for a reason it
can name, and what it refuses is dropped or becomes an inbox line, never a question.

**`_Gate`** (`app/vault/staged.py`) judges the staged reader's values against the message: a date
counts only when the message names a time, a property value or a title only when its words are in
the message or in the guide, a task-file heading only when the guide knows it. What the message's
links say is a source for a title, a field or a product, never for a date: a page is full of
numbers the user did not mean. A plan step's text is a source for a title or a place it names,
never for a value. A task whose words equal an open line points at that line instead of adding a
twin. A fix passes the same gate (§7).

**`filer.check`** runs on either reader's answer, and turns it into the actions the writer may
carry out:

- at most 25 actions; one that is not a known kind becomes an inbox line;
- a date that is not a real day, a time that is not `HH:MM` or a repeat rule that does not start
  with `every` is blanked;
- an `append`, `update`, `rewrite` or `move` naming a note that is not there becomes an inbox line
  holding the user's own words, and so does a `move` with nowhere to go;
- a new note in a folder that does not exist goes to the notes folder, and a `note` that only sets
  properties on a note already there becomes an `update` of it rather than a copy with " 2" on
  its name;
- an item of a looked-up set that the vault already has is left exactly as it is;
- a product already on the grocery page is filed there, whatever the model answered (§9);
- a web search is kept only on a `note` or an `append`, the only actions that can hold what it
  found.

**`scope.gate`** holds a group chat's actions to its rights (§14a), and a guarded writer refuses
any other file in case the gate has a hole.

The writer has a last fallback of its own: an action whose note has disappeared by the time it
runs is written to the inbox note rather than lost (§9).

## 9. Actions and the writer

`VaultAction` (`app/vault/writer.py`) is what the readers produce and what the writer carries out,
one at a time. Every write is atomic (temp file + rename) and keeps the file's text before and
after, so Undo can take it back.

| Action | Fields | What the writer does |
|---|---|---|
| `task` | text, time, tags, due, repeat, countdown, heading | a tick-box line in the task file, under `heading` when there is one: `- [ ] 10:00 words #tag 🔁 every day 📅 2026-11-06` |
| `note` | folder, title, body, props, tags | a new file in that folder (the notes folder when none), with a unique name and the folder's shape |
| `append` | note, heading, body or text | lines added to the note, into that section when there is one |
| `update` | note, props, task, done, due, time, heading + body | properties set, a task line ticked or re-dated, a section replaced |
| `rewrite` | note, heading, body | the note's text or one section replaced; the properties stay, and the previous version goes to `.trash` |
| `log` | text | a line in the day's diary note |
| `grocery` | body, done | lines on the grocery page unticked or ticked (see below) |
| `inbox` | text | a line in the inbox note |

`move` never reaches the writer: the pipeline turns it into the writes that do it (below). `search`,
`agenda`, `link_answer`, `reminder`, `reminder_list` and `reminder_cancel` write nothing of their
own: the pipeline answers them. An `append`, `update` or `rewrite` whose note has gone, or that
ends up changing nothing, becomes an inbox line.

A new note takes its folder's shape from the notes already there, never from a model
(`VaultWriter._shaped`): the properties most of them carry, in the order the newest has, a real
tick box where every sibling has a boolean, and a link where most of them keep that property as
one. So a base over the folder shows real check boxes.

**Editing any place in a note** (`app/llm/edits.py`, `app/vault/mdedit.py`): the note, or the
section named, is read once without its frontmatter and shown to the model as numbered lines, and
the model answers with a short list of operations against those numbers (`replace`, `insert`,
`delete`, `image`), or with a whole new text in `full` when the instruction really is "rewrite all
of this". One call decides which. An edit script costs a couple of hundred output tokens instead
of the note again, and needs no special case for pictures: a picture is a line. An `image` edit
adds a `![caption](url)` line, and removing a picture removes its line, never the file.

`mdedit.apply_edits` resolves every edit against the numbering it was given, so several edits never
shift each other, and ignores a line number that is not there: the model can only point at what it
was shown. A note past `MAX_INPUT` characters is refused rather than cut (`texts.VAULT_TOO_LONG`):
a whole text written from the top of a note would delete the rest unread. A heading that is no
longer there widens the change to the whole note: the user asked for the note to change, and the
section was only how they pointed.

**Rewriting what is already there** (`rewrite`): the user names a note, optionally a section, and
says what to change in their own words («оставь один подход и одни размеры»). The current text is
read at execution time — the readers never see a note's contents — and one Sonnet call (the editor
above) returns edits or the whole new text. `app/llm/rewrite.py` does the same job in whole texts
when no editor is wired. The previous version goes to `.trash`, where it outlives the undo window:
this is the one write that can lose something the user spent an evening on.

**Moving between notes** (`move`): the filer names the source, the destination
and what to move; the editor marks lines with `delete` ops, and only a plan made of nothing
but deletes counts. The code copies exactly those lines to the destination (appended, or a
new note next to the source), then cuts them from the source — destination first, so a
failure halfway leaves a copy, never a loss. The model points; it never writes the moved text.

**Groceries** (`app/vault/groceries.py`, the page named by `texts.VAULT_GROCERIES_NOTE`): a
registry rather than a to-do list. One permanent line per product, ticked when it is in stock
and unticked when it has to be bought, so nothing is ever created or archived and the page's
size tracks the grocery vocabulary instead of the number of shopping trips. The home page
finds the unticked ones with the Tasks plugin's `filename includes`, which is why the lines
carry no tag — the file is the identity, and the same query keys the undated-tasks view's
`filename does not include` exclusion.

The rule it rests on is a lookup and not a judgement: `filer.check` reads the page, and a
product already on it is filed there whatever the model answered. The model is consulted only
about a word the page has never seen, and correcting it means editing one line of markdown —
which is also how the user teaches it. Two invariants are enforced in code: a grocery line
never carries a date or a repeat rule (a recurring tick copies itself on completion, which is
what filled the archive note with duplicates), and an ambiguous name adds a line rather than
unticking a guess, because a wrong untick puts the wrong thing in the shop while a
near-duplicate takes seconds to merge. Groceries are left out of the task listings — a
shopping list is not a thing to do today — and reduced to one line in the morning digest.

## 10. Speech

`app/speech/whisper_local.py:WhisperLocal`, faster-whisper, model `WHISPER_MODEL` (default
`large-v3-turbo`), `compute_type=int8`, `device=auto` (CUDA if available, else CPU), `language=ru`
hint (configurable), `vad_filter=True`. Transcript is stored in audit only; the reply describes
what was actually written, never the transcript (FLOWS.md F13, ERRORS.md `STT_EMPTY`/`STT_FAILED`).

* **Lazy load.** The model is *not* loaded at startup (`app.main.post_init_checks` deliberately
  never touches `app.speech`) — only on the first voice message, inside the worker thread
  described below, and then kept for the life of the process. A user who never sends a voice
  message never pays the load cost at all.
* **One `threading.Lock` serialises every call**, not just the load. `transcribe()` runs the
  whole thing (`_transcribe_sync`) via `asyncio.to_thread`, so it executes on a worker thread, not
  the event loop — a plain `asyncio.Lock` would not help there. The lock covers the load *and*
  the decode because one GPU cannot decode two clips at once anyway, and narrowing it to just the
  load would reopen two races: two concurrent calls disagreeing about which device is current
  mid-fallback, and a fallback on one call racing an in-flight transcription on another.
* **CUDA → CPU fallback, remembered for the process.** A failure — whether raised while loading
  the model or only while decoding the first segments — logs one WARNING naming the original
  error, flips the instance to CPU, and retries once; a second failure raises `SpeechError`
  (`STT_FAILED`). Once CPU has been taken, it is permanent for that `WhisperLocal` instance: every
  later call goes straight to CPU and never touches CUDA again, even if the earlier failure was a
  fluke. There is no way to force a retry on CUDA short of restarting the process.

GPU on Windows needs cuBLAS + cuDNN 9 for CUDA 12 (install via `nvidia-cublas-cu12`,
`nvidia-cudnn-cu12` wheels, add their `bin` to PATH); CPU fallback is automatic if CUDA libs are
missing, per the paragraph above.

## 11. Audit and storage (SQLite)

Tables: `events` (one row per handled message, per spec §27), `executions` (undo data, expires),
`reminders` and `reminder_fired` (§14a), `chat_memory` (§7). `sessions` is still in the schema and
is no longer used. See [DATA_MODEL.md](DATA_MODEL.md). Tokens never stored; each model call's
kind, model, token counts and error are stored as JSON text in `llm_response`, and the pages read
from the message's links in `llm_context`.

Exactly one `events` row per handled message: opened at entry and closed once on every path, error
exits included. A voice note that could not be transcribed is the exception: the handler answers
it before the orchestrator is called, so it has no row. `events.decision` says how the turn went
(a JSON `kind`: `VAULT`, `ABOUT`, `FIX`, `FIX_ASK`, `UNDO`, `NOT_LISTED`, `NOT_YOURS`, `ERROR`).
`events.error` holds the code the turn earned for itself (ERRORS.md); a vault failure that still
produced a reply line is not one of those, and shows as the `error` of its call in `llm_response`.

`executions.reply_message_id` starts null — the row is written before its reply exists — and the
transport fills it in afterwards with `AuditStore.set_reply_message_id`, so that a Telegram reply
to that message can be recognised as a fix of that write (`AuditStore.execution_by_reply`).

## 12. Admin page

`app/admin/server.py`: a stdlib `http.server.ThreadingHTTPServer` on a daemon thread, exactly
three routes, no framework, no auth beyond the loopback guard below:

| Route | Does |
|---|---|
| `GET /` | Serves `page.html` (read once at import, from disk). |
| `GET /api/settings` | Everything the page shows as one JSON document: switches, the vault's path, folders and tags, mail buckets, tuning, chats, reminder rules and pending reminders, sources and the Google status, watched folders and their status, Syncthing's status. |
| `POST /api/settings` | Saves the parts of the document that were posted: each is validated by the store that owns it (a bad part → 400) and returns how many fields changed. Files are re-read per message or per run, so no restart is needed. |

The page's sections: **Что включено** (the switches), **Общее** (the bot's name, the agenda's times,
the never-open sites, the web words, the research note), **Obsidian** (the countdown tag, the date
properties, the linker note), **Почта**, **Чаты**, **Напоминания**, **Источники**, **Syncthing**,
**Папки**. Where the vault is filed is not here: the rules live in `_bot.md`, inside the vault.

**Host loopback guard.** Every request — `GET` and `POST` alike — is rejected with `403
forbidden_host` before anything else runs unless its `Host` header names `127.0.0.1`, `localhost`
or `::1` (`_loopback_host`). This is a DNS-rebinding guard, not redundant with binding the socket
to `127.0.0.1`: binding keeps other *machines* out, but a page open in the user's own browser,
navigated to a hostile domain that resolves to `127.0.0.1` via DNS rebinding, would otherwise
still reach a server merely bound to loopback — the `Host` header is what a same-origin browser
request cannot forge to say anything else. `MAX_BODY_BYTES` (512 KiB) similarly bounds
`POST`'s `Content-Length` before `rfile.read()` ever runs, so a hostile/huge header cannot force
an unbounded blocking read.

**Own-page guard on `POST`.** The `Host` header stops rebinding; it does not stop a page on
another site, open in the user's browser, from making that browser POST here — the socket is
loopback either way. So `POST /api/settings` also requires `Content-Type: application/json`
(a foreign origin can only send that after a CORS preflight this server never answers) and
refuses an `Origin` header that is not loopback (`403 forbidden_origin`, `_own_page`).

## 13. Model selection

Every model is Claude except a local mail model. Each job has its own setting, so a job can move
to a cheaper or a stronger model without touching the others:

| Job | Setting | Default | Why |
|---|---|---|---|
| Reading a message, the plan filter | `FILER_MODEL` | `claude-haiku-4-5` | A small job on a small context, on every message. |
| Listing a set, planning steps, the second reading | `FILER_STRONG_MODEL` (planning and lists fall back to `REWRITE_MODEL`) | `claude-sonnet-5` | Haiku lists 8 of a writer's novels, one of them wrong. |
| Links between notes | `LINKER_MODEL` | `claude-haiku-4-5` | A small job on a small context. |
| Answering from what the search found | `VAULT_ANSWER_MODEL` | `claude-haiku-4-5` | Empty: the reply is the list of notes found. |
| The second opinion | `ESCALATE_MODEL` | `claude-sonnet-5` | Read again when the answerer cannot tell who asks, or finds nothing on a question tied to the owner's own folder or tag; also checks a watched folder's summary before it is sent. Empty: never. |
| A question about a linked page | `CLAUDE_MODEL` | `claude-haiku-4-5` | Also reads watched folders when `VAULT_ANSWER_MODEL` is empty. |
| Changing a note | `REWRITE_MODEL` | `claude-sonnet-5` | It is the user's own writing, and Haiku loses facts while shortening. |
| A joke for a group that calls the bot for nothing | `BANTER_MODEL` | `claude-sonnet-5` | One short line; thinking off. Empty: stock lines only. |
| Web research | `RESEARCH_MODEL` | `claude-sonnet-5` | A search already costs far more than a message, and this is the call whose quality you read. |
| Sorting mail | `MAIL_MODEL` | `claude-haiku-4-5` | A `claude-...` name goes through the API; any other name is a local Ollama model, which needs no key. |

A local mail model is the only thing Ollama is for. `LLM_NUM_CTX=8192` is the context window it
gets: 16384 costs ~1.6 GB more KV cache, which on an 8 GB card pushed even `qwen3:8b` into a 20% CPU
offload and ~35% more latency for no measurable accuracy gain, and a request that overflows 8k is
split and retried. Because a local model leaves little VRAM spare, `.env.example` ships
`WHISPER_DEVICE=cpu`. `MAIL_SHADOW_MODEL` is a trial: each model listed sorts the same mail again
and sends its own digest after the real one, so they can be read side by side.

`tools/benchmark_filer.py` scores models on the vault reader's frozen case file (Ollama models by
name, Claude by API id), `tools/vault_cases.py` builds such a file from messages the bot actually
handled, and `tools/regressions.py` runs the real messages the bot once got wrong through the real
pipeline before a release.

## 14. Security boundaries

- LLM receives: the message, the user's guide note, names (folders, tags, the notes the message may
  mean, open tasks that share a word with it, products), the shape of those notes, the chat's
  recent exchanges, the pages behind the message's links, and the time. A note's text only when
  the user asked to change it or a question's search found it. Never tokens or paths.
- LLM output is data, never trusted as an address: every note, folder and date in it is re-checked
  against the index before anything is written (§8). `tests/test_security.py` exercises this
  against the real orchestrator with fakes, not a mock that only proves "was called".
- What a web search or a linked page returned is data, not instructions. Found text is written as
  it stands and never enters a reader's prompt; a linked page reaches the prompts under a header
  that says so.
- Vault files are written only by `VaultWriter`, which refuses a path outside the vault or inside
  `.obsidian/`; a group chat's writer refuses anything outside its scope (§14a).
- Telegram allowlist enforced before any processing; unknown users get no reply, no audit row, and
  trigger no provider call at all (`app/telegram/auth.py`).
- Admin page bound to loopback, no auth beyond the `Host`-header guard (§12) — host-local by
  design.
- Logging never carries a token, at any level: besides `httpx`/`httpcore` (which log Telegram's
  token-bearing request URL at INFO), `telegram.ext.ExtBot` logs the same URL once at DEBUG from
  its own constructor — a leak the httpx floor alone does not cover, since it isn't an httpx
  request. `app/logging_setup.py` floors both to WARNING unconditionally. A third leak needs more
  than a floor: `telegram.ext`'s polling retry loop logs a token-bearing `InvalidToken` at ERROR
  *with* its traceback when Telegram rejects the token, so `configure()` also wraps the handler in
  a redacting formatter that rewrites every occurrence of the secret values `main()` explicitly
  hands it (the Telegram token, the Anthropic key and the Gmail app password, never a
  `Settings`) to `***` — message, args and traceback alike. The
  same `InvalidToken` escaping `run_polling()` is caught in `main()` and reported as a config exit
  that names `TELEGRAM_BOT_TOKEN` and prints nothing of the exception. See its module docstring
  and `documentation/ERRORS.md`'s "Logging" section.

## 14a. Around the vault

**Web research** (`app/llm/research.py`, `app/web/`). A message that asks to find something is
looked up by a loop the bot drives. Claude gets three tools: `web_search` (Anthropic's, the
basic version without a code sandbox), `site_search` (a shop's or real-estate site's own search
page) and `read` (a page through Jina Reader, which gets past most Cloudflare challenges, with a
plain fetch as fallback — public addresses only, every redirect checked). Pages are cut to what
matters before the model sees them (`app/web/trim.py`; images become their alt text, so a shop
named only by its logo keeps its name).

Nothing local is built in. The site table is the owner's `data/sites.json`, loaded by
`app/web/sites.py:load` — a list of `{name, kind, template, rent_template, probe}`, where `kind`
is one of `prompts.SITE_KINDS` (`real_estate`, `groceries`, `building`, `goods`, `prices`),
`template` and `rent_template` contain `{q}`, and an empty `template` marks a site worth naming
that has no direct search. The prompt lists the sites by kind (`prompts.site_guide`), and
`site_search` is offered only when at least one site has a template; no file means no
`site_search` tool at all. Where the user lives, which sources to prefer and which currency to
quote come from the admin page's «Дополнение к промпту поиска» (`tuning.research_note`), appended
to the research prompt as written. The budget is the code's, not the model's:
`RESEARCH_MAX_SEARCHES` searches and `RESEARCH_MAX_READS` site searches and reads, and after
`RESEARCH_SOFT_DEADLINE_S` the tools are switched off and the model answers with what it has;
`RESEARCH_DEADLINE_S` is a backstop. It replaced Claude's server-side tool loop, which once
spent 20 minutes on a listing it had already read in the first 40 seconds; on the same
eight questions the loop costs about an eighth and answers at least as well.
`tools/check_sites.py --sites data/sites.json` checks the site table live;
`tools/benchmark_research.py` measures a change on the owner's own questions, read from the
git-ignored `data/eval/web/questions.json`.

**Links as data** (`app/web/links.py`, `app/llm/answer.py`). Before a message is read,
`LinkReader` reads up to three of its links in parallel through the research
`Reader`, with a 12-second deadline. Each page is cut by `trim.page_facts` to its share of
6 000 characters. The cut is not `relevant`'s: the page is the subject, and the message may
share no word with it. So table rows, prices, sizes and headings come first wherever they sit,
then prose; menus and other listings are dropped. A link whose host is on the tuning list `never_open` (admin
page; defaults to `NEVER_OPEN_DEFAULT`: Google Docs/Drive/Mail and Notion — banks and other
private sites are the owner's to add) is not
read. Nor is one past the deadline. Either way it stays a plain link with a reason.

The pages travel as `VaultContext.links` and are rendered by `prompts.links_section`, after the
user's message, under a header that calls them data and not instructions. Every reader gets them:
the single `Filer` and every `StagedFiler` stage. Each uses them for the action it picks: fields, a
real title, a summary.

A question about a page is a `link_answer` action: the staged reader's question stage returns one
when at least one page was read, and the pipeline has `LinkAnswerer` (`CLAUDE_MODEL`, no tools)
reply from the pages. Nothing is written. A message in a group with its own scope goes to the
vault without its links read.

**When Claude cannot be used** (`app/llm/health.py`): one `Health` instance is shared by
every component that calls Anthropic. It names the reason behind a failure — no credit,
a rejected key, a rate limit, an outage — keeps it until a call succeeds again, and
hands out one plain sentence about it at most once an hour, appended to the reply and to
either digest. A failure older than five minutes is checked with a one-token call before it is
reported again, so a reply that made no call of its own cannot repeat what has since been fixed.
The Obsidian line says the cause in the user's own language instead of `claude 400`. There is no
fallback model: the message is kept as it is in the inbox note (§5).

**The morning digest** (`app/daily.py`): one asyncio task sleeps until `DAILY_DIGEST_AT` in
`TIMEZONE`, asks the vault for overdue / today / tomorrow plus dated notes, and sends it to the
allowed chats. Nothing due means nothing sent. A restart within two hours of the time still
sends that day's. It reads the vault — that is where the dates live.

**The bot's name** (`app/address.py`, `BOT_NAME`): recognised deterministically, never by a
model. The name is stripped from the start or the end of a message before the vault reads
it, so "Robo, buy milk" is a shopping item; anywhere else in the sentence it is ordinary words
and stays. The name alone answers `texts.CALLED`, and a short question about the bot answers
`texts.ABOUT` — both without a model call and without writing anything.

**Tuning** (`app/tuning.py`, `data/tuning.json`, edited on the admin page): the bot's name, the
agenda's, mail digest's and watched folders' times, the words that may start a web search, the
never-open sites, the vault's countdown tag and date properties, how long a Syncthing device may be
away, and two "extra instructions" fields appended to the research and linker prompts.
Every field falls back to `.env` and then to the code, is re-read when the file changes, and
takes effect on the next message — no restart, no release. The system prompts themselves stay
in code: they are long, interdependent and covered by tests, and the working pattern is a small
structured text the prompt *reads* (`_bot.md`, the mail buckets).

**Switches** (`app/switches.py`, `data/switches.json`, edited on the admin page): `obsidian`,
`linker`, `mail`, `syncthing`, `folders`. `.env` says what `obsidian` and `linker` are until the
file exists; the file then wins, and each is read per message or per run, so a change needs no
restart. With `obsidian` off a message is refused (`NOTHING_ENABLED`) rather than swallowed. A
switch only turns off what was built: `OBSIDIAN_ENABLED=false` at startup, or no vault or no key,
builds no pipeline at all, the page cannot bring one up, and the refusal says so
(`VAULT_NOT_SET`).

**Per-chat scopes** (`app/chats.py`, `app/vault/scope.py`). An owner's private chat has full
access. A group is answered only when listed in `data/chats.json` (the admin page's «Чаты»,
which also offers the groups the bot has seen) and only when addressed — @mention, reply to the
bot, its name, a command; anything else in the group costs nothing. A listed group's scope is
one tag plus a home folder, and its rights (`ask`, `add`, `tasks`, `tick`, `change`, `move`,
`remind`, `media`, `banter`).
Its message goes through `VaultPipeline.scoped(scope)`, which rebuilds three
layers per turn: `ScopedIndex` (every reader — staged reader, `check()`, search, answerer,
agenda, Omnisearch — sees only notes whose *frontmatter* carries the tag or that lie in the
home folder, and the tasks note
with only the lines carrying `#tag`); `gate()` after `check()` (rights per action kind; tasks
always get the tag; new notes go to the home folder, renamed when an owner's note has the
title; diary, groceries and inbox never); and a guarded writer that refuses any other file. No
linker, no inbox fallback, no `obsidian://` links in replies. Undo and «Поправить» in a group
belong to the member who asked (`events.telegram_user_id`) or an owner; after Fix is pressed,
only the presser's next message is the correction.

**Jokes** (`app/llm/banter.py`). In a group with the `banter` right, a mention with nothing to ask
(a bare mention, the name alone, or what the reader calls `unclear`) gets a joke instead of «не
понял»: the message and the recent exchanges, no notes, in the tone of the admin page's
`banter_note`, written nowhere. Only the reader's own verdict counts, so a request that could not
be placed still says so; when Claude cannot be asked it is a stock line (`texts.BANTER_LINES`).

**Instagram links** (`app/media/instagram.py`, `app/telegram/media.py`). In a group with the
`media` right, a message with an Instagram post link that is *not* addressed to the bot gets
the post back as a reply: a video, a photo, or an album of a carousel with the caption under
the first item. Its own handler group and `block=False`, so a download never holds up the
conversation. yt-dlp asks Instagram first (no login, no ffmpeg: a progressive format); when
Instagram refuses, `INSTAGRAM_MIRRORS` are tried in order — URL templates, `{n}` for a numbered
carousel item (a repeated file ends it). Nothing fetched, or Telegram refusing the upload
(50 MB a video, 10 MB a photo): nothing is said in the chat, one INFO line in the log. In an
owner's private chat a message of *nothing but* post links is the same "show me" — the text
handler never sees it (`private_links` is excluded from its filter), and a post not fetched is
said there (`INSTAGRAM_NOT_FETCHED`); a link with words around it is a request as before.

**Reminders** (`app/reminders/`).
«напомни …» is the staged reader's `remind` intent: `REMIND_PROMPT` returns add/list/cancel, and
an add becomes a `reminder` action plus a `task` with the same words, date, time and 🔁. The
pipeline (`_reminded`) completes the moment (`timing.first_due`: 09:00 without a time, a time
alone today or tomorrow, a repeat rule's next occurrence), stores it (`ReminderStore`, table
`reminders`, migration 0002), dates the task, and refuses a moment already past or one with no
time at all. `UndoRecord.reminders`: Undo cancels them. `ReminderScheduler` sleeps until the
soonest due (≤ 60 s), sends «⏰ …» to that chat — late ones say how late, a repeating one missed
several times is sent once and moves on, a chat Telegram refuses cancels it — and every 10
minutes runs the admin page's rules (`data/reminder_rules.json`: N days before notes with a date
property or tasks with a tag, to a chat), each item once per due moment (`reminder_fired`),
reading a group's vault through its scope. Groups need the `remind` right.

**Google sources** (`app/google/`, the admin page's «Источники», `data/sources.json`). A
question may also be answered from Google Docs, Sheets, Forms and folders,
read-only. Each source carries the chats that may use it, how a document is cut into sections
(`split`: `pieces` or `headings`) and the owner's instruction «Как читать» — what the document
holds, in their own words. All of that is the owner's configuration: `app/google/sources.py`
only carries it to the model, and no document's meaning is written in code. The owner's private
chat uses every source and may search the rest of the Drive; a group uses only the sources that
list it. Copies are kept as sections in `data/sources/<file id>.json` and re-read only when
Drive says the file changed. Access is the owner's own OAuth client (`data/google_client.json`,
type "Desktop app") and sign-in (`tools/google_login.py` → `data/google_token.json`); without
either file the Google side is off.

**Watchers** (`app/watch/`). `syncthing.py` reads Syncthing's REST API on this PC (GET only;
address and API key from its `config.xml`, this PC's own device left out through
`/rest/system/status`) every 5 minutes (`SYNCTHING_EVERY_S`, a `Sweeper` loop) and
`SyncthingHealth` turns observations into one message when a problem starts and one when it
ends: the PC's Syncthing not answering, a device disconnected for `Tuning.sync_after` minutes as
the bot itself observed it (Syncthing's last-seen would make a PC waking from sleep report a
phone that was there), a folder with errors; paused ones never. What was told is kept in
`data/syncthing_state.json`. `folders.py`: the admin page's «Папки» (`data/folders.json`: path,
pattern, the owner's instruction) read by a `DailyMessage` at `Tuning.mail_at`. A folder just
added takes a baseline; afterwards a new file that has not changed for 15 minutes is read and
sent with the instruction to the model (structured `{attention, text}`), and is reported once
— only after the model answered, so a failed call leaves it for next time
(`data/folders_state.json`). No folder's meaning is in code. Both have a switch on the admin
page and go to the owners' private chats only.

## 15. Releases and migrations

`pyproject.toml`'s `[project].version` is the version source (`app/version.py`); `release.py` bumps it, commits, tags, and builds `dist/ai_assistant-X.Y.Z.zip` (app files + `VERSION` + `manifest.json` with a lock hash). Production is a separate directory (e.g. `C:\apps\ai_assistant`) with its own `.env`/`.venv`/`data`, populated only from a release zip via `deploy/install.ps1` or `deploy/update.ps1` (`apply_update.py`), never `git clone`; `uv sync --frozen --no-dev` (re-run on updates only when the lock hash changed) provisions the venv. Schema migrations (`migrations/NNNN_name.sql`, journaled in `schema_migrations`) are applied only by the installer/updater or `tools/migrate.py` — never implicitly. At startup, `AuditStore.assert_schema_current()` verifies no migration is pending and refuses to run otherwise. See [RELEASE.md](../RELEASE.md) for the full process.
