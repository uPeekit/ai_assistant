"""Security tests: what an unauthorised Telegram user can do to this pipeline, and where the
secrets may turn up.

Everything is the real `main.build()` app: the real `app.telegram.handlers`/`app.telegram.auth`
gate, a real on-disk `AuditStore`, the real `Settings` holding both secrets as `SecretStr`s, the
same components a live deployment runs. No test touches the network, Telegram or a real Whisper
model: speech is `FakeSpeech` (`tests/test_handlers.py`), there is no vault key, and dispatch
walks the real registered handlers the way `tests/test_handlers.py:dispatch` does — never
`Application.process_update`, which would call `Bot.get_me()`.

Two things must hold, whatever a message says:
  * the Telegram bot token never appears in a reply, an audit row, or a log record at any level;
  * a user outside `TELEGRAM_ALLOWED_USER_IDS` gets no reply and earns no audit row at all.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from app import logging_setup, main
from app.config import Settings
from tests.test_handlers import (
    ALLOWED_USER,
    DENIED_USER,
    FakeBot,
    FakeSpeech,
    command_update,
    dispatch,
    text_update,
)
from tests.test_logging import _reset_root_and_third_party

# An obviously fake secret, chosen so a substring match can never be an accident.
TELEGRAM_SECRET = "918273645:AA-TELEGRAM-SECRET-f00dcafe1234567890ab"


def _build_full_app(env) -> main.App:
    env.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_SECRET)
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", f"{ALLOWED_USER},2")
    env.setenv("ADMIN_UI_PORT", "0")
    app = main.build(Settings(), speech_factory=lambda s: FakeSpeech())
    app.store.migrate()
    return app


def _events(app: main.App) -> list[dict]:
    con = sqlite3.connect(app.settings.db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM events ORDER BY rowid")]
    finally:
        con.close()


def _context(app: main.App, fbot: FakeBot) -> SimpleNamespace:
    return SimpleNamespace(bot_data=app.telegram_app.bot_data, bot=fbot, error=None)


async def test_unauthorized_user_gets_no_reply_and_no_audit_row(env):
    app = _build_full_app(env)
    fbot = FakeBot()
    update = text_update(DENIED_USER, "купи молоко", fbot)

    dispatched = await dispatch(app.telegram_app, update, _context(app, fbot))

    assert dispatched is False
    assert fbot.sent == []
    assert _events(app) == []


async def test_authorized_user_control_gets_a_reply_and_a_row(env):
    """Positive control for the test above: without it, a bug that gated *every* user (not just
    unauthorised ones) would pass the unauthorised-user test for the wrong reason."""
    app = _build_full_app(env)
    fbot = FakeBot()
    update = text_update(ALLOWED_USER, "купи молоко", fbot)

    dispatched = await dispatch(app.telegram_app, update, _context(app, fbot))

    assert dispatched is True
    assert len(fbot.sent) == 1
    assert len(_events(app)) == 1


async def test_the_token_does_not_leak_into_a_reply_or_an_audit_row(env):
    app = _build_full_app(env)
    fbot = FakeBot()
    context = _context(app, fbot)

    await dispatch(app.telegram_app, text_update(ALLOWED_USER, "купи молоко", fbot), context)
    await dispatch(app.telegram_app, command_update(ALLOWED_USER, "undo", fbot), context)

    assert len(fbot.sent) == 2
    for sent in fbot.sent:
        assert TELEGRAM_SECRET not in sent["text"]
    for event in _events(app):
        assert TELEGRAM_SECRET not in json.dumps(event, ensure_ascii=False, default=str)


async def test_log_capture_over_text_and_undo_has_no_token_and_no_message_above_debug(
    env, capsys
):
    """One full flow under the real `app.logging_setup.configure("DEBUG")` — an operator's
    `LOG_LEVEL=DEBUG` in `.env` — which is what makes this test worth having: at DEBUG,
    `Application.builder().token(...).build()` (constructing PTB's own `ExtBot`) logs
    `"Set Bot API URL: https://api.telegram.org/bot<TOKEN>"` straight to stderr from its own
    constructor, a leak `httpx`/`httpcore` silencing alone does not cover (`ExtBot` is not an
    httpx request) — see `app/logging_setup.py`'s module docstring for the full story. Then: an
    unauthorised attempt, a text message, and `/undo`.

    `capsys` (real stderr), not `caplog`, is deliberate: `configure()` installs its own
    `StreamHandler` on the root logger, replacing whatever was there — calling it while relying on
    `caplog`'s handler would tear that handler out from under the fixture. `tests/test_logging.py`
    already established this exact pattern (`test_configure_silences_httpx_and_httpcore_info_
    logging`); `_reset_root_and_third_party` (same module) restores global logging state in a
    `finally`, so this test's `configure()` call cannot bleed into any test that runs after it.

    The denied-user attempt gives the capture at least one real record — the `AUTH_DENIED`
    warning — to scan; without it this test could vacuously pass over empty output."""
    message_text = "купи молоко"
    logging_setup.configure("DEBUG")
    try:
        app = _build_full_app(env)  # constructs ExtBot under DEBUG
        fbot = FakeBot()
        context = _context(app, fbot)

        await dispatch(app.telegram_app, text_update(DENIED_USER, message_text, fbot), context)
        await dispatch(app.telegram_app, text_update(ALLOWED_USER, message_text, fbot), context)
        await dispatch(app.telegram_app, command_update(ALLOWED_USER, "undo", fbot), context)
        err = capsys.readouterr().err
    finally:
        _reset_root_and_third_party()

    assert err  # the test would prove nothing over empty output
    assert "AUTH_DENIED" in err
    assert TELEGRAM_SECRET not in err
    for line in err.splitlines():
        if not line.strip():
            continue
        level_name = line.split()[2]  # FORMAT: "<date> <time> <LEVEL> <name> [event=..] <msg>"
        if level_name != "DEBUG":
            assert message_text not in line
