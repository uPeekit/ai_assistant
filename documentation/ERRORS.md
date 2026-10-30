# Errors and Recovery

Principle: a message is never lost and a turn never fails outright. What the vault cannot place becomes a line in the inbox note, what it cannot do is one line in the reply, and only what nobody expected answers `INTERNAL`.

## Classes

| Code | Where | Cause | User message (ru) | Recovery |
|---|---|---|---|---|
| `AUTH_DENIED` | telegram/auth | user id not allowlisted | none | log WARNING, ignore |
| `STT_EMPTY` | telegram/handlers | no speech detected | Не разобрал голосовое сообщение. Повторите или напишите текстом. | none; no `events` row, the handler answers before the orchestrator |
| `STT_FAILED` | telegram/handlers | model load / decode exception | Ошибка распознавания речи. | no `events` row; if CUDA error → auto-switch to CPU for process lifetime |
| `NOTHING_ENABLED` | conversation/orchestrator | the `obsidian` switch is off, or no vault was built: `OBSIDIAN_VAULT` empty or not a folder, `OBSIDIAN_ENABLED=false`, no `ANTHROPIC_API_KEY` | Obsidian выключен — включите его на странице настроек. | fix the setting; a restart is needed for anything but the switch |
| `UNDO_EXPIRED` | conversation/orchestrator | undo window passed, already undone, another chat's write, or an older version's record with nothing to take back | Отменить уже нельзя (прошло больше N минут). | none |
| `UNDO_FAILED` | conversation/orchestrator | the vault undo raised | Не удалось отменить: <exception class>. | audit; the row stays, so Undo can be tried again |
| `FIX_EXPIRED` | conversation/orchestrator | Fix pressed or a correction sent after the undo window, or on a write that cannot be fixed | Поправить уже нельзя (прошло больше N минут). | none; lives in `texts.FIX_EXPIRED`, not in `ERRORS` |
| `BUTTON_EXPIRED` | conversation/orchestrator | a button from an older version of the bot, which nothing answers any more | Эта кнопка больше не работает. | none |
| `LINK_ANSWER_FAILED` | vault/pipeline | the answer to a question about a linked page failed | Не получилось ответить по ссылке — попробуйте спросить ещё раз. | log WARNING; not an `events.error` value |
| `INTERNAL` | conversation/orchestrator, telegram/handlers | unexpected exception on any path (the orchestrator never raises to the transport), or the audit store failing | Не удалось обработать сообщение. | audit error, log exception |
| `CONFIG_INVALID` | config | missing env / bad value | process exits with message | fix `.env` |

Placeholders in `texts.ERRORS` are filled by the orchestrator, which degrades to `INTERNAL` for any
template it cannot fill, so a template may only ask for a value its caller actually has.
`test_texts.py` pins the placeholder set of every code against that.

## Lines in a vault reply

A vault turn that went partly or wholly wrong is not an error code: `events.error` stays empty and
the failure is the `error` of its call in `events.llm_response`. The reply says it in one line.
Nothing written: `⚠️ Obsidian — не записано: <line>`. Something written: the usual
`✅ Obsidian — …` with the line after it, because those files are on disk and Undo reaches them.

| Line | Source | Cause | Recovery |
|---|---|---|---|
| the reason in words («закончились кредиты Claude») | `texts.LLM_DOWN_SHORT` | Claude could not be used | the message is kept in the inbox note; `texts.LLM_DOWN_NOTE` follows at most once an hour |
| the exception class | `VaultPipeline._write_all` | a file could not be read or written | the other writes stand, with `ещё N записать не удалось` (`texts.VAULT_SOME_FAILED`) |
| не нашёл, что именно поправить | `texts.VAULT_NOTHING_TO_CHANGE` | the model read the note and found nothing the instruction applies to | nothing written |
| некому переписать — нужен Claude | `texts.VAULT_REWRITE_OFF` | no editor and no rewriter | none |
| заметка слишком длинная, чтобы менять её целиком — назовите раздел | `texts.VAULT_TOO_LONG` | the note or section is past `MAX_INPUT` characters; it is never cut and written back over the whole | nothing written; name a section instead |
| не нашёл, что переносить — ничего не тронул | `texts.VAULT_NOTHING_TO_MOVE` | nothing in the note matches what was to move | nothing written |
| не понял, какие строки переносить — ничего не тронул | `texts.VAULT_MOVE_UNCLEAR` | the editor did not answer with deletes alone | nothing written |
| поиск в интернете не подключён, записал без него | `texts.VAULT_WEB_OFF` | no researcher | the note is written without it |
| в интернете ничего не нашлось, записал без найденного | `texts.VAULT_WEB_FAILED` | the search failed | the note is written without it |
| поиску нужен запрос точнее, записал без найденного | `texts.VAULT_WEB_UNCLEAR` | the researcher wanted to ask first | the note is written without it |
| «…» в `↩️ Не отменил в «…»` | `texts.VAULT_UNDO_LEFT` | the lines an Undo or a Fix would take back were changed since | that file is left alone |
| не понял, что поправить — ничего не менял | `texts.FIX_UNCLEAR` | the correction could not be read | the write and its row stay as they were |

## Inbox note

Rather than dropping a message the pipeline could not place, the vault appends it to the inbox note
(`texts.VAULT_INBOX_NOTE`). It takes no model and no setting. What falls back: an action the reader
marked unclear, a reader that answered nothing usable, an action naming a note that is not there
(`filer.check`, or the writer when the note has gone since), a message the model could not be asked
about at all (kind `kept`), and, once, the whole message of a plan whose write step failed. Explicitly
not: a group chat, which has no inbox and is told what it may not do here. See ARCHITECTURE.md §4/§5.

## Startup checks (`main.py`)

Steps 1-2 are synchronous and run in `main()` before any event loop exists. Steps 3-6 are
async and run from `Application.post_init` — i.e. inside python-telegram-bot's own event loop,
the same one that later polls — never from a throwaway `asyncio.run(...)` of their own: a second,
separate loop would leave httpx-backed clients holding idle keep-alive
connections bound to it, and the first real call on them once polling starts would raise
`RuntimeError: Event loop is closed`.

0. `load_settings()`; a value pydantic refuses → exit 2, naming the field and never the value.
   Then `logging_setup.configure(LOG_LEVEL, redact=(Telegram token, Anthropic key, Gmail app
   password), log_file=LOG_FILE)`; an unknown `LOG_LEVEL` → exit 2 naming the valid levels (the name
   is normalised to upper case first, so `LOG_LEVEL=info` is accepted rather than crashing on
   `Logger.setLevel`).
0a. `InstanceLock(<db dir>/bot.pid).acquire()`; another process already holding it → exit 5. One
   bot per database: two pollers on one token get Telegram `409 Conflict`s and race each other's
   writes. The lock is an OS byte-range lock (released by the OS when the process dies, crash
   included), not the file's existence — a `bot.pid` left behind never blocks a restart.
1. Settings check (`Settings.require_telegram()`); missing required → exit 2.
2. `AuditStore.assert_schema_current()`; a pending migration → exit 4 with the hint to run the
   updater. Migrations are never applied here — never `AuditStore.migrate()` — only by the
   installer/updater or `tools/migrate.py` (ARCHITECTURE.md §15).
3. No `ANTHROPIC_API_KEY`: warns that the vault, web research and link answers are off — not fatal.
   An `OBSIDIAN_VAULT` that is not a folder warned already, while the vault was being built.
4. Whisper not loaded at startup (lazy).
5. `set_my_commands` publishes the "/" command menu, for private chats only (groups get none);
   a failure only warns (and the warning names the exception class only — an `InvalidToken`
   raised here carries the bot token in its text).
6. Admin server start (if `ADMIN_UI_PORT > 0`); an `OSError` binding the port (already in use, or
   a Windows excluded port range — `WinError 10013` on 8787 is common) only warns and startup
   continues. The page is optional by design; losing it must not lose the bot.
7. Telegram polling start. If Telegram rejects the bot token, python-telegram-bot raises
   `InvalidToken` out of `run_polling()` → exit 2 naming `TELEGRAM_BOT_TOKEN`. Neither that
   exception's message nor its traceback is ever printed or logged: PTB builds it as
   ``InvalidToken(f"The token `{token}` was rejected by the server.")``. PTB's own ERROR line
   ("Invalid token. Aborting retry loop.") is kept, but the formatter drops its ~40-line
   traceback — a rejected token is a config mistake, not a crash.

Exit codes: `0` stopped normally, `2` configuration (`.env`, or a token Telegram rejected), `4`
pending migration, `5` already running. `deploy/start.ps1` (behind `start.cmd`)
turns each into one plain-English line and, for `2`, offers to open `.env`; for `4` it offers
to run `tools.migrate --apply` on an explicit yes — still never implicitly.
`tests/test_deploy_scripts.py` fails if `main.py` gains an `EXIT_*` the launcher doesn't explain.

## Logging

Structured `logging` with `event_id` in every record after an event is created. Never log tokens,
never log message text above DEBUG (`tests/test_security.py` enforces exactly that: over a full
text → write → undo flow, no line above `DEBUG` may contain the message text). DEBUG may log
LLM context and response.

`app.logging_setup.configure()` also floors two third-party loggers to WARNING no matter what
`LOG_LEVEL` is set to, because each would otherwise print the Telegram bot token to stderr on its
own, with no app code calling `log.*` on it: `httpx`/`httpcore` (python-telegram-bot's own HTTP
client logs every request's full URL, token and all, at INFO) and `telegram.ext.ExtBot` (logs the
same token-bearing URL once at DEBUG, from its own constructor — not an HTTP request, so the
httpx floor does nothing for it).

Records also go to `LOG_FILE` (default `logs/bot.log`, rotated at 5 MB × 5; empty disables it)
through the same filter and the same redacting formatter as stderr — a file is where a leaked
token would outlive the console window.

Neither floor can help with the third leak, so `configure()` also installs a redacting formatter
over the stderr handler (and the file handler): every occurrence of a secret *value* it was explicitly given — `main()`
passes the Telegram token, the Anthropic key and the Gmail app password, and nothing else; `configure` never sees a `Settings`
— becomes `***` in the formatted record, message, interpolated args and traceback alike. The leak
it exists for is `telegram.ext`'s polling retry loop
(`telegram/ext/_utils/networkloop.py`), which logs the token-bearing `InvalidToken` at ERROR
*with* `exc_info` when Telegram rejects the token — an ERROR on a logger the app genuinely wants
to hear from, so no level floor could have stopped it. See `app/logging_setup.py`'s module
docstring.
