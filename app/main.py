"""Application wiring, startup checks, and the process entry point.

Everything else in this codebase is a class waiting to be constructed; this module is where that
finally happens, once, for the single long-running process a user starts on their own machine
(`deploy/run.ps1`: `python -m app.main`). `build()` constructs every collaborator and returns them
on one `App`. Startup checks come in two parts, run from two different places, and that split is
load-bearing rather than cosmetic:

* `startup_checks(app)` — settings (exit 2) and schema (exit 4) — is synchronous and runs in
  `main()` before any event loop exists.
* `post_init_checks(app, application)` — the Telegram command menu, the admin server — is
  async and runs from `Application.post_init`, i.e. inside python-telegram-bot's own event loop,
  the same one that later does the real polling.

The split exists because of a real, reproduced failure mode: running async work in a throwaway
`asyncio.run(...)` before calling `Application.run_polling()` leaves httpx-backed clients holding
idle keep-alive connections bound to that now-closed loop; the next real call on them then raises
`RuntimeError: Event loop is closed`, self-heals on retry, and reads like a random flake instead
of the systematic bug it is. Everything async runs on the one loop `run_polling()` owns for the
whole process lifetime, so this never happens.

Startup/exit messages on stderr are host-side operator text, not the Russian user-facing UI in
`app/texts.py`, and stay in English on purpose — this module must contain no Cyrillic literal.

Migrations are never applied here (documentation/ARCHITECTURE.md section 15):
`AuditStore.assert_schema_current()` refuses to run against a pending migration, and this
module's only reaction to that is to print the hint it already carries and exit 4. Nothing here
ever calls `AuditStore.migrate()` — that is the installer/updater's job (or `tools/migrate.py`
run by hand), never something the bot does to itself on the way up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import NoReturn

import anthropic
from telegram import Bot, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault
from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken
from telegram.ext import Application

from app import address, texts
from app.admin.server import AdminServer
from app.audit.migrate import MigrationError
from app.audit.store import AuditStore
from app.chats import Chats
from app.config import Settings, load_settings
from app.conversation.memory import ChatMemory
from app.conversation.orchestrator import Orchestrator
from app.daily import DailyMessage, LastRun, parse_times
from app.google.auth import GoogleAuth
from app.google.client import GoogleClient
from app.google.sources import GoogleSources, Sources
from app.instance_lock import AlreadyRunning, InstanceLock
from app.llm.answer import LinkAnswerer
from app.llm.edits import Editor
from app.llm.health import Health
from app.llm.image_search import ImageSearch
from app.llm.prompts import WEB_WORDS
from app.llm.research import WebResearcher
from app.llm.rewrite import Rewriter
from app.logging_setup import configure
from app.mail.buckets import Buckets
from app.mail.classify import Classifier
from app.mail.imap import GmailIMAP
from app.mail.local import LocalClassifier
from app.mail.marks import DigestMarks
from app.mail.service import MailService, MailState, digest, shadow_digest
from app.media.instagram import Instagram, mirrors
from app.reminders.rules import ReminderRules, run_rules
from app.reminders.scheduler import ChatGone, ChatMoved, ReminderScheduler
from app.reminders.store import ReminderStore
from app.speech.base import SpeechToText
from app.speech.whisper_local import WhisperLocal
from app.switches import Switches
from app.telegram.handlers import mail_keyboard, register
from app.telegram.recent import RecentMessages
from app.telegram.sending import send_text
from app.tuning import Defaults, Tuning
from app.vault.answer import VaultAnswerer
from app.vault.askers import Askers
from app.vault.filer import Filer
from app.vault.index import VaultIndex
from app.vault.linker import Linker
from app.vault.omnisearch import OmniSearch
from app.vault.pipeline import VaultPipeline
from app.vault.planner import VaultPlanner
from app.vault.scope import ScopedIndex
from app.vault.staged import StagedFiler, claude_ask
from app.vault.writer import VaultWriter
from app.watch.folders import Folders, FolderWatch
from app.watch.syncthing import SyncthingHealth, load_api
from app.web.images import ImageCheck
from app.web.links import LinkReader
from app.web.reader import Reader
from app.web.sites import load as load_sites

log = logging.getLogger(__name__)

SYNCTHING_EVERY_S = 300.0  # how often Syncthing's health is checked

EXIT_CONFIG = 2
EXIT_PENDING_MIGRATION = 4
EXIT_ALREADY_RUNNING = 5

_ALREADY_RUNNING = (
    "the bot is already running for this install (look for its other window). "
    "Close that one first - two copies would fight over Telegram and the database."
)

# Non-empty so python-telegram-bot's Bot() constructor (which only ever checks truthiness, never
# format) accepts it when the real token is blank. Real validity is settings.require_telegram()'s
# job — the very first startup check, which exits the process long before polling would ever put
# this placeholder in front of Telegram.
_PLACEHOLDER_TOKEN = "0:MISSING-TELEGRAM-BOT-TOKEN"

# What main() prints when Telegram itself rejects the token. Fixed text, never `str(e)` and never
# `exc_info`: python-telegram-bot builds `InvalidToken`'s message by interpolating the token into
# it (`telegram/_bot.py`: "The token `<token>` was rejected by the server."), so printing or
# logging that exception is exactly the leak this message exists to avoid.
_TELEGRAM_TOKEN_REJECTED = (
    "configuration error: Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in .env "
    "(re-issue it with @BotFather if it was revoked) and restart."
)

SpeechFactory = Callable[[Settings], SpeechToText]


def uses_cloud(settings: Settings) -> bool:
    return bool(settings.anthropic_api_key.get_secret_value())


def _default_speech(settings: Settings) -> SpeechToText:
    return WhisperLocal(settings)


class Sweeper:
    """Runs `flush` every `interval_s`, forever, until stopped. One asyncio task loops
    sequentially — sleep, one run, sleep again — so two runs can never overlap without any extra
    locking of their own. Whatever a run raises is logged and swallowed here: the loop's only job
    is to keep ticking."""

    def __init__(self, flush: Callable[[], Awaitable[object]], interval_s: float) -> None:
        self._flush = flush
        self._interval = interval_s
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._flush()
            except Exception:
                log.exception("a repeating check failed")


@dataclass
class App:
    """Every object the process needs, constructed exactly once by `build()`. Tests build one
    with a fake speech factory and call `startup_checks`/`post_init_checks`/its collaborators
    directly — nothing in this module ever polls Telegram on its own."""

    settings: Settings
    store: AuditStore
    speech: SpeechToText
    orchestrator: Orchestrator
    admin: AdminServer
    telegram_app: Application


def _claude_probe(settings: Settings):
    """The smallest real call, for checking that a remembered failure is still true before the
    user is told about it again: one token from the cheapest model. None without a key."""
    key = settings.anthropic_api_key.get_secret_value()
    if not key:
        return None
    client = anthropic.AsyncAnthropic(api_key=key, timeout=20.0, max_retries=0)

    async def probe() -> None:
        await client.messages.create(model=settings.filer_model, max_tokens=1,
                                     messages=[{"role": "user", "content": "."}])

    return probe


def _vault_pipeline(settings: Settings, switches: Switches, tuning: Tuning,
                    health: Health, rewriter: Rewriter | None = None,
                    editor: Editor | None = None,
                    reminders: ReminderStore | None = None,
                    google: GoogleSources | None = None) -> VaultPipeline | None:
    """The vault side, when a vault is configured and Claude is reachable."""
    key = settings.anthropic_api_key.get_secret_value()
    if not (settings.obsidian_enabled and settings.obsidian_vault and key):
        return None
    root = Path(settings.obsidian_vault)
    if not root.is_dir():
        log.warning("OBSIDIAN_VAULT is not a folder: %s — the Obsidian side stays off", root)
        return None
    index = VaultIndex(root)
    writer = VaultWriter(index, tag_source=lambda: tuning.countdown_tag)
    linker = Linker(index, writer, api_key=key, model=settings.linker_model,
                    extra=lambda: tuning.linker_note, health=health)
    strong = settings.filer_strong_model.strip()
    staged = settings.filer_mode.strip().lower() == "staged"
    log.info("vault reader: %s on %s", "staged" if staged else "single", settings.filer_model)
    # Listing a whole set ("all of his novels") is asked of the stronger model, and only then.
    filer = (StagedFiler.claude(key, settings.filer_model, health=health,
                                lookup_model=strong or settings.rewrite_model) if staged
             else Filer(key, settings.filer_model, health=health))
    return VaultPipeline(index, writer, filer,
                         linker, linking=lambda: switches.get("linker"), rewriter=rewriter,
                         editor=editor,
                         # The single reader's second opinion (0.6.7), with a longer timeout
                         # because Sonnet thinks first. The staged reader asks narrow questions
                         # instead, and one Sonnet question about everything would undo that.
                         strong=(Filer(key, strong, timeout_s=90.0, health=health)
                                 if strong and strong != settings.filer_model and not staged
                                 else None),
                         answerer=(VaultAnswerer.claude(key, settings.vault_answer_model,
                                                        health=health,
                                                        second_model=settings.escalate_model.strip())
                                   if settings.vault_answer_model.strip() else None),
                         omni=(OmniSearch(settings.omnisearch_port)
                               if settings.omnisearch_port else None),
                         reminders=reminders, zone=settings.timezone, google=google,
                         web_words=lambda: tuning.web_words,
                         date_props=lambda: tuning.date_props,
                         askers=Askers(settings.db_path.with_name("askers.json")),
                         # A message of several steps is planned by the stronger model, after
                         # one cheap question to the light one (app/vault/planner.py).
                         planner=(VaultPlanner.claude(key, settings.filer_model,
                                                      strong or settings.rewrite_model,
                                                      health=health) if staged else None))


def _google_sources(settings: Settings, sources: Sources) -> GoogleSources | None:
    """The Google side, when a client key is there; signing in is tools/google_login.py."""
    data = settings.db_path.parent
    if not (data / "google_client.json").exists() and not (data / "google_token.json").exists():
        return None
    client = GoogleClient(GoogleAuth(data / "google_token.json"))
    return GoogleSources(client, sources, data / "sources")


def _reminder_scheduler(settings: Settings, store: ReminderStore, rules: ReminderRules,
                        vault: VaultPipeline | None, chats: Chats, tuning: Tuning,
                        application: Callable[[], Application]) -> ReminderScheduler:
    """The loop that sends reminders and runs the admin page's reminder rules. A rule for a
    group reads the vault as that group's scope sees it, and a group no longer listed gets
    nothing."""

    async def send(chat_id: int, text: str) -> None:
        if chat_id < 0 and chats.get(chat_id) is None:
            # A group taken off the list (or switched off): nothing is sent there, and the
            # reminder waits — switched back on, it still arrives.
            raise RuntimeError("group not listed")
        try:
            await send_text(application().bot, chat_id, text)
        except ChatMigrated as e:
            raise ChatMoved(e.new_chat_id) from None
        except Forbidden as e:
            raise ChatGone(str(e)) from None
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                raise ChatGone(str(e)) from None
            raise

    def index_for(chat_id: int) -> VaultIndex | None:
        if vault is None:
            return None
        if chat_id < 0:
            scope = chats.get(chat_id)
            if scope is None:
                return None
            if not scope.full:
                return ScopedIndex(vault.index, scope)
        return vault.index

    async def run(now) -> None:
        if vault is None or not rules.all():
            return
        await asyncio.to_thread(vault.index.refresh)
        await run_rules(now, rules.all(), store, index_for, tuning.date_props, send,
                        settings.timezone)

    return ReminderScheduler(store, send, settings.timezone, rules=run)


def _daily_digest(settings: Settings, vault: VaultPipeline | None, switches: Switches,
                  tuning: Tuning, health: Health,
                  application: Callable[[], Application]) -> DailyMessage | None:
    """The morning agenda, if there is a vault to read it from and a time to send it at. It
    reads the Obsidian side only: the vault is where the dates live."""
    if vault is None or not settings.allowed_user_ids or not parse_times(tuning.agenda_at):
        return None

    async def send() -> None:
        if not switches.get("obsidian"):
            return
        text = await asyncio.to_thread(vault.digest)
        warning = await health.checked_note()
        if not text and not warning:
            log.info("daily digest: nothing due, nothing sent")
            return
        # A digest that silently did not run is worse than one that says why.
        text = f"{text}\n\n{warning}".strip() if warning else text
        for chat_id in sorted(settings.allowed_user_ids):
            try:
                await send_text(application().bot, chat_id, text)
            except Exception:  # one blocked chat must not stop the others
                log.exception("could not send the daily digest to a chat")
        # Without this line a digest sent twice left no trace in the log at all.
        log.info("daily digest sent: %d lines", len(text.splitlines()))

    return DailyMessage(send, lambda: parse_times(tuning.agenda_at), settings.timezone,
                        remember=LastRun(settings.db_path.with_name("daily_state.json"),
                                         "agenda"))


async def _to_owners(application: Callable[[], Application], settings: Settings,
                     text: str) -> None:
    """A message the bot sends on its own, to each owner's private chat."""
    for chat_id in sorted(settings.allowed_user_ids):
        try:
            await send_text(application().bot, chat_id, text)
        except Exception:
            log.exception("could not send a watcher's message to a chat")


def _syncthing_watch(settings: Settings, switches: Switches, tuning: Tuning,
                     application: Callable[[], Application],
                     ) -> tuple[Sweeper | None, SyncthingHealth | None]:
    """Syncthing checked every few minutes (app/watch/syncthing.py). Off without its config on
    this machine: a PC that does not run Syncthing hears nothing about it."""
    # An empty SYNCTHING_CONFIG= reads as Path("."), which is no config file at all.
    given = settings.syncthing_config
    path = given if given and str(given) not in ("", ".") else (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Syncthing" / "config.xml")
    if load_api(path) is None:
        log.info("syncthing: no readable config at %s; not watched", path)
        return None, None
    health = SyncthingHealth(settings.db_path.with_name("syncthing_state.json"),
                             after=lambda: timedelta(minutes=tuning.sync_after),
                             zone=settings.timezone)

    async def check() -> None:
        if not switches.get("syncthing"):
            return
        api = load_api(path)  # read every time: a device renamed or added is seen at once
        if api is None:
            return
        for text in health.update(await api.observe(), api.devices, api.folders):
            await _to_owners(application, settings, text)

    return Sweeper(check, SYNCTHING_EVERY_S), health


def _folder_digest(settings: Settings, switches: Switches, tuning: Tuning, folders: Folders,
                   application: Callable[[], Application],
                   ) -> tuple[DailyMessage | None, FolderWatch | None,
                              Callable[[], Awaitable[None]] | None]:
    """Watched folders read at their own times — the mail digest's unless set —
    (app/watch/folders.py), whether or not the mail digest itself is on. Needs Claude: the
    files are read with the owner's words."""
    key = settings.anthropic_api_key.get_secret_value()
    if not key or not settings.allowed_user_ids:
        return None, None, None
    sdk = anthropic.AsyncAnthropic(api_key=key, timeout=90.0, max_retries=1)
    strong = settings.escalate_model.strip()
    watch = FolderWatch(folders, settings.db_path.with_name("folders_state.json"),
                        claude_ask(sdk, settings.vault_answer_model or settings.claude_model,
                                   think=False),
                        confirm=claude_ask(sdk, strong, think=False) if strong else None)

    async def send() -> None:
        if not switches.get("folders"):
            return
        text = await watch.run()
        if text:
            await _to_owners(application, settings, text)
        else:
            log.info("watched folders: nothing new, nothing sent")

    return (DailyMessage(send, lambda: parse_times(tuning.folders_at), settings.timezone,
                         remember=LastRun(settings.db_path.with_name("daily_state.json"),
                                          "folders")),
            watch, sdk.close)


def _mail_digest(settings: Settings, switches: Switches, buckets_file: Buckets, tuning: Tuning,
                 health: Health, application: Callable[[], Application],
                 ) -> tuple[MailService | None, DailyMessage | None]:
    """Read-only mail triage: fetch what arrived since the last digest, sort it into the user's
    buckets, and send one message. Off unless both the address and the app password are set."""
    key = settings.anthropic_api_key.get_secret_value()
    password = settings.gmail_app_password.get_secret_value()
    # A claude-... model sorts through the API; any other name is a local Ollama model, which
    # took the job over after a week of side-by-side digests (qwen3:8b) and needs no key.
    cloud = settings.mail_model.startswith("claude")
    if not (settings.gmail_address and password and (key or not cloud)
            and settings.allowed_user_ids and parse_times(tuning.mail_at)):
        return None, None
    buckets, meanings = buckets_file.parsed()
    classifier = (Classifier(key, settings.mail_model, buckets, meanings=meanings, health=health)
                  if cloud else
                  LocalClassifier(settings.ollama_base_url, settings.mail_model, buckets,
                                  meanings=meanings, batch=settings.mail_shadow_batch,
                                  num_ctx=settings.llm_num_ctx))
    shadows = [LocalClassifier(settings.ollama_base_url, model, buckets, meanings=meanings,
                               batch=settings.mail_shadow_batch, num_ctx=settings.llm_num_ctx)
               for model in settings.mail_shadow_models]
    if shadows:
        log.info("mail: comparison digests will be written by %s",
                 ", ".join(settings.mail_shadow_models))
    service = MailService(
        GmailIMAP(settings.gmail_address, password), classifier,
        MailState(settings.db_path.with_name("mail_state.json")),
        marks=DigestMarks(settings.db_path.with_name("mail_digests.json")),
        max_per_run=settings.mail_max_per_run, source=buckets_file.parsed, shadows=shadows,
    )

    async def send() -> None:
        if not switches.get("mail"):
            return
        run = await service.run()
        text = digest(run, service.buckets)
        warning = await health.checked_note()
        if not text and not warning:
            log.info("mail digest: nothing new, nothing sent")
            return
        text = f"{text}\n\n{warning}".strip() if warning else text

        async def to_everyone(body: str, markup=None) -> None:
            for chat_id in sorted(settings.allowed_user_ids):
                try:
                    await send_text(application().bot, chat_id, body, markup)
                except Exception:
                    log.exception("could not send the mail digest to a chat")

        # The real digest carries «mark these as read», for exactly the letters it lists. The
        # comparison digests below cover the same letters and get no button of their own.
        digest_id = service.remember(run)
        await to_everyone(text, mail_keyboard(digest_id, read=False) if digest_id else None)
        # The local models only start once the real digest is out — a slow or hung one must not
        # hold up the digest the user relies on — and each sends its own message as it finishes.
        async for shadow in service.compare(run):
            await to_everyone(shadow_digest(shadow, service.buckets))

    return service, DailyMessage(send, lambda: parse_times(tuning.mail_at), settings.timezone,
                                 remember=LastRun(settings.db_path.with_name("daily_state.json"),
                                                  "mail"))


def build(
    settings: Settings, *, speech_factory: SpeechFactory = _default_speech,
) -> App:
    """Constructs every collaborator once and wires them together. Never touches the network or
    the filesystem beyond opening the sqlite file — `speech_factory` is the seam a test uses to
    hand back a fake instead of a real `WhisperLocal`, so `build()`, `startup_checks()` and
    `post_init_checks()` can all be exercised with no real Telegram or Whisper anywhere in
    reach."""
    store = AuditStore(settings.db_path)
    # One health record for the whole process: whichever Anthropic call fails first is the
    # one that explains it, and every reply and digest can say so (app/llm/health.py).
    health = Health(probe=_claude_probe(settings))
    key = settings.anthropic_api_key.get_secret_value()
    # Web research needs Claude; without it the vault never offers a search at all.
    images = ImageCheck()
    researcher = (
        WebResearcher(key, settings.research_model,
                      max_searches=settings.research_max_searches,
                      max_reads=settings.research_max_reads,
                      soft_deadline_s=settings.research_soft_deadline_s,
                      reader=Reader(jina_key=settings.jina_api_key.get_secret_value(),
                                    never_open=lambda: tuning.never_open),
                      is_image=images.is_image,
                      search=ImageSearch(), extra=lambda: tuning.research_note,
                      deadline_s=settings.research_deadline_s, health=health,
                      sites=load_sites(settings.db_path.with_name("sites.json")))
        if uses_cloud(settings) else None
    )
    # Rewriting a note is the one job where a cheaper model is obviously worse: it is the
    # user's own writing being consolidated, so it gets the rewrite model (Sonnet).
    rewriter = Rewriter(key, settings.rewrite_model, health=health) if uses_cloud(settings) \
        else None
    # The editor changes places in a note; the rewriter replaces a whole text. One model call
    # decides which, so both are built together and share the rewrite model.
    editor = Editor(key, settings.rewrite_model, health=health) if uses_cloud(settings) else None
    switches = Switches(settings.db_path.with_name("switches.json"), {
        "obsidian": settings.obsidian_enabled, "linker": settings.linker_enabled,
    })
    tuning = Tuning(settings.db_path.with_name("tuning.json"), Defaults(
        bot_name=settings.bot_name, agenda_at=settings.daily_digest_at,
        mail_at=settings.mail_digest_at, web_words=WEB_WORDS,
        countdown_tag=texts.VAULT_COUNTDOWN_TAG, date_props=texts.VAULT_DATE_PROPS,
    ))
    # Reminders (app/reminders/): set by message, kept in the database, sent by their own loop.
    reminder_store = ReminderStore(settings.db_path)
    reminder_rules = ReminderRules(settings.db_path.with_name("reminder_rules.json"))
    # Google Docs/Sheets/Forms a question may be answered from (app/google/), when the owner
    # has set up a Google client (data/google_client.json) and signed in.
    sources = Sources(settings.db_path.with_name("sources.json"))
    folders = Folders(settings.db_path.with_name("folders.json"))
    google = _google_sources(settings, sources)
    vault = _vault_pipeline(settings, switches, tuning, health, rewriter, editor,
                            reminders=reminder_store, google=google)
    # Group chats with their own scope, edited on the admin page (app/chats.py).
    chats = Chats(settings.db_path.with_name("chats.json"))
    # Each chat's recent exchanges, for follow-ups and "save this" (app/conversation/memory.py),
    # and the recent unaddressed messages of listed groups, in memory only.
    memory = ChatMemory(settings.db_path)
    recent = RecentMessages()
    # A link in a message is data for the vault's reader; only answering a question about the
    # page needs a model of its own. Sites on the admin page's never-open list are not read.
    link_reader = Reader(jina_key=settings.jina_api_key.get_secret_value(),
                         never_open=lambda: tuning.never_open)
    links = LinkReader(link_reader, never_open=lambda: tuning.never_open)
    answerer = (LinkAnswerer(key, settings.claude_model, health=health)
                if uses_cloud(settings) else None)
    orchestrator = Orchestrator(
        settings, store, vault=vault, researcher=researcher, switches=switches, tuning=tuning,
        health=health, links=links, answerer=answerer, scopes=chats.get, memory=memory,
    )
    speech = speech_factory(settings)
    buckets_file = Buckets(settings.db_path.with_name("mail_buckets.txt"),
                           settings.mail_buckets or texts.MAIL_BUCKETS_DEFAULT)
    sync_watch, sync_health = _syncthing_watch(settings, switches, tuning, lambda: telegram_app)
    folder_digest, folder_watch, close_folders = _folder_digest(
        settings, switches, tuning, folders, lambda: telegram_app)
    admin = AdminServer(settings, switches, buckets_file, tuning, chats=chats,
                        vault_index=vault.index if vault is not None else None,
                        reminder_rules=reminder_rules, reminders=reminder_store,
                        sources=sources,
                        google_status=google.status if google is not None else None,
                        folders=folders,
                        folder_status=folder_watch.status if folder_watch is not None else None,
                        sync_status=sync_health.status if sync_health is not None else None)
    reminders = _reminder_scheduler(settings, reminder_store, reminder_rules, vault, chats,
                                    tuning, lambda: telegram_app)
    daily = _daily_digest(settings, vault, switches, tuning, health, lambda: telegram_app)
    mail, mail_digest = _mail_digest(settings, switches, buckets_file, tuning, health,
                                     lambda: telegram_app)

    token = settings.telegram_bot_token.get_secret_value() or _PLACEHOLDER_TOKEN
    telegram_app = Application.builder().token(token).build()
    register(telegram_app, orchestrator, settings, speech, store, mail=mail,
             chats=chats, names=lambda: address.names(tuning.bot_name), recent=recent,
             instagram=Instagram(mirrors(settings.instagram_mirrors)))

    # `app` is assigned below, after these closures are defined but before either is ever called
    # (PTB only calls post_init/post_shutdown once run_polling() — or a test — invokes them), so
    # the late-binding closure over the name `app` sees the fully constructed object every time.
    warming: list[asyncio.Task] = []

    async def _post_init(application: Application) -> None:
        await post_init_checks(app, application)
        if daily is not None:
            daily.start()
        if mail_digest is not None:
            mail_digest.start()
        if sync_watch is not None:
            sync_watch.start()
        if folder_digest is not None:
            folder_digest.start()
        reminders.start()
        if google is not None:
            # Behind the start: the first question finds the copies already made. A plain
            # task, kept and cancelled at shutdown: post_init runs before the application
            # counts as running, and its own create_task warns about exactly that.
            warming.append(asyncio.create_task(google.warm()))

    async def _post_shutdown(_: Application) -> None:
        for task in warming:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # The loops first: they touch the store on their own timers, so they must be off before
        # the store underneath them closes. Admin next, store last — nothing after this reaches
        # for either.
        await reminders.stop()
        if daily is not None:
            await daily.stop()
        admin.stop()
        if mail_digest is not None:
            await mail_digest.stop()
        if sync_watch is not None:
            await sync_watch.stop()
        if folder_digest is not None:
            await folder_digest.stop()
        if close_folders is not None:
            await close_folders()
        if mail is not None:
            await mail.aclose()
        if vault is not None:
            await vault.aclose()
        await link_reader.aclose()
        await images.aclose()
        if answerer is not None:
            await answerer.aclose()
        reminder_store.close()
        memory.close()
        if google is not None:
            await google.aclose()
        store.close()

    telegram_app.post_init = _post_init
    telegram_app.post_shutdown = _post_shutdown

    app = App(
        settings=settings, store=store, speech=speech, orchestrator=orchestrator, admin=admin,
        telegram_app=telegram_app,
    )
    return app


def _die(code: int, message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(code)


def _error_field_names(e: Exception) -> list[str]:
    """The field names a pydantic ValidationError names, and nothing else. `str(e)` for a
    "missing" error embeds `input_value={...}` — the whole dict of settings that *were* supplied,
    secrets included (pydantic truncates the repr, so what would leak is partial, not complete,
    but a partial token is still a leaked token) — and `e.errors()[i]["input"]` carries the exact
    same dict, untruncated. Only `err["loc"]` (the field path) is ever safe to surface."""
    errors = getattr(e, "errors", None)
    if not callable(errors):
        return []
    try:
        return sorted({".".join(str(p) for p in err.get("loc", ())) for err in errors()})
    except Exception:
        return []


def _config_error_message(e: Exception) -> str:
    """A fixed, token-safe message for a `Settings()` construction failure — never `str(e)`, for
    the reason `_error_field_names` explains."""
    fields = _error_field_names(e)
    where = f" ({', '.join(fields)})" if fields else ""
    return f"configuration error{where}. Fix .env and restart."


def startup_checks(app: App) -> None:
    """The two checks that can (and must) run before any event loop exists: settings
    (`Settings.require_telegram()`, exit 2 — only field *names* ever appear, via
    `_config_error_message`/`_error_field_names`, never a value) and schema
    (`AuditStore.assert_schema_current()`, exit 4, whose message already carries the "run the
    updater" hint). Neither is async. Giving them a throwaway event loop of their own anyway
    would be harmless in isolation, but see `post_init_checks` for why the checks that *are*
    async must not get one."""
    try:
        app.settings.require_telegram()
    except ValueError as e:
        _die(EXIT_CONFIG, f"configuration error: {e}. Fix .env and restart.")

    try:
        app.store.assert_schema_current()
    except MigrationError as e:
        _die(EXIT_PENDING_MIGRATION, str(e))


async def _register_commands(bot: Bot) -> None:
    """Publishes the command menu Telegram shows behind the "/" button. Without this call the
    client offers no menu at all and the user has to remember every command unaided.

    Private chats only: in a group the commands are the owner's business, not the members', so
    a group shows no menu. The default scope (every chat) is cleared, which takes down the menu
    versions before 0.6.49 published there.

    Never fatal: an unreachable Telegram here means the same thing it means anywhere else in this
    function — try again later — and the bot is perfectly usable with commands typed by hand. The
    log line carries the exception's *class* only, never its message: an `InvalidToken` raised
    here would carry the bot token in its text (see `_TELEGRAM_TOKEN_REJECTED`)."""
    commands = [BotCommand(name, description) for name, description in texts.COMMANDS.items()]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except Exception as e:
        log.warning("could not publish the Telegram command menu (%s)", type(e).__name__)


async def post_init_checks(app: App, application: Application) -> None:
    """Everything that needs a running event loop: the Telegram command menu, the admin server.
    Called from `Application.post_init` (wired in `build()`), so it shares PTB's own loop with
    every real call made later — see the module docstring for why that is not optional."""
    if not uses_cloud(app.settings):
        log.warning("no ANTHROPIC_API_KEY: the vault, web research and link answers are off")

    # Whisper (app.speech) is deliberately not touched here: the model loads lazily, off the
    # event loop, on the first voice message (app/speech/whisper_local.py).

    await _register_commands(application.bot)

    if app.settings.admin_ui_port > 0:
        # The admin page is optional by design (ADMIN_UI_PORT=0 turns it off), so losing the port
        # — already taken, or inside a Windows excluded port range, which is a routine WinError
        # 10013 on 8787 — must not take the bot down with it. Unguarded, this OSError would
        # escape post_init, sail past Application.__run's `except (KeyboardInterrupt,
        # SystemExit)` and out of run_polling().
        try:
            app.admin.start()
        except OSError as e:
            log.warning(
                "admin page could not bind 127.0.0.1:%d (%s); continuing without it",
                app.settings.admin_ui_port, e,
            )


def main() -> None:
    try:
        settings = load_settings()
    except Exception as e:  # pydantic ValidationError: a required .env value is missing/invalid
        configure("INFO")
        _die(EXIT_CONFIG, _config_error_message(e))

    # Logging is configured before build() on purpose: build() constructs the ExtBot that logs
    # the token-bearing API URL at DEBUG, so the floor and the redaction must already be in
    # place. The two secret *values* go in here and nowhere else.
    try:
        configure(
            settings.log_level,
            redact=(
                settings.telegram_bot_token.get_secret_value(),
                settings.anthropic_api_key.get_secret_value(),
                settings.gmail_app_password.get_secret_value(),
            ),
            log_file=settings.log_file or None,
        )
    except ValueError as e:
        _die(EXIT_CONFIG, f"configuration error: {e}. Fix .env and restart.")

    # One bot per database: the lock sits next to the file it protects. Taken before build(),
    # which opens that database. `_die` raises SystemExit, so the `finally` still releases it.
    lock = InstanceLock(settings.db_path.parent / "bot.pid")
    try:
        lock.acquire()
    except AlreadyRunning:
        _die(EXIT_ALREADY_RUNNING, _ALREADY_RUNNING)
    try:
        app = build(settings)
        startup_checks(app)  # sync: settings + schema; exits 2/4 directly, no event loop involved
        try:
            app.telegram_app.run_polling()
        except InvalidToken:
            # Telegram refused the token (mistyped, revoked, regenerated). PTB re-raises this out
            # of run_polling() — it is neither KeyboardInterrupt nor SystemExit, so without this
            # catch Python would print the whole traceback, token and all. Nothing about `e` is
            # printed or logged: see _TELEGRAM_TOKEN_REJECTED.
            _die(EXIT_CONFIG, _TELEGRAM_TOKEN_REJECTED)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
