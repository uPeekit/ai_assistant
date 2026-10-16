"""Omnisearch as a second opinion (app/vault/omnisearch.py): asked one word at a time over its
local HTTP server, a picture standing for the notes that show it, and silence when Obsidian is
closed."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.vault.index import VaultIndex
from app.vault.omnisearch import OmniSearch
from app.vault.search import search


@pytest.fixture
def index(tmp_path):
    (tmp_path / "Области").mkdir()
    (tmp_path / "Вложения").mkdir()
    (tmp_path / "Области/Софт.md").write_text("# Garden\n\nидеи\n", encoding="utf-8")
    (tmp_path / "Области/Проекты.md").write_text("# telega\n", encoding="utf-8")
    (tmp_path / "Встреча.md").write_text("![[Вложения/постер 1.png]]\n", encoding="utf-8")
    (tmp_path / "_bot.md").write_text("проекты\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


@pytest.fixture
def server():
    """A stand-in for the plugin's server: answers by the word asked, records the words."""
    answers: dict[str, list[dict]] = {}
    asked: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)["q"][0]
            asked.append(q)
            body = json.dumps(answers.get(q, [])).encode("utf-8")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], answers, asked
    httpd.shutdown()


def test_each_word_is_asked_alone_and_the_scores_add_up(index, server):
    port, answers, asked = server
    answers["проекты"] = [{"path": "Области/Проекты.md", "score": 5},
                          {"path": "_bot.md", "score": 9}]  # the guide is never an answer
    answers["идеи"] = [{"path": "Области/Софт.md", "score": 3},
                       {"path": "Области/Проекты.md", "score": 1}]
    found = OmniSearch(port).find("какие проекты и идеи", index)
    assert asked == ["проекты", "идеи"]  # a question word is not asked about
    assert found == ["Области/Проекты.md", "Области/Софт.md"]


def test_a_picture_it_found_stands_for_the_note_that_shows_it(index, server):
    port, answers, _ = server
    answers["постер"] = [{"path": "Вложения/постер 1.png", "score": 4}]
    assert OmniSearch(port).find("постер", index) == ["Встреча.md"]


def test_with_obsidian_closed_it_finds_nothing_and_says_so_once(index, caplog):
    omni = OmniSearch(1, timeout_s=0.5)  # nothing listens there
    with caplog.at_level("INFO"):
        assert omni.find("проекты идеи", index) == []
        assert omni.find("проекты", index) == []
    assert sum("Omnisearch is not answering" in r.message for r in caplog.records) == 1


def test_our_search_finds_a_note_by_its_heading(index):
    """«# Garden» names what part of the area note is about; the text lines skip headings,
    so the word was found by Omnisearch and not by us."""
    assert [h.path for h in search(index, "garden")] == ["Области/Софт.md"]
