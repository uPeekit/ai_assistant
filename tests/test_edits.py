"""Editing one place in a note.

The note is shown to the model as numbered lines; the model answers with a short list of
operations against those numbers, and `app/vault/mdedit.py: apply_edits` carries them out.
"""

from __future__ import annotations

import pytest

from app import texts
from app.llm.edits import Edit, EditError, Editor
from app.vault import mdedit


class FakeAnthropic:
    """Answers each call with the next item: a dict becomes the model's JSON, an exception is
    raised instead."""

    def __init__(self, *answers: object) -> None:
        import json as _json

        self._json = _json
        self.answers = list(answers)
        self.seen: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return type("Resp", (), {
            "content": [type("Block", (), {"type": "text",
                                            "text": self._json.dumps(answer)})()],
            "stop_reason": "end_turn",
            "usage": type("U", (), {"input_tokens": 300, "output_tokens": 60})(),
        })()

    async def close(self) -> None:
        pass


# ---- the gate: what the model is allowed to point at -------------------------------------


# ---- what the model is shown -----------------------------------------------------------------


# ---- applying the edits ----------------------------------------------------------------------


# ---- undo ---------------------------------------------------------------------------------


# ---- failures -------------------------------------------------------------------------------


def _edits(*ops: dict) -> dict:
    full = {"op": "replace", "at": 0, "to": 0, "text": "", "url": "", "caption": ""}
    return {"edits": [{**full, **op} for op in ops], "full": ""}


async def test_the_editor_refuses_an_empty_page_without_calling_claude():
    client = FakeAnthropic()
    with pytest.raises(EditError):
        await Editor("", "m", client=client).plan("   ", "убери лишнее")
    assert client.seen == []


# ---- the Obsidian side -----------------------------------------------------------------------


def test_edits_apply_to_a_notes_lines_without_shifting_each_other():
    lines = ["# Стенка", "", "вариант А", "вариант Б", "конец"]
    edits = [Edit(op="replace", at=3, text="вариант А, 240 см"),
             Edit(op="insert", at=3, text="- купить анкеры"),
             Edit(op="delete", at=4)]

    assert mdedit.apply_edits(lines, edits) == [
        "# Стенка", "", "вариант А, 240 см", "- купить анкеры", "конец"]


def test_an_edit_pointing_outside_the_note_is_ignored():
    lines = ["один", "два"]
    assert mdedit.apply_edits(lines, [Edit(op="replace", at=9, text="нет")]) == lines
    assert mdedit.apply_edits(lines, [Edit(op="delete", at=0)]) == lines


def test_an_image_edit_becomes_a_markdown_image_line():
    out = mdedit.apply_edits(["текст"], [Edit(op="image", at=1, url="https://x/p.png",
                                              caption="подпись")])
    assert out == ["текст", "![подпись](https://x/p.png)"]


def test_a_note_is_numbered_line_by_line():
    assert mdedit.numbered(["один", "два"]) == "[1] один\n[2] два"


async def test_a_note_is_edited_in_place_and_keeps_everything_else(tmp_path):
    from app.vault.filer import Filer
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline
    from app.vault.writer import VaultWriter
    from tests.test_vault_filer import FakeAnthropic as FakeFiler

    (tmp_path / "Шведская стенка.md").write_text(
        "---\ntag: дом\n---\n\n## Подход\n\nвариант А\nвариант Б\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    writer = VaultWriter(index)
    pipeline = VaultPipeline(
        index, writer,
        Filer("", "haiku", client=FakeFiler({"actions": [{
            "action": "rewrite", "note": "Шведская стенка", "text": "убери вариант Б"}]})),
        # Body lines, without the frontmatter: 1 "## Подход", 2 "", 3 вариант А, 4 вариант Б.
        None, editor=Editor("", "sonnet", client=FakeAnthropic(
            _edits({"op": "delete", "at": 4}))))

    turn = await pipeline.handle("убери из стенки вариант Б")

    text = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    assert "вариант А" in text and "вариант Б" not in text
    assert "## Подход" in text and "tag: дом" in text
    assert turn.writes and turn.writes[0].kind == "rewrite"


async def test_undoing_a_note_edit_is_exact_because_the_whole_file_is_kept(tmp_path):
    from app.vault.filer import Filer
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline
    from app.vault.writer import VaultWriter
    from tests.test_vault_filer import FakeAnthropic as FakeFiler

    before = "---\ntag: дом\n---\n\n![[стенка.png]]\n\nвариант А\nвариант Б\n"
    (tmp_path / "Шведская стенка.md").write_text(before, encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    pipeline = VaultPipeline(
        index, VaultWriter(index),
        Filer("", "haiku", client=FakeFiler({"actions": [{
            "action": "rewrite", "note": "Шведская стенка", "text": "убери картинку"}]})),
        # Line 1 of the body is the embedded picture.
        None, editor=Editor("", "sonnet", client=FakeAnthropic(
            _edits({"op": "delete", "at": 1}))))

    turn = await pipeline.handle("убери из стенки картинку")
    assert "стенка.png" not in (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")

    await pipeline.undo(turn.undos)

    # Byte for byte, including the picture: the file's whole previous text is the undo record,
    # and the attachment itself was never deleted.
    assert (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8") == before


# ---- the page a short follow-up means --------------------------------------------------------

# ---- a line added to a list is undone without taking its neighbours ---------------------------

# ---- a page too long to show the model -------------------------------------------------------

async def test_the_editor_refuses_a_page_it_could_only_read_the_top_of():
    from app.llm.edits import MAX_INPUT, TooLong

    client = FakeAnthropic({"edits": [], "full": "короче"})
    with pytest.raises(TooLong):
        await Editor("", "m", client=client).plan("[1] " + "я" * MAX_INPUT, "сократи")
    assert client.seen == []  # nothing was sent, so nothing can come back to be written


async def test_a_note_too_long_to_edit_is_left_exactly_as_it_is(tmp_path):
    from app.llm.edits import MAX_INPUT
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline, VaultTurn
    from app.vault.writer import VaultAction, VaultWriter

    long_text = "\n".join(f"строка {i} " + "я" * 80 for i in range(MAX_INPUT // 80))
    (tmp_path / "Длинная.md").write_text(long_text, encoding="utf-8", newline="\n")
    index = VaultIndex(tmp_path)
    index.refresh()
    client = FakeAnthropic({"edits": [], "full": "короче"})
    pipe = VaultPipeline(index, VaultWriter(index), filer=None,  # type: ignore[arg-type]
                         editor=Editor("", "m", client=client))
    turn = VaultTurn()

    action = await pipe._rewritten(VaultAction(action="rewrite", note="Длинная",
                                               text="сократи"), turn)

    assert action is None and turn.error == texts.VAULT_TOO_LONG
    assert client.seen == []
    assert (tmp_path / "Длинная.md").read_text(encoding="utf-8") == long_text


# ---- a page longer than one read ------------------------------------------------------------
