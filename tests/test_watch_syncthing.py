"""Syncthing health (app/watch/syncthing.py): what is checked, and when the owner is told."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from app import texts
from app.watch.syncthing import Observation, SyncthingApi, SyncthingHealth, load_api

CONFIG = """<configuration version="37">
    <folder id="notes-1" label="Notes" path="C:\\notes" paused="false"></folder>
    <folder id="old-2" label="Old" path="C:\\old" paused="true"></folder>
    <device id="PHONE-ID" name="Phone"><paused>false</paused></device>
    <device id="TAB-ID" name="Tablet"><paused>true</paused></device>
    <device id="SELF-ID" name="Desktop"></device>
    <gui enabled="true" tls="false"><address>127.0.0.1:8384</address>
        <apikey>secret-key</apikey></gui>
</configuration>
"""
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def test_a_config_gives_the_api_address_key_devices_and_folders(tmp_path):
    path = tmp_path / "config.xml"
    path.write_text(CONFIG, encoding="utf-8")

    api = load_api(path, own_id="SELF-ID")

    assert api.base_url == "http://127.0.0.1:8384" and api.api_key == "secret-key"
    assert api.devices == {"PHONE-ID": "Phone", "TAB-ID": "Tablet"}
    assert api.folders == {"notes-1": "Notes", "old-2": "Old"}
    assert api.paused == {"TAB-ID", "old-2"}
    assert load_api(tmp_path / "missing.xml") is None


def _api(handler) -> SyncthingApi:
    return SyncthingApi("http://st", "k", {"PHONE-ID": "Phone"}, {"notes-1": "Notes"}, set(),
                        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_the_api_is_asked_with_its_key_and_read_into_an_observation():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-API-Key"))
        path = request.url.path
        if path == "/rest/system/ping":
            return httpx.Response(200, json={"ping": "pong"})
        if path == "/rest/system/connections":
            return httpx.Response(200, json={"connections": {"PHONE-ID": {"connected": False}}})
        if path == "/rest/stats/device":
            return httpx.Response(200, json={"PHONE-ID": {"lastSeen": "2026-10-18T09:00:00Z"}})
        return httpx.Response(200, json={"errors": 2, "pullErrors": 0, "state": "idle"})

    obs = await _api(handler).observe()

    assert obs.running and obs.connected == {"PHONE-ID": False}
    assert obs.last_seen["PHONE-ID"] == datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    assert obs.folder_errors == {"notes-1": 2}
    assert set(seen) == {"k"}


async def test_api_down_is_not_running():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert (await _api(handler).observe()).running is False


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _health(tmp_path, clock) -> SyncthingHealth:
    return SyncthingHealth(tmp_path / "sync.json", after=lambda: timedelta(minutes=60),
                           clock=clock, zone="UTC")


NAMES = ({"PHONE-ID": "Phone"}, {"notes-1": "Notes"})


def up(connected=True, errors=0) -> Observation:
    return Observation(True, {"PHONE-ID": connected},
                       {"PHONE-ID": NOW - timedelta(hours=5)}, {"notes-1": errors})


def test_pc_down_is_announced_once_and_its_return_once(tmp_path):
    health = _health(tmp_path, Clock())
    down = Observation(False, {}, {}, {})

    assert health.update(down, *NAMES) == [texts.SYNC_PC_DOWN]
    assert health.update(down, *NAMES) == []
    assert health.update(up(), *NAMES) == [texts.SYNC_PC_BACK]


def test_a_device_alerts_only_after_the_threshold_of_the_bots_own_observation(tmp_path):
    """Syncthing's last-seen was five hours ago (the PC slept); the clock starts when the bot
    itself first sees the device away."""
    clock = Clock()
    health = _health(tmp_path, clock)

    assert health.update(up(connected=False), *NAMES) == []
    clock.t += 59 * 60
    assert health.update(up(connected=False), *NAMES) == []
    clock.t += 2 * 60
    [gone] = health.update(up(connected=False), *NAMES)
    assert gone == texts.SYNC_DEVICE_GONE.format(name="Phone", minutes=60, seen="18.10 07:00")
    assert health.update(up(connected=False), *NAMES) == []
    assert health.update(up(), *NAMES) == [texts.SYNC_DEVICE_BACK.format(name="Phone")]


def test_a_device_back_before_the_threshold_was_never_news(tmp_path):
    clock = Clock()
    health = _health(tmp_path, clock)
    health.update(up(connected=False), *NAMES)
    clock.t += 10 * 60
    assert health.update(up(), *NAMES) == []


def test_a_paused_device_or_folder_never_alerts(tmp_path):
    clock = Clock()
    health = _health(tmp_path, clock)
    obs = Observation(True, {"PHONE-ID": False}, {}, {"notes-1": 3}, paused={"PHONE-ID",
                                                                              "notes-1"})
    health.update(obs, *NAMES)
    clock.t += 3 * 3600
    assert health.update(obs, *NAMES) == []


def test_a_restart_while_down_does_not_announce_again(tmp_path):
    down = Observation(False, {}, {}, {})
    assert _health(tmp_path, Clock()).update(down, *NAMES) == [texts.SYNC_PC_DOWN]
    again = _health(tmp_path, Clock())
    assert again.update(down, *NAMES) == []
    assert again.update(up(), *NAMES) == [texts.SYNC_PC_BACK]


def test_while_the_pc_is_down_the_devices_are_not_judged(tmp_path):
    """Nothing is known about the phone while the API is down: no alert about it, and its clock
    does not run on."""
    clock = Clock()
    health = _health(tmp_path, clock)
    health.update(Observation(False, {}, {}, {}), *NAMES)
    clock.t += 3 * 3600
    assert health.update(Observation(False, {}, {}, {}), *NAMES) == []
    assert health.update(up(connected=False), *NAMES) == [texts.SYNC_PC_BACK]


def test_folder_errors_alert_and_clear(tmp_path):
    health = _health(tmp_path, Clock())
    assert health.update(up(errors=2), *NAMES) == [
        texts.SYNC_FOLDER_ERRORS.format(name="Notes", n=2)]
    assert health.update(up(errors=2), *NAMES) == []
    assert health.update(up(), *NAMES) == [texts.SYNC_FOLDER_OK.format(name="Notes")]


def test_status_rows_name_each_device_and_folder(tmp_path):
    health = _health(tmp_path, Clock())
    health.update(up(connected=False, errors=1), *NAMES)

    rows = {r["name"]: r for r in health.status()}
    assert rows["Phone"]["ok"] is False and rows["Notes"]["ok"] is False
    assert rows[texts.SYNC_THIS_PC]["ok"] is True
