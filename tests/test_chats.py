"""The chat registry (app/chats.py): what the admin page saves is what a message is judged by."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.chats import MAX_SEEN, Chats

CLUB = {"chat_id": -100123, "title": "Клуб", "enabled": True, "full": False,
        "rights": ["ask", "add", "tasks"], "tag": "#клуб", "folder": "Клуб/",
        "instruction": "встречи клуба"}


@pytest.fixture
def chats(tmp_path):
    return Chats(tmp_path / "chats.json", now=lambda: datetime(2026, 10, 15, 20, 0))


def test_a_saved_chat_is_read_back_with_its_scope(chats, tmp_path):
    assert chats.save([CLUB]) == 1
    scope = Chats(tmp_path / "chats.json").get(-100123)
    assert scope.tag == "клуб" and scope.folder == "Клуб"  # "#" and "/" are tidied away
    assert scope.rights == {"ask", "add", "tasks"}
    assert scope.may("tasks") and not scope.may("tick")
    assert chats.save([CLUB]) == 0  # nothing changed, nothing written


def test_full_access_may_everything_and_a_switched_off_chat_is_not_there(chats):
    chats.save([{**CLUB, "full": True, "rights": []}, {**CLUB, "chat_id": -5, "enabled": False}])
    assert chats.get(-100123).may("move")
    assert chats.get(-5) is None


def test_unknown_rights_are_dropped_and_a_chat_without_an_id_is_refused(chats):
    chats.save([{**CLUB, "rights": ["ask", "delete_everything"]}])
    assert chats.get(-100123).rights == {"ask"}
    with pytest.raises(ValueError):
        chats.save([{"title": "x"}])
    with pytest.raises(ValueError):
        chats.save({"chats": []})


def test_a_broken_file_leaves_no_group_chats_and_says_so_once(tmp_path, caplog):
    path = tmp_path / "chats.json"
    path.write_text("{nope", encoding="utf-8")
    with caplog.at_level("WARNING"):
        chats = Chats(path)
        assert chats.get(-100123) is None
        assert chats.get(-100123) is None
    assert sum("could not read" in r.message for r in caplog.records) == 1


def test_an_unlisted_group_is_remembered_once_newest_first_and_capped(chats, tmp_path):
    chats.save([CLUB])
    chats.saw(-100123, "listed")  # listed: not "seen"
    for i in range(MAX_SEEN + 3):
        chats.saw(-200 - i, f"g{i}")
    chats.saw(-200 - (MAX_SEEN + 2), "again")  # already seen: not added twice
    seen = chats.all()["seen"]
    assert len(seen) == MAX_SEEN and seen[0]["chat_id"] == -200 - (MAX_SEEN + 2)
    assert all(s["chat_id"] != -100123 for s in seen)
    raw = json.loads((tmp_path / "chats.json").read_text(encoding="utf-8"))
    assert raw["chats"][0]["chat_id"] == -100123  # saving "seen" kept the chats


def test_adding_a_seen_group_takes_it_off_the_seen_list(chats):
    chats.saw(-300, "Новая группа")
    chats.save([{**CLUB, "chat_id": -300}])
    assert chats.all()["seen"] == []


def test_a_group_without_a_tag_is_not_answered_until_it_has_one(chats):
    chats.save([{**CLUB, "tag": ""}])
    assert chats.get(-100123) is None
    chats.save([{**CLUB, "tag": "", "full": True}])
    assert chats.get(-100123) is not None  # full access needs no tag
