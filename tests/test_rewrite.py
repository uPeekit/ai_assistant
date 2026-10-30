"""Rewriting text that is already there.

The request this exists for: «у меня страница про шведскую стенку, там много всего из разных
источников — оставь один подход и одни размеры».
"""


from __future__ import annotations

import anthropic
import pytest

from app import texts
from app.llm.rewrite import RewriteError, Rewriter, unfence

PAGE = "pg-wall"


class FakeAnthropic:
    """Answers each call with the next item: a string becomes the model's text, an exception is
    raised instead."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.seen: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        answer = self.answers.pop(0) if self.answers else ""
        if isinstance(answer, Exception):
            raise answer
        return type("Resp", (), {
            "content": [type("Block", (), {"type": "text", "text": answer})()],
            "stop_reason": "end_turn",
            "usage": type("U", (), {"input_tokens": 120, "output_tokens": 40})(),
        })()

    async def close(self) -> None:
        pass


# ---- the rule that matters most ----------------------------------------------------------


# ---- the write itself ------------------------------------------------------------------------


# ---- undo ---------------------------------------------------------------------------------


# ---- what the user is told ----------------------------------------------------------------


# ---- the model's answer ----------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "expected"), [
    ("```markdown\n# A\n```", "# A"),
    ("```\n# A\n```", "# A"),
    ("# A", "# A"),
    # A page that really is one code block keeps its fence.
    ("```python\nx = 1\n```", "```python\nx = 1\n```"),
])
def test_a_fence_the_model_wrapped_everything_in_is_taken_off(answer, expected):
    assert unfence(answer) == expected


async def test_an_answer_cut_off_mid_page_is_refused():
    """Half a page is worse than none: the tail would simply disappear."""
    class Truncating(FakeAnthropic):
        async def create(self, **kwargs):
            resp = await super().create(**kwargs)
            resp.stop_reason = "max_tokens"
            return resp

    rewriter = Rewriter("", "m", client=Truncating("начало текста"))
    with pytest.raises(RewriteError):
        await rewriter.rewrite("что-то длинное", "сократи")


# ---- reading a page -------------------------------------------------------------------------


# ---- the Obsidian side ------------------------------------------------------------------------

def _vault(tmp_path, text: str = ""):
    """A vault with one note, its index, and a writer over it."""
    from app.vault.index import VaultIndex
    from app.vault.writer import VaultWriter

    (tmp_path / "Шведская стенка.md").write_text(
        text or "---\ntag: дом\n---\n\n## Подход\n\nвариант А\n\n## Размеры\n\n90 см\n",
        encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index, VaultWriter(index)


def _pipeline(index, writer, filer_answer: dict, *rewrites: object):
    from app.vault.filer import Filer
    from app.vault.pipeline import VaultPipeline
    from tests.test_vault_filer import FakeAnthropic as FakeFiler

    return VaultPipeline(index, writer, Filer("", "haiku", client=FakeFiler(filer_answer)),
                         None, rewriter=Rewriter("", "sonnet", client=FakeAnthropic(*rewrites)))


async def test_a_note_is_rewritten_and_keeps_its_frontmatter(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "text": "оставь один подход"}]},
        "## Подход\n\nвариант А, 90 см\n")

    turn = await pipeline.handle("перепиши стенку, оставь один подход")

    text = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    assert "tag: дом" in text  # the properties are the note's, not the text's
    assert "вариант А, 90 см" in text and "## Размеры" not in text
    assert "Obsidian —" in turn.reply_line()


async def test_only_the_named_section_is_rewritten(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "heading": "Размеры",
        "text": "в миллиметрах"}]}, "900 мм\n")

    await pipeline.handle("перепиши размеры в миллиметрах")

    text = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    assert "900 мм" in text
    assert "## Подход" in text and "вариант А" in text  # the rest is untouched


async def test_the_model_is_shown_only_the_section_it_is_asked_to_rewrite(tmp_path):
    index, writer = _vault(tmp_path)
    client = FakeAnthropic("900 мм")
    from app.vault.filer import Filer
    from app.vault.pipeline import VaultPipeline
    from tests.test_vault_filer import FakeAnthropic as FakeFiler
    pipeline = VaultPipeline(
        index, writer, Filer("", "haiku", client=FakeFiler({"actions": [{
            "action": "rewrite", "note": "Шведская стенка", "heading": "Размеры",
            "text": "в миллиметрах"}]})),
        None, rewriter=Rewriter("", "sonnet", client=client))

    await pipeline.handle("перепиши размеры")

    sent = client.seen[0]["messages"][0]["content"]
    assert "90 см" in sent and "вариант А" not in sent


async def test_the_previous_version_is_kept_in_trash_beyond_the_undo_window(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "text": "сократи"}]}, "коротко\n")

    turn = await pipeline.handle("сократи стенку")
    await pipeline.undo(turn.undos)

    trashed = list((tmp_path / ".trash").glob("*.md"))
    assert trashed and "вариант А" in trashed[0].read_text(encoding="utf-8")
    # and undo itself put the note back as it was
    assert "вариант А" in (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")


async def test_a_rewrite_of_a_note_that_is_not_there_writes_nothing(tmp_path):
    index, writer = _vault(tmp_path)
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Которой нет", "text": "сократи"}]}, "коротко")

    turn = await pipeline.handle("сократи то, чего нет")

    # The check turns an unknown note into an inbox line: the words are kept, nothing is lost.
    assert [w.kind for w in turn.writes] == ["inbox"]


async def test_a_failed_rewrite_leaves_the_note_exactly_as_it_was(tmp_path):
    index, writer = _vault(tmp_path)
    before = (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8")
    error = anthropic.APIError("boom", request=None,  # type: ignore[arg-type]
                               body={"error": {"message": "Your credit balance is too low"}})
    error.status_code = 400  # type: ignore[attr-defined]
    pipeline = _pipeline(index, writer, {"actions": [{
        "action": "rewrite", "note": "Шведская стенка", "text": "сократи"}]}, error)

    turn = await pipeline.handle("сократи стенку")

    assert (tmp_path / "Шведская стенка.md").read_text(encoding="utf-8") == before
    assert turn.writes == []
    assert texts.LLM_DOWN_SHORT["credit"] in turn.reply_line()


async def test_the_rewriter_refuses_a_text_it_could_only_read_the_top_of():
    """It used to send the first 20 000 characters and hand back a text the caller then wrote
    in place of the whole: everything past the cut was deleted without ever being read."""
    from app.llm.rewrite import MAX_INPUT, TooLong

    client = FakeAnthropic("короче")
    with pytest.raises(TooLong):
        await Rewriter("", "m", client=client).rewrite("я" * (MAX_INPUT + 1), "сократи")
    assert client.seen == []
