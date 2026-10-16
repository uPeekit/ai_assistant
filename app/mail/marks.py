"""Which letters each sent mail digest covered, so its «mark as read» button marks exactly those.

A small JSON file next to the database, like the mail state: a short id per digest (it goes in
the button's callback data, which Telegram caps at 64 bytes), the uids, and the mailbox
generation (UIDVALIDITY) they belong to. Digests older than KEEP_DAYS are dropped — nobody
clears a fortnight-old digest, and a uid that old may since have been renumbered anyway."""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_DAYS = 14
MAX_DIGESTS = 100


class DigestMarks:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("could not save the mail digests: %s", e)

    def add(self, uids: list[str], validity: str) -> str:
        """Remember one digest's letters; returns its id."""
        now = self._clock()
        data = {k: v for k, v in self._load().items()
                if now - float(v.get("at", 0)) < KEEP_DAYS * 86400}
        digest_id = secrets.token_hex(4)
        data[digest_id] = {"uids": list(uids), "validity": validity, "at": now}
        if len(data) > MAX_DIGESTS:
            data = dict(sorted(data.items(), key=lambda kv: kv[1]["at"])[-MAX_DIGESTS:])
        self._save(data)
        return digest_id

    def get(self, digest_id: str) -> tuple[list[str], str] | None:
        entry = self._load().get(digest_id)
        if not entry or self._clock() - float(entry.get("at", 0)) >= KEEP_DAYS * 86400:
            return None
        return [str(u) for u in entry.get("uids", [])], str(entry.get("validity", ""))
