"""The watchers inside the bot (app/main.py): when they run, and who hears from them."""

from __future__ import annotations

from types import SimpleNamespace

from app import main, texts
from app.config import Settings
from app.switches import Switches
from app.tuning import Defaults, Tuning
from app.watch.folders import Folders

DEFAULTS = Defaults(bot_name="", agenda_at="09:00", mail_at="12:00", web_words=(),
                    countdown_tag="", date_props=())


def _tuning(tmp_path) -> Tuning:
    return Tuning(tmp_path / "tuning.json", DEFAULTS)


def test_tuning_sync_after_defaults_to_60_and_is_editable(tmp_path):
    tuning = _tuning(tmp_path)
    assert tuning.sync_after == 60 and tuning.all()["sync_after"] == "60"
    tuning.save({"sync_after": "15"})
    assert tuning.sync_after == 15
    tuning.save({"sync_after": "nonsense"})
    assert tuning.sync_after == 60  # not a number: the default


def _switches(tmp_path) -> Switches:
    return Switches(tmp_path / "switches.json", {"syncthing": True, "folders": True})


def test_no_syncthing_config_means_no_watch(env, tmp_path):
    env.setenv("SYNCTHING_CONFIG", str(tmp_path / "missing.xml"))
    watch, health = main._syncthing_watch(Settings(), _switches(tmp_path), _tuning(tmp_path),
                                          lambda: None)
    assert watch is None and health is None


async def test_syncthing_down_is_sent_to_each_owner_and_never_twice(env, tmp_path, monkeypatch):
    """A config whose API nobody answers: Syncthing is not running on this PC."""
    config = tmp_path / "config.xml"
    config.write_text('<configuration><gui tls="false"><address>127.0.0.1:1</address>'
                      "<apikey>k</apikey></gui></configuration>", encoding="utf-8")
    env.setenv("SYNCTHING_CONFIG", str(config))
    env.setenv("DB_PATH", str(tmp_path / "bot.sqlite"))
    sent = []

    async def send_text(bot, chat_id, text, markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(main, "send_text", send_text)
    application = SimpleNamespace(bot=object())
    watch, health = main._syncthing_watch(Settings(), _switches(tmp_path), _tuning(tmp_path),
                                          lambda: application)

    await watch._flush()
    await watch._flush()

    assert sent == [(1, texts.SYNC_PC_DOWN), (2, texts.SYNC_PC_DOWN)]
    assert health.status()[0]["ok"] is False


async def test_syncthing_switched_off_checks_nothing(env, tmp_path, monkeypatch):
    config = tmp_path / "config.xml"
    config.write_text('<configuration><gui><address>127.0.0.1:1</address><apikey>k</apikey>'
                      "</gui></configuration>", encoding="utf-8")
    env.setenv("SYNCTHING_CONFIG", str(config))
    env.setenv("DB_PATH", str(tmp_path / "bot.sqlite"))
    switches = _switches(tmp_path)
    switches.save({"syncthing": False})
    sent = []

    async def send_text(bot, chat_id, text, markup=None):
        sent.append(text)

    monkeypatch.setattr(main, "send_text", send_text)
    watch, _ = main._syncthing_watch(Settings(), switches, _tuning(tmp_path),
                                     lambda: SimpleNamespace(bot=object()))
    await watch._flush()
    assert sent == []


def test_watched_folders_need_a_claude_key(env, tmp_path):
    digest, watch, close = main._folder_digest(Settings(), _switches(tmp_path),
                                               _tuning(tmp_path),
                                               Folders(tmp_path / "folders.json"), lambda: None)
    assert digest is None and watch is None and close is None


def test_watched_folders_run_at_the_mail_times_even_without_mail(env, tmp_path):
    env.setenv("ANTHROPIC_API_KEY", "sk-test")
    env.setenv("DB_PATH", str(tmp_path / "bot.sqlite"))
    digest, watch, close = main._folder_digest(Settings(), _switches(tmp_path),
                                               _tuning(tmp_path),
                                               Folders(tmp_path / "folders.json"), lambda: None)
    assert digest is not None and watch is not None
    assert [t.strftime("%H:%M") for t in digest._times] == ["12:00"]


def test_an_empty_syncthing_setting_means_the_windows_default(env, tmp_path):
    """`SYNCTHING_CONFIG=` copied from .env.example must not turn the check off."""
    (tmp_path / "Syncthing").mkdir()
    (tmp_path / "Syncthing" / "config.xml").write_text(
        "<configuration><gui><address>127.0.0.1:1</address><apikey>k</apikey></gui>"
        "</configuration>", encoding="utf-8")
    env.setenv("LOCALAPPDATA", str(tmp_path))
    env.setenv("SYNCTHING_CONFIG", "")
    watch, _ = main._syncthing_watch(Settings(), _switches(tmp_path), _tuning(tmp_path),
                                     lambda: None)
    assert watch is not None
