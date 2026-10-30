from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.chats import Chats
from app.config import Settings
from app.google.sources import Sources
from app.mail.buckets import Buckets
from app.reminders.rules import ReminderRules
from app.reminders.store import ReminderStore
from app.switches import Switches
from app.tuning import Tuning
from app.vault.index import VaultIndex
from app.watch.folders import Folders

log = logging.getLogger(__name__)

_PAGE_HTML = (Path(__file__).parent / "page.html").read_bytes()  # read once at import
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# The page's one endpoint: GET reads everything it shows, POST saves what it edits.
API = "/api/settings"
# Generous cap for a hand-edited settings document; also bounds the blocking rfile.read()
# below so a hostile/huge Content-Length can't force an unbounded read.
MAX_BODY_BYTES = 512 * 1024
# How long a rejected request's unread body is waited for before the connection is closed.
DRAIN_WAIT_S = 0.2


def _parse_content_length(raw: str | None) -> int | None:
    """Returns the number of body bytes to read, or None when the header is missing (treated
    as an empty body), non-numeric, negative, or over MAX_BODY_BYTES — the caller must reject
    with 400 without ever calling rfile.read() in the last three cases."""
    if raw is None:
        return 0
    try:
        length = int(raw)
    except ValueError:
        return None
    if length < 0 or length > MAX_BODY_BYTES:
        return None
    return length


def _loopback_host(host_header: str | None) -> bool:
    """DNS-rebinding guard: only accept requests a loopback-only browser tab would send."""
    if not host_header:
        return False
    if host_header.startswith("["):
        host = host_header[1 : host_header.find("]")] if "]" in host_header else host_header
    else:
        host = host_header.split(":", 1)[0]
    return host in _LOOPBACK_HOSTS


def _own_page(headers) -> bool:
    """Is this POST the admin page's own? Binding to loopback keeps other machines out, but a
    page on any other site can have the user's own browser send a POST here, and the socket
    would not know the difference. Two things tell it apart: the page sends JSON — a content
    type another origin can only send after a CORS preflight this server never answers — and a
    browser names the origin a cross-site request comes from."""
    ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return False
    origin = headers.get("Origin")
    if origin is None:
        return True  # not a browser's cross-site request
    netloc = origin.split("://", 1)[-1].split("/", 1)[0]
    return _loopback_host(netloc)


def _reminders_payload(store: ReminderStore, tz: str) -> list[dict]:
    """Every chat's pending reminders, in local time, for the page's list."""
    zone = ZoneInfo(tz)
    return [{"id": r.id, "chat_id": r.chat_id, "text": r.text, "repeat": r.repeat,
             "due": r.due_at.astimezone(zone).strftime("%Y-%m-%d %H:%M")}
            for r in store.pending()]


def _chat_choices(settings: Settings, chats: Chats | None) -> list[dict]:
    """Where a rule may send: each owner's private chat, and the listed groups."""
    out = [{"chat_id": uid, "title": "", "private": True}
           for uid in sorted(settings.allowed_user_ids)]
    if chats is not None:
        out += [{"chat_id": c["chat_id"], "title": c["title"], "private": False}
                for c in chats.all()["chats"]]
    return out


class _AdminHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        settings: Settings,
        switches: Switches | None = None,
        buckets: Buckets | None = None,
        tuning: Tuning | None = None,
        chats: Chats | None = None,
        vault_index: VaultIndex | None = None,
        reminder_rules: ReminderRules | None = None,
        reminders: ReminderStore | None = None,
        sources: Sources | None = None,
        google_status: Callable[[], dict] | None = None,
        folders: Folders | None = None,
        folder_status: Callable[[], list] | None = None,
        sync_status: Callable[[], list] | None = None,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.sources = sources
        # Watched folders and Syncthing's health (app/watch/): the list the page edits, and
        # what the last runs saw.
        self.folders = folders
        self.folder_status = folder_status
        self.sync_status = sync_status
        self.google_status = google_status
        self.reminder_rules = reminder_rules
        self.reminders = reminders
        self.chats = chats
        # The vault's folders and tags, offered when a group chat's scope is chosen.
        self.vault_index = vault_index
        self.app_settings = settings
        self.switches = switches
        self.buckets = buckets
        self.tuning = tuning


class _Handler(BaseHTTPRequestHandler):
    server: _AdminHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:  # route stdlib's default access log
        log.info("%s - %s", self.address_string(), fmt % args)

    def _rejects_host(self) -> bool:
        return not _loopback_host(self.headers.get("Host"))

    def _drop_body(self) -> None:
        """Throw away a body we are not going to read, before answering. Closing a socket that
        still holds unread data makes Windows reset the connection, and the client then sees a
        dropped connection instead of the 400 — the header said "abc" or half a megabyte, so
        there is no length to trust here; take what has arrived and stop."""
        sock = self.connection
        timeout = sock.gettimeout()
        left = MAX_BODY_BYTES
        try:
            sock.settimeout(DRAIN_WAIT_S)
            while left > 0 and sock.recv(min(left, 8192)):
                left -= 8192
        except OSError:
            pass  # nothing waiting, or the client has gone: either way there is nothing to read
        finally:
            with suppress(OSError):
                sock.settimeout(timeout)
        self.close_connection = True

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self._rejects_host():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return
        if self.path == "/":
            self._send_html(HTTPStatus.OK, _PAGE_HTML)
        elif self.path == API:
            payload: dict[str, Any] = {}
            if self.server.switches is not None:
                payload["switches"] = self.server.switches.all()
            payload["obsidian_vault"] = str(self.server.app_settings.obsidian_vault or "")
            if self.server.buckets is not None:
                payload["mail_buckets"] = self.server.buckets.load()
            if self.server.tuning is not None:
                payload["tuning"] = self.server.tuning.all()
            if self.server.chats is not None:
                payload["chats"] = self.server.chats.all()
            if self.server.reminder_rules is not None:
                payload["reminder_rules"] = self.server.reminder_rules.dump()
            if self.server.reminders is not None:
                payload["reminders"] = _reminders_payload(self.server.reminders,
                                                          self.server.app_settings.timezone)
            payload["chat_choices"] = _chat_choices(self.server.app_settings,
                                                    self.server.chats)
            if self.server.folders is not None:
                payload["folders"] = self.server.folders.dump()
                payload["folder_status"] = (self.server.folder_status()
                                            if self.server.folder_status is not None else [])
            # None: Syncthing is not watched on this machine (no config found).
            payload["syncthing"] = (self.server.sync_status()
                                    if self.server.sync_status is not None else None)
            if self.server.sources is not None:
                payload["sources"] = self.server.sources.dump()
                payload["google"] = (self.server.google_status()
                                     if self.server.google_status is not None else {})
            if self.server.vault_index is not None:
                index = self.server.vault_index
                try:
                    index.refresh()
                except OSError as e:
                    log.warning("could not read the vault for the admin page: %s", e)
                payload["vault_folders"] = index.folders()
                payload["vault_tags"] = index.tags()
            self._send_json(HTTPStatus.OK, payload)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self._rejects_host():
            self._drop_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
            return
        if self.path != API:
            # Read (and drop) the body first: closing a socket with unread data makes Windows
            # reset the connection, and the client never sees the 404.
            length = _parse_content_length(self.headers.get("Content-Length"))
            if length:
                self.rfile.read(length)
            else:
                self._drop_body()  # a header we would not read from is dropped the other way
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not _own_page(self.headers):
            log.warning("rejected POST %s: not from the admin page", API)
            self._drop_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_origin"})
            return
        length = _parse_content_length(self.headers.get("Content-Length"))
        if length is None:
            log.warning("rejected POST %s: bad Content-Length header", API)
            self._drop_body()
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            saved = 0
            buckets = body.get("mail_buckets") if isinstance(body, dict) else None
            if buckets is not None:
                if not isinstance(buckets, str):
                    raise ValueError("'mail_buckets' must be a string")
                if self.server.buckets is not None and self.server.buckets.save(buckets):
                    saved += 1
            tuning = body.get("tuning") if isinstance(body, dict) else None
            if tuning is not None:
                if not isinstance(tuning, dict):
                    raise ValueError("'tuning' must be an object")
                if self.server.tuning is not None:
                    saved += self.server.tuning.save(tuning)
            folders = body.get("folders") if isinstance(body, dict) else None
            if folders is not None and self.server.folders is not None:
                saved += self.server.folders.save(folders)  # ValueError: no path
            sources = body.get("sources") if isinstance(body, dict) else None
            if sources is not None and self.server.sources is not None:
                saved += self.server.sources.save(sources)  # ValueError: a bad link
            rules = body.get("reminder_rules") if isinstance(body, dict) else None
            if rules is not None and self.server.reminder_rules is not None:
                saved += self.server.reminder_rules.save(rules)  # ValueError: malformed
            cancel = body.get("cancel_reminders") if isinstance(body, dict) else None
            if cancel is not None:
                if not isinstance(cancel, list) or not all(isinstance(i, int) for i in cancel):
                    raise ValueError("'cancel_reminders' must be a list of ids")
                if self.server.reminders is not None:
                    saved += sum(1 for i in cancel if self.server.reminders.cancel(i))
            chats = body.get("chats") if isinstance(body, dict) else None
            if chats is not None and self.server.chats is not None:
                saved += self.server.chats.save(chats)  # ValueError: a malformed list
            switches = body.get("switches") if isinstance(body, dict) else None
            if switches is not None:
                if not isinstance(switches, dict):
                    raise ValueError("'switches' must be an object")
                if self.server.switches is not None:
                    saved += self.server.switches.save(switches)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
            log.warning("rejected POST %s: %s", API, e)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        self._send_json(HTTPStatus.OK, {"saved": saved})


class AdminServer:
    """The local admin page: loopback-only HTTP server on a daemon thread. It edits the
    files in data/ the bot reads per message: switches, tuning, chats, rules, sources."""

    def __init__(
        self, settings: Settings, switches: Switches | None = None,
        buckets: Buckets | None = None, tuning: Tuning | None = None,
        *, chats: Chats | None = None, vault_index: VaultIndex | None = None,
        reminder_rules: ReminderRules | None = None, reminders: ReminderStore | None = None,
        sources: Sources | None = None, google_status: Callable[[], dict] | None = None,
        folders: Folders | None = None, folder_status: Callable[[], list] | None = None,
        sync_status: Callable[[], list] | None = None,
    ) -> None:
        self._settings = settings
        self._folders = folders
        self._folder_status = folder_status
        self._sync_status = sync_status
        self._sources = sources
        self._google_status = google_status
        self._reminder_rules = reminder_rules
        self._reminders = reminders
        self._chats = chats
        self._vault_index = vault_index
        self._switches = switches
        self._buckets = buckets
        self._tuning = tuning
        self._httpd: _AdminHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("admin server is not started")
        return self._httpd.server_address[1]

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = _AdminHTTPServer(
            ("127.0.0.1", self._settings.admin_ui_port),
            _Handler,
            self._settings,
            self._switches,
            self._buckets,
            self._tuning,
            self._chats,
            self._vault_index,
            self._reminder_rules,
            self._reminders,
            self._sources,
            self._google_status,
            self._folders,
            self._folder_status,
            self._sync_status,
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="admin-http", daemon=True
        )
        self._thread.start()
        log.info("admin page listening on http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None
