"""The vault's reader looks things up and searches the web on its own; the orchestrator only
hands it the search to run."""

from __future__ import annotations

from app import texts
from app.llm.research import ResearchError, ResearchQuestion
from app.vault.filer import check
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.staged import StagedFiler
from app.vault.writer import VaultWriter
from tests.test_orchestrator import (
    CHAT,
    USER,
    FakeResearcher,
    bot,  # noqa: F401  (the fixture)
)
from tests.test_vault_staged import Script

RECIPE = "## Борщ\n- свёкла\n- капуста"


def _searching(title: str = "рецепт борща", media: str = "text") -> Script:
    return Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                  FOLDER_PROMPT={"items": [{"title": title, "props": [], "body": [],
                                            "tags": []}],
                                 "lookup": "", "web": title, "media": media})


def _pipeline(tmp_path, script: Script) -> tuple[VaultPipeline, VaultIndex]:
    (tmp_path / "Заметки").mkdir()
    (tmp_path / "Заметки/идеи.md").write_text("текст\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return VaultPipeline(index, VaultWriter(index), StagedFiler(script, "haiku"), None), index


async def test_the_vault_fills_its_note_from_its_own_search(tmp_path):
    pipeline, _ = _pipeline(tmp_path, _searching())
    asked = []

    async def research(request: str, query: str, media: str) -> str:
        asked.append((request, query, media))
        return RECIPE

    turn = await pipeline.handle("найди рецепт борща и запиши", research=research)

    note = (tmp_path / "Заметки/рецепт борща.md").read_text(encoding="utf-8")
    assert "- свёкла" in note and turn.remark == ""
    assert asked == [("найди рецепт борща и запиши", "рецепт борща", "text")]


async def test_a_search_that_fails_still_writes_the_note_and_says_why_it_is_empty(tmp_path):
    for failure, remark in ((ResearchError("no answer"), texts.VAULT_WEB_FAILED),
                            (ResearchQuestion("какой борщ?"), texts.VAULT_WEB_UNCLEAR),
                            (None, texts.VAULT_WEB_OFF)):
        folder = tmp_path / str(len(remark))
        folder.mkdir()
        pipeline, _ = _pipeline(folder, _searching())

        async def research(request: str, query: str, media: str, failure=failure) -> str:
            raise failure

        turn = await pipeline.handle("найди рецепт борща и запиши",
                                     research=research if failure else None)

        assert (folder / "Заметки/рецепт борща.md").exists()
        assert turn.remark == remark and remark in turn.reply_line()


def test_a_book_that_is_already_there_is_updated_rather_than_copied(tmp_path):
    """«Преступление и наказание — прочитал» when the note exists: set its status."""
    (tmp_path / "Книги").mkdir()
    (tmp_path / "Книги/Идиот.md").write_text("---\nstatus: To read\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()

    [action] = check([{"action": "note", "folder": "Книги", "title": "Идиот",
                       "props": [{"name": "status", "value": "Read"}]}], index, "прочитал Идиот")

    assert (action.action, action.note, action.props) == ("update", "Идиот", {"status": "Read"})
    # A new book is still a new note.
    [action] = check([{"action": "note", "folder": "Книги", "title": "Бесы",
                       "props": [{"name": "status", "value": "Read"}]}], index, "Бесы")
    assert action.action == "note"


async def test_the_orchestrator_hands_the_vault_the_search_it_asks_for(bot):  # noqa: F811
    bot.orch._researcher = researcher = FakeResearcher(RECIPE)
    bot.orch._vault._filer = StagedFiler(_searching(), "haiku")

    await bot.orch.handle_text(CHAT, USER, "найди рецепт борща и запиши")

    assert len(researcher.asked) == 1
    notes = list((bot.dir / "Области").glob("рецепт*.md"))
    assert len(notes) == 1 and "- свёкла" in notes[0].read_text(encoding="utf-8")


def test_a_looked_up_item_that_is_already_there_is_left_alone(tmp_path):
    """Live: «хочу прочитать все романы Достоевского, Идиот я уже прочитал» marked «Бесы»,
    «Игрок» and «Подросток» read, and a lookup that says "To read" would reset one the user
    has read. The lookup knows the books, not what the user did with them."""
    (tmp_path / "Книги").mkdir()
    (tmp_path / "Книги/Бесы.md").write_text("---\nstatus: Read\n---\n", encoding="utf-8")
    (tmp_path / "Книги/S.N.U.F.F.md").write_text("---\nstatus: Read\n---\n", encoding="utf-8")
    (tmp_path / "Книги/Generation П.md").write_text("---\nstatus: Read\n---\n", encoding="utf-8")
    (tmp_path / "Книги/Униженные и оскорблённые.md").write_text("x", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    looked_up = [{"action": "note", "folder": "Книги", "title": title, "looked_up": True,
                  "props": [{"name": "status", "value": "To read"}]}
                 for title in ("Бесы", "S.N.U.F.F.", "Generation «П»",
                               "Унижённые и оскорблённые", "Игрок")]

    actions = check(looked_up, index, "хочу прочитать все романы")

    # The three already there — one by name, two only once punctuation is dropped — are
    # untouched; only the missing one is written.
    assert [(a.action, a.title) for a in actions] == [("note", "Игрок")]


async def test_what_was_said_about_one_listed_book_is_not_copied_onto_the_set():
    light = Script(
        INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f2"},
        FOLDER_PROMPT={"items": [{"title": "Идиот", "body": [], "tags": [],
                                  "props": [{"name": "status", "value": "Read"}]}],
                       "lookup": "романы Достоевского, хочу прочитать", "web": "",
                       "media": "text"})
    strong = Script(LOOKUP_NOTES_PROMPT={"items": [
        {"title": "Бесы", "props": [{"name": "status", "value": "To read"}]}]})
    from tests.test_vault_staged import ctx

    actions, _, _ = await StagedFiler(light, "haiku", lookup=strong).file(
        "хочу прочитать все романы Достоевского, Идиот я уже прочитал", ctx())

    assert [(a["title"], a["props"]) for a in actions] == [
        ("Идиот", [{"name": "status", "value": "Read"}]),
        ("Бесы", [{"name": "status", "value": "To read"}])]
    assert actions[1]["looked_up"] is True and "looked_up" not in actions[0]


async def test_a_set_that_is_all_there_already_is_said_so_not_sent_to_the_inbox(tmp_path):
    """Live: «добавь все романы Пелевина» when every one of them is in «Книги» ended as
    «не понял, куда это» and a line in the inbox."""
    (tmp_path / "Книги").mkdir()
    (tmp_path / "Книги/Омон Ра.md").write_text("---\nstatus: Read\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    light = Script(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "f1"},
                   FOLDER_PROMPT={"items": [], "lookup": "романы Пелевина", "web": "",
                                  "media": "text"})
    strong = Script(LOOKUP_NOTES_PROMPT={"items": [{"title": "Омон Ра", "props": []}]})
    pipeline = VaultPipeline(index, VaultWriter(index),
                             StagedFiler(light, "haiku", lookup=strong), None)

    turn = await pipeline.handle("добавь все романы Пелевина в книги")

    assert turn.writes == [] and turn.reply_line() == texts.VAULT_ALL_THERE
    assert not (tmp_path / f"{texts.VAULT_INBOX_NOTE}.md").exists()
