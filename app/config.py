from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_user_ids: str = ""

    # A local Ollama, for the mail models that are not Claude (MAIL_MODEL, MAIL_SHADOW_MODEL),
    # and the context window they get.
    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_num_ctx: int = 8192

    # Every model call but the local mail models goes to Claude: without a key the vault, web
    # research and link answers stay off.
    anthropic_api_key: SecretStr = SecretStr("")
    # Answers a question about a linked page, and reads watched folders when
    # VAULT_ANSWER_MODEL is empty.
    claude_model: str = "claude-haiku-4-5"
    # "Find X on the web and write it to Y": Claude with web search. Sonnet digs deeper and
    # writes the better note, for about three times what Haiku costs — a search already costs
    # far more than an ordinary message, and this is the call whose quality you read.
    research_model: str = "claude-sonnet-5"
    research_max_searches: int = Field(4, ge=1, le=20)
    # Site searches and page reads together, per lookup; past it the model is told to answer.
    research_max_reads: int = Field(8, ge=1, le=40)
    # After this the tools are switched off and the model answers with what it found.
    research_soft_deadline_s: float = Field(90.0, ge=15.0, le=900.0)
    # The backstop: only fires when one call hangs. The user is told it was cut short.
    research_deadline_s: float = Field(180.0, ge=30.0, le=1800.0)
    # Optional: Jina Reader works without a key at ~20 pages a minute; a key raises that.
    jina_api_key: SecretStr = SecretStr("")
    # Where an Instagram post is fetched when Instagram refuses (app/media/instagram.py): URL
    # templates, comma-separated, "{id}" the post's code, "{n}" an item of a carousel from 0.
    instagram_mirrors: str = ("https://vxinstagram.com/offload/{id}/{n},"
                              "https://kkinstagram.com/p/{id}/")
    # The second opinion: a vault answer it could not settle, and a watched folder's summary
    # checked before it is sent. Empty: never asked.
    escalate_model: str = "claude-sonnet-5"

    # Rewriting text the user already wrote (a page consolidated into one plan, a note
    # trimmed). Sonnet: this is their own writing, and Haiku loses facts while shortening.
    rewrite_model: str = "claude-sonnet-5"

    # A joke for a group with the "banter" right that calls the bot for nothing. Empty: only the
    # stock lines of app/texts.py.
    banter_model: str = "claude-sonnet-5"

    # What to call the bot. Several spellings separated by commas ("Robo,Robbie"): the
    # name is taken off the front or the back of a message before anything reads it, so
    # "Robo, buy milk" is a shopping item and not a note about someone called Robo.
    bot_name: str = ""

    # The Obsidian vault the bot writes: a folder on this machine. Empty = off.
    obsidian_vault: Path | None = None
    # Syncthing's own config.xml, for its API address and key (app/watch/syncthing.py). Empty:
    # where Syncthing keeps it on Windows, %LOCALAPPDATA%\Syncthing\config.xml.
    syncthing_config: Path | None = None
    # When the bot sends the morning agenda from the vault (HH:MM, in TIMEZONE). Empty: never.
    daily_digest_at: str = "09:00"
    obsidian_enabled: bool = True
    # The vault's own interpreter ("which note is this?") and the linker. Both are small jobs
    # on a small context: Haiku.
    filer_model: str = "claude-haiku-4-5"
    # How the vault reads a message: "single" asks one question about everything at once;
    # "staged" asks intent, then one target, then that target's details (app/vault/staged.py).
    # Staged since 0.6.10: on the 53 real messages where the two disagreed, the user judged
    # staged right 36 times, both fine 10, single right 4, neither 3.
    filer_mode: str = "staged"
    # Asked the same question when the light filer's answer is visibly off (an empty note, an
    # inbox line). "" turns the second reading off.
    filer_strong_model: str = "claude-sonnet-5"
    linker_model: str = "claude-haiku-4-5"
    linker_enabled: bool = True
    # Answers a question about the vault from the notes its search found (app/vault/answer.py).
    # "" turns it off: the reply is then the list of notes found.
    vault_answer_model: str = "claude-haiku-4-5"
    # Obsidian's Omnisearch plugin, asked over its local HTTP server while the app runs (its
    # setting "HTTP server"). 0: never asked.
    omnisearch_port: int = 51361

    # Mail: read-only triage over IMAP with a Gmail app password (no OAuth, no token that
    # expires weekly). The bot only ever reads — there is no code path that marks, deletes or
    # sends. Empty address or password = off.
    gmail_address: str = ""
    gmail_app_password: SecretStr = SecretStr("")
    # When to send the digest, comma-separated wall-clock times in TIMEZONE.
    mail_digest_at: str = "12:00,19:00"
    # "name: what belongs in it", comma-separated on this one line; a description may contain
    # commas, since a new bucket starts only where a "name:" follows one. The description is
    # what the classifier follows — a bare name leaves it guessing. Empty falls back to
    # texts.MAIL_BUCKETS_DEFAULT.
    mail_buckets: str = ""
    # claude-... sorts through the API; any other name is a local Ollama model (no key needed).
    mail_model: str = "claude-haiku-4-5"
    mail_max_per_run: int = Field(40, ge=1, le=200)
    # Local Ollama models, comma-separated, that each sort the same mail again, so their
    # digests can be read side by side with the real one — one message each, in this order.
    # Empty is off, which is the normal state: it is a trial, not a feature. They run one after
    # another and each is unloaded when done, so they never share the card.
    mail_shadow_model: str = ""
    # How many emails to try in one local request. More at once sorts better (the model tells
    # buckets apart by contrast); a request that overflows the context is split and retried.
    mail_shadow_batch: int = Field(20, ge=1, le=200)

    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_language: str = "ru"

    timezone: str = "UTC"
    locale: str = "ru"

    db_path: Path = Path("data/bot.sqlite")
    admin_ui_port: int = 8787

    undo_window_s: int = 300
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"  # rotating, redacted like stderr; empty disables it

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _allowlist_digits(cls, v: str) -> str:
        for p in (x.strip() for x in v.split(",") if x.strip()):
            if not p.isdigit():
                raise ValueError(f"bad user id: {p!r}")
        return v

    def require_telegram(self) -> None:
        """Raise a clear error when the bot itself is started without Telegram settings."""
        missing = []
        if not self.telegram_bot_token.get_secret_value():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.allowed_user_ids:
            missing.append("TELEGRAM_ALLOWED_USER_IDS")
        if missing:
            raise ValueError("missing in .env: " + ", ".join(missing))

    @cached_property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(int(p) for p in self.telegram_allowed_user_ids.split(",") if p.strip())

    @cached_property
    def mail_shadow_models(self) -> list[str]:
        return [m.strip() for m in self.mail_shadow_model.split(",") if m.strip()]


def load_settings() -> Settings:
    return Settings()
