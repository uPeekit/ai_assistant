"""Obsidian's own search (the Omnisearch plugin), asked over its local HTTP server.

It is a second opinion next to app/vault/search.py, not a replacement. Measured on the user's
vault, each finds what the other misses: ours reads Russian word forms (the genitive of an
author's name finds the author's note, "Hannibal meeting" the meetings), where Omnisearch
needs every word as written and found nothing; Omnisearch forgives spelling ("projects" found
the note whose name misspells it) and has read the pictures (its image analyzer and text
extractor), which ours cannot. So the answerer is given ours first and Omnisearch's extra
finds after them.

It works only while Obsidian runs on this computer with the plugin's server switched on; any
failure is an empty answer, and the bot's own search carries on alone."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from app import texts
from app.vault.index import GUIDE_NOTE, VaultIndex

log = logging.getLogger(__name__)

TIMEOUT_S = 2.0
MAX_WORDS = 5  # one request per word: the server wants every word of a query in a note
MIN_WORD = 3
# Extra notes, after ours: its unique finds are few, and each one is text the answerer reads.
MAX_FOUND = 3
_WORD = re.compile(r"[^\W_]+")


class OmniSearch:
    def __init__(self, port: int, *, host: str = "localhost",
                 timeout_s: float = TIMEOUT_S) -> None:
        # "localhost", not 127.0.0.1: the plugin listens on the IPv6 loopback only.
        self._base = f"http://{host}:{port}/search?q="
        self._timeout = timeout_s
        self._down = False  # said once in the log, not on every question

    def _ask(self, query: str) -> list[dict]:
        try:
            with urllib.request.urlopen(self._base + urllib.parse.quote(query),
                                        timeout=self._timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as e:
            if not self._down:
                log.info("Omnisearch is not answering (%s); the bot's own search alone",
                         type(e).__name__)
            self._down = True
            return []
        if self._down:
            log.info("Omnisearch is answering again")
        self._down = False
        return [d for d in data if isinstance(d, dict) and isinstance(d.get("path"), str)] \
            if isinstance(data, list) else []

    def find(self, query: str, index: VaultIndex) -> list[str]:
        """Vault paths of the notes Omnisearch ranks for these words, best first. A picture
        it found stands for the notes that show it."""
        wanted = [w for w in dict.fromkeys(_WORD.findall(query.casefold()))
                  if len(w) >= MIN_WORD and w not in texts.SEARCH_STOP_WORDS][:MAX_WORDS]
        scores: Counter = Counter()
        for word in wanted:
            results = self._ask(word)
            if self._down:
                return []
            for result in results:
                scores[result["path"]] += float(result.get("score") or 0)
        # The user's guide to the bot is about the bot, not about anything asked.
        notes = {n.path for n in index.notes if n.name != GUIDE_NOTE}
        out: list[str] = []
        for path, _ in scores.most_common():
            found = [path] if path in notes else [
                p for p in _showing(index, path) if p in notes]
            out += [p for p in found if p not in out]
            if len(out) >= MAX_FOUND:
                break
        return out[:MAX_FOUND]


def _showing(index: VaultIndex, attachment: str) -> list[str]:
    """The notes that embed or link this file: what its picture is part of."""
    name = attachment.rsplit("/", 1)[-1]
    out = []
    for note in index.notes:
        try:
            text = index.read(note.path)
            if f"[[{name}" in text or f"/{name}" in text:
                out.append(note.path)
        except OSError:
            continue
    return out
