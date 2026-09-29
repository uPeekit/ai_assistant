"""Moving part of one note to another.

The case this exists for: «перемести все секции с гитхаб проектами из пройектов в pehmevara».
The filer had no such action, called it a rewrite of «пройекты», and the editor — asked to
change one note — deleted the three sections. Nothing was written to «pehmevara»; the text
survived only in `.trash`."""

from __future__ import annotations

import pytest

from app import texts
from app.llm.edits import Editor
from app.vault import frontmatter
from app.vault.filer import Filer, check
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.writer import VaultWriter
from tests.test_edits import FakeAnthropic as FakeEditor
from tests.test_vault_filer import FakeAnthropic as FakeFiler

# The note exactly as it was before event 97 (from the vault's .trash copy).
PROJECTS = """---
notion: https://app.notion.com/p/3853b0e81b678090af3dff053df8ef37
---

# telega

---

# NoDogWalk

https://github.com/uPeekit/no_dog_walk

---

# OpenTimelapse

https://github.com/uPeekit/open_timelapse

---

# ScalperBot

https://github.com/uPeekit/scalper_bot

---

[[Виды ворот тории]]
"""
AREAS = "Области"


@pytest.fixture
def vault(tmp_path):
    (tmp_path / AREAS).mkdir()
    (tmp_path / AREAS / "пройекты.md").write_text(PROJECTS, encoding="utf-8")
    (tmp_path / AREAS / "pehmevara.md").write_text("Раздел для GitHub-проектов.\n",
                                                   encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return tmp_path, index


def _github_span() -> tuple[int, int]:
    """The line numbers the editor is shown for the three GitHub sections."""
    _, body = frontmatter.split(PROJECTS)
    lines = body.split("\n")
    first = lines.index("# NoDogWalk") + 1
    last = lines.index("https://github.com/uPeekit/scalper_bot") + 1
    return first, last


def _pipeline(index, filer_answer, *editor_answers) -> VaultPipeline:
    return VaultPipeline(index, VaultWriter(index), Filer("", "haiku", client=FakeFiler(
        filer_answer)), None, editor=Editor("", "sonnet", client=FakeEditor(*editor_answers)))


MOVE = {"action": "move", "note": "пройекты", "to": "pehmevara",
        "text": "все секции с гитхаб проектами"}


async def test_sections_move_word_for_word_and_one_undo_puts_both_notes_back(vault):
    root, index = vault
    first, last = _github_span()
    pipeline = _pipeline(index, {"actions": [MOVE]},
                         {"edits": [{"op": "delete", "at": first, "to": last}], "full": ""})

    turn = await pipeline.handle("перемести все секции с гитхаб проектами из пройектов в pehmevara")

    target = (root / AREAS / "pehmevara.md").read_text(encoding="utf-8")
    source = (root / AREAS / "пройекты.md").read_text(encoding="utf-8")
    for url in ("no_dog_walk", "open_timelapse", "scalper_bot"):
        assert f"https://github.com/uPeekit/{url}" in target
        assert url not in source
    assert "Раздел для GitHub-проектов." in target  # added to, not replaced
    assert source.startswith("---\nnotion: https://app.notion.com/p/3853b0e8")  # props kept
    assert "# telega" in source and "[[Виды ворот тории]]" in source
    # Destination first: a failure between the two writes leaves a copy, never a loss.
    assert [w.note for w in turn.writes] == ["pehmevara", "пройекты"]

    await pipeline.undo(turn.undos)

    assert (root / AREAS / "пройекты.md").read_text(encoding="utf-8") == PROJECTS
    assert (root / AREAS / "pehmevara.md").read_text(
        encoding="utf-8") == "Раздел для GitHub-проектов.\n"


async def test_create_and_move_in_one_message_fills_the_new_note(vault):
    """Event 96 asked for both at once: make «pehmevara» and put the projects there."""
    root, index = vault
    (root / AREAS / "pehmevara.md").unlink()
    index.refresh()
    first, last = _github_span()
    pipeline = _pipeline(index, {"actions": [
        {"action": "note", "folder": AREAS, "title": "Гитхаб"},
        {**MOVE, "to": "Гитхаб"}]},
        {"edits": [{"op": "delete", "at": first, "to": last}], "full": ""})

    await pipeline.handle("создай раздел Гитхаб и перенеси туда все гитхаб проекты")

    made = (root / AREAS / "Гитхаб.md").read_text(encoding="utf-8")
    assert "https://github.com/uPeekit/scalper_bot" in made
    assert sorted(p.name for p in (root / AREAS).glob("*.md")) == ["Гитхаб.md", "пройекты.md"]


async def test_a_destination_that_does_not_exist_is_made_next_to_the_source(vault):
    root, index = vault
    first, last = _github_span()
    pipeline = _pipeline(index, {"actions": [{**MOVE, "to": "Код"}]},
                         {"edits": [{"op": "delete", "at": first, "to": last}], "full": ""})

    await pipeline.handle("вынеси гитхаб проекты в заметку Код")

    assert "open_timelapse" in (root / AREAS / "Код.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("answer", [
    # Rewriting instead of pointing: the words would be the model's, not the user's.
    {"edits": [], "full": "# telega"},
    # An insertion or a replacement alongside the deletes: not a move.
    {"edits": [{"op": "delete", "at": 3, "to": 5},
               {"op": "insert", "at": 1, "text": "переехало"}], "full": ""},
    {"edits": [{"op": "replace", "at": 3, "text": "# Другое"}], "full": ""},
])
async def test_anything_but_plain_deletes_moves_nothing_and_cuts_nothing(vault, answer):
    root, index = vault
    pipeline = _pipeline(index, {"actions": [MOVE]}, answer)

    turn = await pipeline.handle("перенеси гитхаб проекты в pehmevara")

    assert turn.writes == []
    assert texts.VAULT_MOVE_UNCLEAR in turn.reply_line()
    assert (root / AREAS / "пройекты.md").read_text(encoding="utf-8") == PROJECTS


async def test_nothing_marked_is_said_so_and_nothing_is_touched(vault):
    root, index = vault
    pipeline = _pipeline(index, {"actions": [MOVE]}, {"edits": [], "full": ""})

    turn = await pipeline.handle("перенеси рецепты в pehmevara")

    assert turn.writes == []
    assert texts.VAULT_NOTHING_TO_MOVE in turn.reply_line()
    assert (root / AREAS / "пройекты.md").read_text(encoding="utf-8") == PROJECTS


def test_a_move_with_nowhere_to_go_is_never_a_cut(vault):
    _, index = vault
    for broken in ({**MOVE, "to": ""}, {**MOVE, "to": "пройекты"}):
        [action] = check([broken], index, "перенеси проекты")
        assert action.action == "inbox"
    [action] = check([{**MOVE, "text": ""}], index, "перенеси проекты в pehmevara")
    assert action.action == "move" and action.text == "перенеси проекты в pehmevara"
