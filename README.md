# ai_assistant

A Telegram bot that turns a text or voice message into a change in your own notes —
"купи молоко" becomes a line on your shopping list, "в идеи: ..." becomes a paragraph on your
ideas page, and so on.

It writes to an **Obsidian vault**: a folder of markdown files on your machine. No plugin is
needed and Obsidian does not have to be running. It also answers questions from those notes, and
sends you the morning's agenda and your reminders.

Speech recognition (faster-whisper) always runs on your own machine. Reading your messages takes
Claude, through an Anthropic API key (see "Using Claude", below); there is no local model for
that. The bot does not stop to ask what you meant: anything it can't confidently place goes to an
inbox note, so that nothing is ever dropped into oblivion.

Design and internals live in [`documentation/`](documentation/) (start with
[ARCHITECTURE.md](documentation/ARCHITECTURE.md) and [FLOWS.md](documentation/FLOWS.md) if you
want to know *why* something works the way it does). This file is the operator's guide: install
it, configure it, run it, and know where to look when something goes wrong.

## What it understands

Send the bot a plain Russian sentence, as text or as a voice note — no command syntax, no fixed
phrasing:

- **"позвонить врачу завтра в 10"**, **"каждую среду выносить мусор"** → a task in your task note,
  with its date, time or repeat rule, in the format the Tasks plugin reads.
- **"купи молоко"** → the product on your grocery page, unticked, which means "buy it"; **"купил
  молоко"** ticks it back. The page keeps one permanent line per product.
- **"забрал посылку"**, **"отметь отчёт сделанным"** → finds the open task and ticks it.
- **"в идеи: попробовать сыр с плесенью"**, **"надо посмотреть фильм Uncharted"** → adds a line to
  a note, in the section it belongs to.
- **"добавь книгу Солярис, автор Лем"** → a new note in that folder, with the properties the notes
  there share.
- **"что у меня по дому?"**, **"что у меня в пятницу?"**, **"что мне сейчас делать?"** → an answer
  from your notes and dates; nothing is written.
- **"напомни завтра в 10 позвонить врачу"** → a reminder at that moment, and a task with the same
  words.
- **"в заметке про стенку оставь один подход"**, **"убери второй вариант"**, **"перенеси раздел про
  маты в заметку Спорт"** → changes that place in the note. Moved lines are copied word for word,
  and a rewritten note's old version stays in the vault's `.trash`.
- **"найди рецепт борща и запиши в заметки"**, **"найди визуальные референсы скамейки из дуба и
  сохрани в Проекты"** → searches the web, and writes a summary with its sources — and, when you
  ask for pictures, the images themselves — where you said. Needs Claude (see "Using Claude").
- Anything it can't place is saved as a line in the inbox note «Разное», so nothing you sent is
  ever silently lost.

Every write comes back with an **Undo** button that works for a few minutes
(`UNDO_WINDOW_S`, see below). Undo takes back that one message's change, not the whole file: a
note you added after it stays, and if you edited the very lines it wrote the bot leaves them
alone and says so. The bot never deletes or bulk-edits anything: a note it created goes to the
vault's `.trash`.

Next to Undo, a reply has **Поправить**: press it (or reply to the bot's message)
and say what is wrong — «не в задачи, а в продукты», «на пятницу», «кефир не надо», «и ещё
яйца». Only that part changes; usually one small model call, and Undo then takes the whole thing back.

## Install

You need four things before the bot can run: a vault for it to write in, an Anthropic API key, a
Telegram bot of your own, and this repo's release. None of this touches the bot's code — it all
lands in one `.env` file.

1. **Install [uv](https://docs.astral.sh/uv/)** (`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`) if you don't already have it.
2. **Pick a vault.** Any folder of markdown files on this machine, usually your Obsidian vault.
   An empty folder works: the bot makes its notes on first use.
3. **Get an Anthropic API key.** See "Using Claude", below — this is what reads your messages.
4. **Create a Telegram bot and find your own user id:**
   - Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow the prompts →
     copy the token it gives you.
   - Message [@userinfobot](https://t.me/userinfobot) (or any similar "what's my id" bot) to get
     your own numeric Telegram user id. Without this the bot will not talk to you — see
     `TELEGRAM_ALLOWED_USER_IDS` below.
5. **Build and install a release.** Double-click **`release.cmd`** in this repo and accept the
   defaults (see [RELEASE.md](RELEASE.md)). When it asks *"Update the production install now"*,
   say yes: with no install there yet, it offers a fresh one in `C:\apps\ai_assistant` — its own
   Python environment, the database schema, and a `.env` copied from `.env.example`, which it
   offers to open in Notepad. It does **not** fill in your tokens for you — do that next.
6. **Fill in `.env`** in the install directory (`C:\apps\ai_assistant\.env` in the example
   above) with the Telegram bot token, your Telegram user id, the Anthropic key and the vault's
   path (see the table below for exactly which keys).
7. **Start the bot:** double-click **`start.cmd`** in the install folder
   (`C:\apps\ai_assistant\start.cmd`). The bot runs in that window; stop it with Ctrl+C (then
   `Y`) or by closing the window. Watch for `vault reader: staged on claude-haiku-4-5` to confirm
   it found the vault and the key. If it stops instead, the window says why in one line and offers
   the fix (open `.env`, or apply a pending migration); the exit codes are under "When something
   breaks" below.

   Only one copy runs per install: a second `start.cmd` refuses with "already running", because
   two copies would fight over the same Telegram bot and database. `deploy\run.ps1` still exists
   for unattended starts (Task Scheduler and the like) — it has no prompts.

To upgrade later, see [RELEASE.md](RELEASE.md) — `update.cmd` in the install directory walks you
through it.

## The `.env` keys that matter day to day

Everything below lives in `.env` in the install directory. `.env.example` documents the keys with
their defaults, and `app/config.py` is the full list; these are the ones worth knowing about once
the bot is running:

| Key | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (required) | From @BotFather. |
| `TELEGRAM_ALLOWED_USER_IDS` | — (required) | Comma-separated numeric Telegram user ids. Anyone else's message is silently ignored — no reply, nothing written anywhere. Add a second id here to let another person use the same bot. |
| `ANTHROPIC_API_KEY` | — (required) | See "Using Claude". Empty: the bot starts, but the vault, web research and link answers stay off. |
| `OBSIDIAN_VAULT` | — (required) | The vault: a folder on this machine. Empty: off. |
| `TIMEZONE` | `UTC` | Your own IANA zone (e.g. `Europe/Paris`): dates in messages, the morning agenda, the mail digest and reminders all run on it. |
| `DAILY_DIGEST_AT` | `09:00` | When the morning agenda is sent. Empty: never. Editable on the admin page. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Where Ollama is listening, if you sort mail with a local model — see below. |
| `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` / `WHISPER_LANGUAGE` | `large-v3-turbo` / `auto` / `int8` / `ru` | Speech recognition. `WHISPER_DEVICE=auto` tries your GPU first and falls back to CPU automatically (and remembers that decision for as long as the process runs). **`.env.example` ships `cpu`**, because a local mail model leaves little of an 8 GB card for Whisper — with no local model, or on a bigger GPU, `auto` is fine. |
| `ADMIN_UI_PORT` | `8787` | The local admin page's port (`http://127.0.0.1:8787`). Set to `0` to disable the admin page entirely. |
| `UNDO_WINDOW_S` | `300` (5 min) | How long the `[Отменить]` button on a reply keeps working. |
| `LOG_LEVEL` | `INFO` | See "Logging", below. |

Lines left in an existing `.env` for settings that no longer exist are ignored.

## Using Claude (required)

Claude understands messages noticeably better than any model an 8 GB laptop GPU can run, so it
is what reads them: the vault's reader, answers to questions, web research and changes to a note
all go through it. There is no local fallback. Whenever Claude can't answer — no internet, a rate
limit, an outage, credit run out — your words are kept as they are in the inbox note and the reply
says why, with one plain sentence on what to do about it, at most once an hour. Every answer goes
through the same checks before anything is written.

**This needs an API key, not your claude.ai subscription.** Anthropic's Consumer Terms don't
allow a bot or script to use a Pro/Max subscription; automated access has to go through an
API key, which is billed separately, per use.

1. Open **[platform.claude.com](https://platform.claude.com)** and sign in (the same email as
   your claude.ai account works; it is a separate account area).
2. **Billing** → add a payment method and buy credits. The minimum top-up is plenty: an ordinary
   message is a few short Haiku calls; only a web search costs real money (see below).
3. **Limits** → set a monthly spend limit (say, $10), so a bug can never run up a bill.
4. **API keys** → **Create key**, name it `ai-assistant`, and copy it. It is shown only once.
5. Put it in `.env` and restart the bot:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   The console window then shows `vault reader: staged on claude-haiku-4-5`. Without a key it warns
   `no ANTHROPIC_API_KEY: the vault, web research and link answers are off`. A wrong key shows up on
   your first message: the reply carries «⚠️ Claude не принимает ключ — проверьте
   ANTHROPIC_API_KEY».

| Key | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | (empty) | Like the other tokens, it is never logged. |
| `FILER_MODEL` | `claude-haiku-4-5` | Reads every message. |
| `FILER_STRONG_MODEL` | `claude-sonnet-5` | Lists a whole set («все романы Пелевина») and plans a message of several steps. |
| `VAULT_ANSWER_MODEL` | `claude-haiku-4-5` | Answers a question from the notes the search found. Empty: the reply is the list of notes found. |
| `ESCALATE_MODEL` | `claude-sonnet-5` | A second opinion on an answer the first model could not settle, and on a watched folder's summary. Empty: never. |
| `REWRITE_MODEL` | `claude-sonnet-5` | Changes a note. Sonnet, because this is your own writing and Haiku loses facts while shortening. |
| `LINKER_MODEL` | `claude-haiku-4-5` | Adds links between notes after the reply. |
| `CLAUDE_MODEL` | `claude-haiku-4-5` | Answers a question about a linked page. |
| `RESEARCH_MODEL` | `claude-sonnet-5` | Web research, below. |

**What Claude sees.** Each message's text and the chat's recent exchanges, your guide note
(`_bot.md`), your folder names and tags, the names, properties and headings of the notes the
message may mean, open tasks that share a word with it, and your product names. A note's text only
when you ask to change it or a question's search finds it. Anthropic's API does not train on API
traffic by default.

## Goals that take several steps

A message that asks for different things, or for things that build on each other, becomes a
**plan**:

- «организуй поездку в Японию: заметка в Проекты с планом, достопримечательности Киото с
  фото, задачи купить билеты и оформить визу»
- «найди лучшие книги про Рим, сравни с тем, что я уже читал, и посоветуй три»

A list is not a plan: «добавь книги: Солярис, Дюна, Пикник на обочине», or a set you did not list,
«все романы Пелевина», is one step, with one note per book. No note is added twice: a book the
vault already has is left exactly as it is, and the bot compares the way you would, ignoring
case and punctuation.

Beside the normal reading of every message, one cheap question decides whether it takes several
steps, so a one-step message waits for nothing. If it does, `FILER_STRONG_MODEL` splits it into
linked steps — write something, answer a question, read your records, search the web, or work over
what earlier steps produced (filter, rank, compare, recommend) — and each gets the results it
needs. At most 8 steps and 3 web searches.

It all ends as one reply, with one **Отменить** that takes back everything the message wrote. A
step that failed is named in the reply and skips only what needed it; if a write failed, your
whole message also goes to the inbox note, once.

## Web search and images

With an Anthropic key set, a message that asks to *find* something *and write it down* is
looked up first, and the bot writes the Markdown result — a short summary, then «Источники» —
to the note you named, or to a new note. A message that only asks is answered in the chat (🌐),
and nothing is written.

Nothing about where you live is built in. Write it on the admin page, in **Дополнение к
промпту поиска**: your city, which sources to prefer, which currency to quote prices in
(«Я живу в Париже; цены — в евро, сначала местные магазины»). That text is added to the
research prompt as it is.

Besides web search (`RESEARCH_MAX_SEARCHES`, default 4) Claude reads pages, and it can search
sites you list directly, using each site's own search page. The list is yours, in
`data/sites.json` next to the database — one entry per site:

```json
[{"name": "shop.example", "kind": "groceries", "template": "https://shop.example/search?q={q}",
  "rent_template": "", "probe": "milk"}]
```

`kind` is one of `real_estate`, `groceries`, `building`, `goods`, `prices` (a price-comparison
site, tried first for "where is it cheapest"). `{q}` becomes the query; `rent_template` is a
second search for rentals on a real-estate site; `probe` is a query that should return results.
A site with an empty `template` is only named to Claude, which finds it with web search instead.
Without the file (or without any template) there is no direct site search at all. Site searches
and page reads together are capped at `RESEARCH_MAX_READS` (default 8). Pages are read through
Jina Reader, which gets past most bot checks (`JINA_API_KEY` is optional);
`uv run python -m tools.check_sites --sites data/sites.json` checks your templates live.
«что у меня в …» stays a search of your own vault.

When you ask for pictures («с изображениями», «добавь картинок», «покажи, как выглядит»), up
to six are added under «Изображения». They come from Wikimedia Commons and from the pages the
text cites — never from links the model writes itself, which turned out to be mostly invented.
Each is checked to really be an image (≤ 5 MB) and kept as a link in the note, so it shows for as
long as the original site serves it. The check only ever goes to public internet addresses, never
to your machine or local network.

A request the search cannot make sense of — a voice note that came out wrong, say — is not
guessed at: a question gets «🌐 Чтобы поискать, уточните: …» back, and a note is written without
what the search would have found, and the reply says so.

The bot only searches when your message asks it to — «найди», «поищи», «узнай», «с
картинками», «референсы»; the list is yours, under **Слова, по которым бот идёт искать в
интернете** on the admin page. «Хочу посмотреть фильм Uncharted» is a line for a list, not a
research project, and is written as one even if the model offers to look it up.

A search takes about 20 seconds to a minute with `claude-sonnet-5` (what `RESEARCH_MODEL` ships
as) and costs roughly 4–15 ¢; the chat shows the bot typing while it runs, and a question says
«🔎 Ищу в интернете…». After
`RESEARCH_SOFT_DEADLINE_S` (90 s) the bot stops it looking and has it answer with what it found,
and `RESEARCH_DEADLINE_S` (3 minutes) is the backstop. `claude-haiku-4-5` is faster and about
a third of the price, with shallower writing. Undo removes the whole written result, and its few
minutes are counted from the moment the note appears — not from when you sent the message.

## Links in a message

A link you send is read, and what is on the page goes to the bot along with your message, so
it acts on the page as well as on your words. «добавь в сравнение квартир» with a listing link
fills in the price, rooms and area. «в список книг» with a shop link writes the book's real
title. «сохрани» summarises an article into the note. A question about the page («сколько тут
этажей?») is answered from the page and nothing is written. The link itself is always kept.
Up to three links per message are read, in parallel, for at most 12 seconds; a page that will
not open just stays a plain link.

Pages go through the same reader as web search (Jina Reader, or a direct fetch). Links to sites
on the admin page's **Сайты, ссылки на которые бот не открывает** list are never read. By default
that list holds only Google Docs/Drive/Mail and Notion; add your bank and anything else private
there yourself. Anything on a page is
treated as data, never as instructions to the bot. A group chat with its own scope does not read
links at all.

## Google documents as sources

A question can also be answered from Google Docs, Sheets, Forms
and folders — read-only. In a Google Cloud project of your own, enable the Drive, Docs, Sheets
and Forms APIs, create an OAuth client of type "Desktop app", save its key as
`data/google_client.json` next to the database, and run `uv run python -m tools.google_login`
once in the install folder: it prints a link, you allow read-only access, and the sign-in is
kept in `data/google_token.json`.

The sources themselves are listed on the admin page under **Источники**: a link per document,
table, form or folder, the chats that may use it, how a document is cut into sections, and
**Как читать** — your own words on what the document holds (what a form's answers mean, which
column is what). The bot passes that text to the model as it is; nothing about any document is
built in. Your private chat uses every source and may also search the rest of your Drive; a
group uses only the sources ticked for it.

## Watching Syncthing and folders

**Syncthing.** If the vault reaches this PC through Syncthing, the bot checks Syncthing's own API
on this machine every 5 minutes (address and key from its `config.xml`; `SYNCTHING_CONFIG` if it
is not in the Windows default place) and tells you once when Syncthing here stops answering, a
device has been away longer than the admin page's threshold (60 minutes by default) or a folder
has errors — and once when it is back. Paused devices and folders are left alone.

**Watched folders.** Under **Папки** on the admin page, list folders on this machine that
something else writes files into — a scheduled job's reports, say — each with an optional file
pattern and your instruction: what to look for and what to tell you. At the mail digest's times
(even with the mail digest off) the bot reads the new files that have stopped changing (15
minutes), each once, and writes to you: nothing when there is nothing new, one line when all is
well, otherwise what needs you, with the file it came from. Needs `ANTHROPIC_API_KEY`.

## Teaching the bot your vault

The bot learns how your vault is organised from the vault itself, and from the admin page.
Everything applies from the next message — no restart.

- **`_bot.md`**: a note in your vault, in your own words, sent with every message: where things
  go, what your tags mean, what a folder holds. Example: «Фильмы — список на странице Медиа. Рецепты
  — папка Рецепты. Покупки для дома — задачи с тэгом дом.» Edit it in Obsidian, on your phone too.
  Pages it links with `[[...]]` are your hubs: the bot knows their shape even when a message shares
  no word with their name.
- **Folders.** A folder of notes with the same properties is a table, and a new note takes its
  shape from them: the properties most of them carry, in the same order, real tick boxes, and a
  link where they keep that property as one. Change the notes and the shape follows.
- **Names.** The notes it works with are `Задачи`, `Продукты` and `Разное`, the folders `Заметки` and
  `Дневник`. It makes them on first use.

The admin page (**`http://127.0.0.1:8787`**, on the machine the bot runs on — it only ever answers
requests from that machine, so it is not reachable from your phone or another computer) holds the
rest, saved next to the database: what is switched on (Obsidian, links between notes, the mail
digest, Syncthing, watched folders), the bot's name, the agenda's times, the sites it never opens,
the words that start a web search, the mail buckets, group chats and what each may do, reminders
and reminder rules, Google sources, and watched folders.

## Telegram commands

| Command | Does |
|---|---|
| `/start`, `/help` | Prints a short usage reminder. |
| `/undo` | Reverts the most recent change in this chat, if the undo window hasn't closed. In a group, your own most recent change. |

All three are published to Telegram at startup, for private chats only, so they show up in the
"/" menu in the chat rather than having to be remembered. A group shows no menu.

Besides commands, just talk to it — text or a voice note, in Russian. A voice note is transcribed
locally and never echoed back to you; you get a short «Расшифровываю…» acknowledgement while that
runs, and the reply always describes what was actually written, not the transcript.

## Reading the audit log

Every message the bot handles — successful or not — writes exactly one row to the `events` table
in `data\bot.sqlite` (in the install directory), whether or not anything ended up in the vault. This
is the place to look when you want to know *why* the bot did (or didn't do) something, days later.
Useful columns: `ts`, `raw_input` (your text, or the voice transcript), `decision` (how the turn
went: `VAULT`, `UNDO`, `FIX`, `ERROR` …), `error` (the code, if any — cross-reference against
[ERRORS.md](documentation/ERRORS.md)), `llm_model`, `llm_response` (each model call, with its
tokens and its error), `duration_ms`. A voice note that could not be transcribed has no row.

Query it with the `sqlite3` CLI, or open it with a free GUI tool like
[DB Browser for SQLite](https://sqlitebrowser.org/) if you'd rather click around than type SQL:

```powershell
sqlite3 C:\apps\ai_assistant\data\bot.sqlite "SELECT ts, kind, decision, error FROM events ORDER BY id DESC LIMIT 20;"
```

Your Telegram token and Anthropic key are never written to this database, or to any log —
see "Logging" below and `documentation/ARCHITECTURE.md` §14.

## Running Ollama or Whisper on another machine

**Ollama is only for sorting mail with a local model** (`MAIL_MODEL` set to anything that is not
`claude-...`, or the `MAIL_SHADOW_MODEL` trial). Nothing else needs it, and with the default
`MAIL_MODEL` you need not install it at all. Set `OLLAMA_BASE_URL` in `.env` to point at any
reachable Ollama server (e.g. `http://192.168.1.20:11434` for another PC on your network with a
better GPU) and restart the bot — nothing else changes.

**Whisper (speech recognition) does not, yet.** There's a `WHISPER_DEVICE=cpu` setting for running
it *without a GPU on the same machine*, but there is currently no way to point speech recognition
at a different machine — the interface for that (`app/speech/whisper_remote.py`) hasn't been
built. If your machine has no usable GPU, voice messages still work, just more slowly (CPU
`int8`); text messages are unaffected either way.

## Logging

The bot logs to its console window **and** to `logs\bot.log` in the install folder, rotated at
5 MB with five old files kept (`bot.log.1` … `bot.log.5`), so the log survives closing the window
and never grows without bound. Change the location with `LOG_FILE` in `.env`; set it empty to
switch the file off. At the default `LOG_LEVEL=INFO` every message you send leaves a short trail
— what each stage decided, how long it took, what was written, and one closing line with the
decision:

```
INFO app.vault.staged [event=28] staged claude-haiku-4-5: add > t > task | 3 call(s), 1.2 + 1.1 + 1.4 = 3.7s
INFO app.vault.pipeline [event=28] vault claude-haiku-4-5: task:Задачи
INFO app.conversation.orchestrator [event=28] done: vault in 4.1s
```

`[event=28]` is the row in the audit database (`data\bot.sqlite`), which has the message itself
and every model call it made. The log never has your message text (it is there at
`LOG_LEVEL=DEBUG` only), and never a token, however verbose you make it. That second part isn't just "we try not to log tokens": every line
the bot writes is passed through a filter that replaces your Telegram token, Anthropic key and
Gmail app password with
`***` first, whoever produced the line — in the file exactly as on screen. On top of that, Telegram's
own client library normally puts your bot token straight into the URL of every request it makes,
and logs that URL — so the bot deliberately holds two of that library's own loggers (`httpx`/
`httpcore`, its HTTP layer, and `telegram.ext.ExtBot`, which logs the same URL once on its own at
`DEBUG`) at `WARNING` no matter what `LOG_LEVEL` you set, specifically so raising it to `DEBUG` to
chase down a problem can never turn either leak back on. See `app/logging_setup.py` and
`documentation/ARCHITECTURE.md` §14 for the full detail.

## When something breaks

If the process **exits immediately** instead of starting up, the exit code tells you what to fix
— see [documentation/ERRORS.md](documentation/ERRORS.md) ("Startup checks") for the full list:

| Exit code | Means |
|---|---|
| `2` | A `.env` value is missing or unusable (it names which key, never the value). Three ways to get it: a required key missing; a `LOG_LEVEL` that isn't one of `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`; or Telegram itself rejecting `TELEGRAM_BOT_TOKEN` (mistyped, or revoked/regenerated in @BotFather since you last pasted it). |
| `4` | The database has a pending schema migration. `start.cmd` offers to apply it on the spot (backing the database up first); otherwise run `.venv\Scripts\python.exe -m tools.migrate --apply` in the install folder. |
| `5` | The bot is already running for this install — look for its other window. |

**The first voice message takes minutes and looks like nothing is happening.** That is expected
once: the speech model (`large-v3-turbo`, ~1.5 GB) is downloaded from HuggingFace on first use
and cached under your user profile, and the console shows download progress bars while it
happens. The bot replies «Расшифровываю голосовое сообщение…» as soon as the voice note arrives
so you can tell it is alive; the real answer follows when the model has loaded. Every later voice
message reuses the loaded model and is fast. During that one download HuggingFace may print a
warning about *unauthenticated requests* — harmless, it only means no `HF_TOKEN` is set, which a
public model doesn't need. After that the model loads from the local cache with no network
request at all, so the warning does not come back.

**A voice message kills the whole bot process on an NVIDIA machine** — the console shows
something like `Could not locate cudnn_ops64_9.dll` and the process disappears with no Python
traceback. That abort happens inside the CUDA library, below Python, so the bot's own
CPU fallback never gets the chance to catch it. Either install the cuDNN 9 runtime for your CUDA
version, or set `WHISPER_DEVICE=cpu` in `.env` and restart — voice messages then work on the CPU,
just more slowly, and nothing else changes.

**The admin page isn't there but the bot works.** Look for a `admin page could not bind
127.0.0.1:8787` warning in the console: something else is on that port, or Windows has reserved
it (`WinError 10013`; `netsh interface ipv4 show excludedportrange protocol=tcp` lists the
reserved ranges). Pick another port with `ADMIN_UI_PORT` and restart. The bot itself keeps
running either way — the page is optional.

**The bot answers «Хранилище не подключено».** There is no vault to write to: look at the startup
log for `no ANTHROPIC_API_KEY` or `OBSIDIAN_VAULT is not a folder`, and check that
`OBSIDIAN_ENABLED` is not `false`. Fix `.env` and restart.

A failure that only **warns** (no Anthropic key, the admin port unavailable, the command menu not
published) does not stop the bot — it keeps running and simply can't do
that one thing until you fix it. `documentation/ERRORS.md` is the full table of every error code,
what the user sees, and what the bot does about it; it's worth a look before assuming something
is broken versus working as designed (a line in the inbox note is not an error).

## Quick smoke test after installing

Once `.env` is filled in and the bot is running:

1. Text: «позвонить врачу завтра» → a line appears in `Задачи.md` in your vault, the reply names
   it, `[Отменить]` works.
2. The same sentence as a voice note → the same result.
3. «что у меня завтра?» → the task, answered from the file.
4. Something that can't be placed → a line in `Разное.md`, and the reply says so.

## Development

This repo (`uv sync`, `uv run pytest -q`, `uv run ruff check .`) is the development tree — the
bot is only ever meant to be *run* from a release install, never from here. See
[RELEASE.md](RELEASE.md) for building and shipping a release, and `documentation/` for how the
whole thing is put together.
