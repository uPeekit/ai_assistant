import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings


def test_defaults_load(env):
    s = load_settings()
    assert s.undo_window_s == 300
    assert s.allowed_user_ids == frozenset({1, 2})
    assert s.timezone == "UTC"


def test_allowlist_parsing(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", " 10, 20 ,30 ")
    assert Settings(_env_file=None).allowed_user_ids == frozenset({10, 20, 30})


def test_empty_allowlist_loads_but_require_telegram_fails(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", " ")
    env.delenv("TELEGRAM_BOT_TOKEN")
    s = Settings(_env_file=None)
    assert s.allowed_user_ids == frozenset()
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS"):
        s.require_telegram()
    ok = Settings(_env_file=None, telegram_bot_token="t", telegram_allowed_user_ids="5")
    ok.require_telegram()


def test_bad_user_id_fails(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,abc")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_tokens_are_not_exposed_in_repr_or_str(env):
    s = Settings(_env_file=None)
    assert "ntn-test-token" not in repr(s)
    assert "ntn-test-token" not in str(s)
    assert "tg-test-token" not in repr(s)
    assert "tg-test-token" not in str(s)


def test_research_budget_defaults(env):
    s = Settings(_env_file=None)
    assert (s.research_max_searches, s.research_max_reads) == (4, 8)
    assert (s.research_soft_deadline_s, s.research_deadline_s) == (90.0, 180.0)
    assert s.jina_api_key.get_secret_value() == ""


def test_a_settings_file_from_before_the_notion_removal_still_loads(env, tmp_path):
    """Prod's .env keeps lines nothing reads any more; they must not stop the bot."""
    (tmp_path / ".env").write_text(
        "NOTION_TOKEN=ntn-old\nNOTION_VERSION=2025-09-03\nLLM_MODEL=mistral-nemo:12b\n"
        "INBOX_MODE=auto\nPOLICY_TARGET_MIN=0.85\n", encoding="utf-8")
    assert not hasattr(Settings(), "notion_token")
