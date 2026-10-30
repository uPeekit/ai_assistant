# Data Model

## 1. Vault actions (`app/vault/writer.py`)

What a reader produces and the writer carries out. `filer.check` builds these from the model's raw
answer; no field reaches the writer unchecked (ARCHITECTURE.md §8).

```python
class VaultAction(BaseModel):           # extra="forbid"
    action: str                         # task | note | append | update | rewrite | move | log |
                                        # grocery | inbox, and what writes nothing: search |
                                        # agenda | link_answer | reminder | reminder_list |
                                        # reminder_cancel
    text: str = ""                      # the task's, log line's or inbox line's words
    note: str = ""                      # which note to add to or change
    to: str = ""                        # move only: the note the lines go to
    folder: str = ""
    title: str = ""
    heading: str = ""
    body: list[str] = []
    props: dict[str, str] = {}          # "" clears the property
    tags: list[str] = []
    due: str = ""                       # ISO date
    time: str = ""                      # "HH:MM", written in front of a task's words
    reminder: int = 0                   # reminder_cancel: 1-based, as listed to the model
    repeat: str = ""                    # "every ...", the Tasks plugin's words
    countdown: bool = False
    done: bool | None = None
    task: str = ""
    due_from: str = ""                  # a question about dates: the day or range to look at
    due_to: str = ""
    scope: str = ""                     # day | now | overdue | list (a grocery question)
    research: str = ""                  # the body is to be looked up: what to search for
    media: str = ""                     # text | text_and_images | images
```

`research` and `media` never reach the writer: the pipeline fills `body` in first. What the writer
does with each action is in ARCHITECTURE.md §9.

`VaultWrite` is what a write reports: `kind` (the action, or `kept`, `grocery_done`,
`grocery_none`), `path`, `note`, `detail` (`app/vault/summary.py`) and its `undo`.

## 2. Plans (`app/vault/planner.py`, in memory)

```python
@dataclass(frozen=True)
class Step:
    id: int
    kind: Literal["write", "ask", "research", "derive", "read"]
    text: str                           # a self-contained request
    needs: tuple[int, ...] = ()         # the steps it waits for and reads
    reply: bool = False                 # this step is the chat answer

@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]             # 2 to 8; a need is always a step written earlier
```

`plan_from` refuses anything else: an unknown kind, a repeated id, more than three `research`
steps, a `derive` that needs nothing. A refused plan means the message goes the usual way.

## 3. Undo records

`VaultUndo` (`app/vault/writer.py`), one per write:

```python
class VaultUndo(BaseModel):             # extra="forbid"
    path: str
    previous: str | None = None         # None: the file did not exist before
    written: str | None = None          # the text the write left behind; None on a record made
                                         # before 0.6.18, which is undone the old way
    action: dict | None = None          # what the write did, as a VaultAction dump, so a later
                                         # message can fix part of it; None on old records
```

With both `previous` and `written`, undo removes that one change from whatever the file holds by
then (`app/vault/revert.py`) instead of putting the whole old text back over it.

`UndoRecord` (`app/conversation/undo.py`) is the JSON in `executions.undo`, one per turn:

```python
class UndoRecord(BaseModel):            # extra="ignore"
    kind: str = "vault"                 # "vault" for every row written now
    vault: list[VaultUndo] = []         # files the turn wrote, put back newest first
    reminders: list[int] = []           # reminders the turn set: Undo cancels them
    batch: list[UndoRecord] = []        # an old plan's writes, one record each
```

Rows written before the bot was vault-only hold other kinds of record. They still parse, because a
reply to one of those messages has to be read to be recognised as nothing to fix; their Notion
parts are ignored, and Undo of one answers `UNDO_EXPIRED`.

## 4. SQLite (`data/bot.sqlite`)

No column or table has been dropped: the schema below is what `migrations/` builds. Some of it
held the bot's Notion side and is no longer written. Old rows stay as they were.

```sql
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                     -- ISO UTC
  telegram_user_id INTEGER NOT NULL,    -- 0: an /undo with no sender
  chat_id INTEGER NOT NULL,
  message_id INTEGER,                   -- not written
  kind TEXT NOT NULL,                   -- text | voice | callback | undo
  raw_input TEXT,                       -- the text, the transcript, or the button's payload
  transcription TEXT,
  llm_model TEXT,                       -- the models used, "claude-haiku-4-5, claude-sonnet-5 x2"
  llm_context TEXT,                     -- JSON: the pages read from the message's links
  llm_response TEXT,                    -- JSON: one entry per model call
  interpretation TEXT,                  -- not written
  candidate_scores TEXT,                -- not written
  validation_result TEXT,               -- not written
  decision TEXT,                        -- JSON {"kind": ...}, see below
  clarification_state TEXT,             -- not written
  command TEXT,                         -- not written
  executed INTEGER NOT NULL DEFAULT 0,  -- not written
  notion_page_id TEXT,                  -- not written
  error TEXT,                           -- the code the turn earned (ERRORS.md)
  duration_ms INTEGER
);

CREATE TABLE sessions (                 -- not used: nothing reads or writes it
  chat_id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE executions (
  id INTEGER PRIMARY KEY,
  event_id INTEGER REFERENCES events(id),
  chat_id INTEGER NOT NULL,
  reply_message_id INTEGER,             -- null at insert (no reply yet); the transport fills it
                                        -- in via AuditStore.set_reply_message_id, so a Telegram
                                        -- reply to that message can be read as a fix of the write
  undo TEXT NOT NULL,                   -- JSON: UndoRecord
  undone INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT NOT NULL
);

CREATE TABLE reminders (                -- migration 0002, app/reminders/store.py
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  event_id INTEGER,
  text TEXT NOT NULL,
  due_at TEXT NOT NULL,                 -- UTC ISO; a repeating one moves to its next occurrence
  start_at TEXT NOT NULL,               -- its first local time
  repeat TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  sent_at TEXT,
  cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE reminder_fired (           -- what reminder rules already sent
  rule_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (rule_id, item_key, fire_at)
);

CREATE TABLE chat_memory (              -- migration 0003, app/conversation/memory.py
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,                   -- user | bot | answer
  text TEXT NOT NULL,
  at TEXT NOT NULL                      -- read back for 2 hours, then deleted
);
```

`events.decision` is a JSON object with a `kind`:

| `kind` | Means |
|---|---|
| `VAULT` | the message went to the vault |
| `ABOUT` | the bot was called by name, or asked what it is: answered from `texts.py` |
| `FIX` | a correction of a write, applied or refused |
| `FIX_ASK` | `[Поправить]` was pressed |
| `UNDO` | a write was taken back |
| `NOT_LISTED` | a group no longer listed: no reply |
| `NOT_YOURS` | a group member pressed a button on someone else's write |
| `ERROR` | with a `code`: the same value as `events.error` |
| `OPEN` | the placeholder from when the row was opened: a row that was never closed |

`executions` holds one row per turn that wrote something or set a reminder. `undone` is set by an
Undo that took anything back, and by a fix: the fixed row is done with, and a new row carries the
untouched writes and the new ones.

Secrets are never written.

## 5. Files in `data/`

Next to the database, gitignored. What the admin page edits is read per message or per run, so a
change there needs no restart.

| File | Written by | Holds |
|---|---|---|
| `bot.sqlite` | the bot | §4 |
| `bot.pid` | `app/instance_lock.py` | the one-bot-per-install lock (an OS byte-range lock, not the file's existence) |
| `switches.json` | admin page | the on/off switches |
| `tuning.json` | admin page | the bot's name, times, word lists, extra instructions |
| `mail_buckets.txt` | admin page | the mail digest's buckets |
| `chats.json` | admin page, the bot | listed groups and their scopes; groups seen |
| `reminder_rules.json` | admin page | reminder rules |
| `sources.json` | admin page | Google sources |
| `folders.json` | admin page | watched folders |
| `sites.json` | by hand | the owner's site table for web research |
| `google_client.json` | by hand | the owner's OAuth client |
| `google_token.json` | `tools/google_login.py` | the Google sign-in |
| `sources/<file id>.json` | the bot | copies of Google files, as sections |
| `askers.json` | the bot | the names a person goes by in the records |
| `mail_state.json`, `mail_digests.json` | the bot | what was digested; which letters each digest covered |
| `daily_state.json` | the bot | when each scheduled message last went out |
| `folders_state.json`, `syncthing_state.json` | the bot | what the watchers already reported |

Files an older version left there (`targets.yaml`, `workspace_note.md`) are no longer read.

## 6. Settings (`.env`)

Lines left in an existing `.env` for settings that no longer exist are ignored.

| Key | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | |
| `TELEGRAM_ALLOWED_USER_IDS` | required | comma-separated |
| `ANTHROPIC_API_KEY` | `` | every model but a local mail model; empty: the vault, web research and link answers stay off |
| `OBSIDIAN_VAULT` | `` | a folder on this machine; empty: off |
| `OBSIDIAN_ENABLED` | `true` | the `obsidian` switch until `switches.json` exists; `false` at startup builds no vault at all |
| `LINKER_ENABLED` | `true` | the `linker` switch until `switches.json` exists |
| `BOT_NAME` | `` | comma-separated spellings; the tuning file overrides it |
| `CLAUDE_MODEL` | `claude-haiku-4-5` | a question about a linked page; watched folders when `VAULT_ANSWER_MODEL` is empty |
| `FILER_MODEL` | `claude-haiku-4-5` | reads every message |
| `FILER_MODE` | `staged` | `single` is the older reader: no planner, no fix |
| `FILER_STRONG_MODEL` | `claude-sonnet-5` | lists, plans, the single reader's second reading; empty: lists and plans use `REWRITE_MODEL` |
| `LINKER_MODEL` | `claude-haiku-4-5` | |
| `VAULT_ANSWER_MODEL` | `claude-haiku-4-5` | empty: the reply is the list of notes found |
| `ESCALATE_MODEL` | `claude-sonnet-5` | the second opinion; empty: never asked |
| `REWRITE_MODEL` | `claude-sonnet-5` | changing a note |
| `BANTER_MODEL` | `claude-sonnet-5` | a joke in a group with the `banter` right; empty: stock lines |
| `RESEARCH_MODEL` | `claude-sonnet-5` | |
| `RESEARCH_MAX_SEARCHES` | `4` | 1 to 20 |
| `RESEARCH_MAX_READS` | `8` | site searches and page reads together, 1 to 40 |
| `RESEARCH_SOFT_DEADLINE_S` | `90` | the tools are switched off, the model answers with what it found |
| `RESEARCH_DEADLINE_S` | `180` | the backstop |
| `JINA_API_KEY` | `` | optional, raises Jina Reader's rate limit |
| `INSTAGRAM_MIRRORS` | two URL templates | tried in order when Instagram refuses; `{id}` the post's code, `{n}` a carousel item |
| `OMNISEARCH_PORT` | `51361` | Obsidian's Omnisearch plugin; `0`: never asked |
| `SYNCTHING_CONFIG` | `` | empty: `%LOCALAPPDATA%\Syncthing\config.xml` |
| `DAILY_DIGEST_AT` | `09:00` | HH:MM in `TIMEZONE`; empty: never; the tuning file overrides it |
| `GMAIL_ADDRESS` | `` | empty: no mail digest |
| `GMAIL_APP_PASSWORD` | `` | |
| `MAIL_DIGEST_AT` | `12:00,19:00` | comma-separated times; the tuning file overrides it |
| `MAIL_BUCKETS` | `` | the starting point until `mail_buckets.txt` exists; empty: the built-in list |
| `MAIL_MODEL` | `claude-haiku-4-5` | `claude-...` through the API; any other name is a local Ollama model |
| `MAIL_MAX_PER_RUN` | `40` | 1 to 200 |
| `MAIL_SHADOW_MODEL` | `` | a trial: Ollama models, comma-separated, that sort the same mail again |
| `MAIL_SHADOW_BATCH` | `20` | emails in one local request |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | for a local mail model; point at a remote host if you like |
| `LLM_NUM_CTX` | `8192` | the context window a local mail model gets; fits the KV cache in 8 GB VRAM |
| `WHISPER_MODEL` | `large-v3-turbo` | |
| `WHISPER_DEVICE` | `auto` | `cuda` / `cpu`; `.env.example` ships `cpu` — a local mail model leaves little VRAM |
| `WHISPER_COMPUTE_TYPE` | `int8` | |
| `WHISPER_LANGUAGE` | `ru` | empty = autodetect |
| `TIMEZONE` | `UTC` | set your own IANA zone, e.g. `Europe/Paris` |
| `LOCALE` | `ru` | |
| `DB_PATH` | `data/bot.sqlite` | |
| `ADMIN_UI_PORT` | `8787` | `0` disables |
| `UNDO_WINDOW_S` | `300` | |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FILE` | `logs/bot.log` | rotating 5 MB × 5, redacted like stderr; empty = console only |
