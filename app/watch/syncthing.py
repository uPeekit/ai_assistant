"""Syncthing health: is the vault still syncing between this PC and the owner's devices?

The vault reaches this PC through Syncthing. When it stops — Syncthing not running here, or a
phone that has not connected for a day — the bot keeps reading a stale vault and nobody notices.
This reads Syncthing's own REST API on this machine (GET only), with the address and API key from
its `config.xml`, and turns what it sees into one message when something breaks and one when it
is back. Nothing about the owner's devices is in code: their names come from that config.

    PC not answering          -> told at the next check
    a device not connected    -> told once the bot itself has seen it away for `after`
                                  (Syncthing's own last-seen would make a PC waking from sleep
                                  report a phone that was there all along)
    a folder with errors      -> told at the next check
    a paused device or folder -> never

What was told is kept in a small JSON file, so a restart while something is down does not say
it again."""

from __future__ import annotations

import json
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app import texts

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0
PC = "pc"


@dataclass(frozen=True)
class Observation:
    running: bool
    connected: dict[str, bool] = field(default_factory=dict)
    last_seen: dict[str, datetime | None] = field(default_factory=dict)
    folder_errors: dict[str, int] = field(default_factory=dict)
    paused: frozenset[str] | set[str] = frozenset()


class SyncthingApi:
    def __init__(self, base_url: str, api_key: str, devices: dict[str, str],
                 folders: dict[str, str], paused: set[str], *,
                 http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.devices = devices
        self.folders = folders
        self.paused = paused
        self._http = http

    async def observe(self) -> Observation:
        """What Syncthing says now; `running=False` when it does not answer at all."""
        http = self._http or httpx.AsyncClient(timeout=TIMEOUT_S)
        try:
            get = lambda path, **params: self._get(http, path, params)  # noqa: E731
            await get("/rest/system/ping")
            me = (await get("/rest/system/status")).get("myID", "")
            connections = (await get("/rest/system/connections")).get("connections", {})
            stats = await get("/rest/stats/device")
            errors = {}
            for folder in self.folders:
                if folder in self.paused:
                    continue
                status = await get("/rest/db/status", folder=folder)
                errors[folder] = int(status.get("errors") or 0) + int(
                    status.get("pullErrors") or 0)
        except (httpx.HTTPError, ValueError) as e:
            log.info("syncthing: the API did not answer (%s)", type(e).__name__)
            return Observation(False)
        finally:
            if self._http is None:
                await http.aclose()
        devices = [d for d in self.devices if d != me]
        return Observation(
            True,
            connected={d: bool((connections.get(d) or {}).get("connected")) for d in devices},
            last_seen={d: _when((stats.get(d) or {}).get("lastSeen")) for d in devices},
            folder_errors=errors, paused=set(self.paused))

    async def _get(self, http: httpx.AsyncClient, path: str, params: dict) -> dict:
        resp = await http.get(f"{self.base_url}{path}", params=params or None,
                              headers={"X-API-Key": self.api_key})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or value.startswith("1970"):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def load_api(config_path: Path, own_id: str = "") -> SyncthingApi | None:
    """The API as Syncthing's own config describes it; None without a readable config."""
    try:
        root = ET.parse(config_path).getroot()
    except (OSError, ET.ParseError):
        return None
    gui = root.find("gui")
    address = (gui.findtext("address") if gui is not None else "") or ""
    key = (gui.findtext("apikey") if gui is not None else "") or ""
    if not address or not key:
        return None
    scheme = "https" if gui is not None and gui.get("tls") == "true" else "http"
    devices, paused = {}, set()
    for device in root.findall("device"):
        device_id = device.get("id", "")
        if not device_id or device_id == own_id:
            continue
        devices[device_id] = device.get("name") or device_id[:7]
        if (device.findtext("paused") or "").strip() == "true":
            paused.add(device_id)
    folders = {}
    for folder in root.findall("folder"):
        folder_id = folder.get("id", "")
        if folder_id:
            folders[folder_id] = folder.get("label") or folder_id
            if folder.get("paused") == "true":
                paused.add(folder_id)
    if address.startswith((":", "0.0.0.0")):
        address = "127.0.0.1" + address[address.index(":"):]
    return SyncthingApi(f"{scheme}://{address}", key, devices, folders, paused)


class SyncthingHealth:
    """Observations in, messages out: each problem told once, and its end told once."""

    def __init__(self, state_path: Path, *, after: Callable[[], timedelta],
                 clock: Callable[[], float] = time.time, zone: str = "UTC") -> None:
        self._path = state_path
        self._after = after
        self._clock = clock
        self._zone = ZoneInfo(zone)
        self._last: Observation | None = None
        self._names: tuple[dict[str, str], dict[str, str]] = ({}, {})
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            self._state: dict[str, dict] = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self._state = {}

    def update(self, obs: Observation, devices: dict[str, str],
               folders: dict[str, str]) -> list[str]:
        now = self._clock()
        self._last, self._names = obs, (devices, folders)
        bad: dict[str, str] = {}  # problem -> the message telling it
        good: dict[str, str] = {}  # problem -> the message telling its end
        if not obs.running:
            bad[PC] = texts.SYNC_PC_DOWN
            # Nothing is known about the devices meanwhile: a clock not yet told about starts
            # again when Syncthing is back, instead of running on through the outage.
            for key in [k for k, e in self._state.items() if k != PC and not e.get("told")]:
                del self._state[key]
        else:
            good[PC] = texts.SYNC_PC_BACK
            for device, connected in obs.connected.items():
                key, name = f"device:{device}", devices.get(device, device[:7])
                if device in obs.paused:
                    self._state.pop(key, None)
                    continue
                if connected:
                    good[key] = texts.SYNC_DEVICE_BACK.format(name=name)
                else:
                    bad[key] = texts.SYNC_DEVICE_GONE.format(
                        name=name, minutes=int(self._after().total_seconds() // 60),
                        seen=self._seen(obs.last_seen.get(device)))
            for folder, n in obs.folder_errors.items():
                key, name = f"folder:{folder}", folders.get(folder, folder)
                if folder in obs.paused:
                    self._state.pop(key, None)
                    continue
                if n:
                    bad[key] = texts.SYNC_FOLDER_ERRORS.format(name=name, n=n)
                else:
                    good[key] = texts.SYNC_FOLDER_OK.format(name=name)
        out = []
        for key, message in bad.items():
            entry = self._state.setdefault(key, {"since": now, "told": False})
            wait = self._after().total_seconds() if key.startswith("device:") else 0
            if not entry["told"] and now - entry["since"] >= wait:
                entry["told"] = True
                out.append(message)
        for key, message in good.items():
            entry = self._state.pop(key, None)
            if entry and entry.get("told"):
                out.append(message)
        self._save()
        return out

    def _seen(self, when: datetime | None) -> str:
        return when.astimezone(self._zone).strftime("%d.%m %H:%M") if when else "—"

    def status(self) -> list[dict]:
        """For the admin page: this PC, then every device and folder, as last seen."""
        obs = self._last
        if obs is None:
            return []
        devices, folders = self._names
        rows = [{"name": texts.SYNC_THIS_PC, "ok": obs.running,
                 "detail": texts.SYNC_RUNNING if obs.running else texts.SYNC_NOT_RUNNING}]
        for device, connected in obs.connected.items():
            rows.append({"name": devices.get(device, device[:7]),
                         "ok": connected or device in obs.paused,
                         "detail": texts.SYNC_PAUSED if device in obs.paused else
                         texts.SYNC_CONNECTED if connected else
                         texts.SYNC_LAST_SEEN.format(seen=self._seen(obs.last_seen.get(device)))})
        for folder, n in obs.folder_errors.items():
            rows.append({"name": folders.get(folder, folder), "ok": not n,
                         "detail": texts.SYNC_ERRORS.format(n=n) if n else texts.SYNC_IN_ORDER})
        return rows

    def _save(self) -> None:
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("could not keep the Syncthing state in %s: %s", self._path, e)
