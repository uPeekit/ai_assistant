"""A message through the orchestrator: the vault writes it, one audit row records it, and Undo
and Fix take it back. Claude is a stand-in; the vault, the database and the clock are real."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import pytest

from app import texts
from app.audit.store import AuditStore
from app.config import Settings
from app.conversation.orchestrator import Orchestrator
from app.llm.health import Health
from app.switches import Switches
from app.vault.filer import Filer
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline, VaultTurn
from app.vault.writer import VaultWriter
from tests.test_vault_filer import FakeAnthropic

NOW = datetime.now(UTC)
CHAT, USER = 42, 7
VAULT_NOW = datetime(2026, 9, 22, 18, 30)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += timedelta(seconds=seconds)


class FakeLinks:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    async def read(self, message):
        self.asked.append(message)
        return self.pages if "http" in message else []


class FakeAnswerer:
    def __init__(self, reply="Залог — две месячные платы.", fail=False):
        self.reply, self.fail, self.asked = reply, fail, []

    async def answer(self, question, pages):
        from app.llm.answer import LinkAnswerError

        self.asked.append((question, [p.url for p in pages]))
        if self.fail:
            raise LinkAnswerError("no answer")
        return self.reply


class FakeResearcher:
    def __init__(self, answer: str = "## Борщ\n- свёкла\n- капуста", fail: bool = False):
        self.answer, self.fail = answer, fail
        self.asked: list[tuple[str, str]] = []

    async def research(self, request: str, query: str, media: str = "text") -> str:
        from app.llm.research import ResearchError

        self.asked.append((request, query))
        self.media = media
        if self.fail:
            raise ResearchError("no answer")
        return self.answer


@pytest.fixture
def bot(tmp_path, env):
    vault_dir = tmp_path / "vault"
    (vault_dir / texts.VAULT_AREAS_DIR).mkdir(parents=True)
    (vault_dir / f"{texts.VAULT_AREAS_DIR}/дом.md").write_text("---\ntag: home\n---\n",
                                                                encoding="utf-8")
    (vault_dir / f"{texts.VAULT_TASKS_NOTE}.md").write_text("## дом\n\n- [ ] счета #home\n",
                                                             encoding="utf-8")
    db = tmp_path / "bot.sqlite"
    store = AuditStore(db)
    store.migrate()
    settings = Settings(db_path=db, timezone="Europe/Athens", undo_window_s=300)
    index = VaultIndex(vault_dir)
    index.refresh()
    writer = VaultWriter(index, now=lambda: VAULT_NOW)
    claude = FakeAnthropic()
    # One health record for the process, exactly as main.build wires it: the filer is what
    # notices Claude is down, the orchestrator is what tells the user.
    health = Health()
    vault = VaultPipeline(index, writer,
                          Filer("", "claude-haiku-4-5", client=claude, health=health),
                          None, now=lambda: VAULT_NOW)
    clock = Clock(NOW)
    orch = Orchestrator(settings, store, vault=vault, clock=clock, health=health)
    yield type("VaultBot", (), {
        "orch": orch, "claude": claude, "index": index, "store": store, "clock": clock,
        "db": db, "vault": vault, "dir": vault_dir,
    })()
    store.close()


def rows(bot, table: str) -> list[dict]:
    con = sqlite3.connect(bot.db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        con.close()


def closed_events(bot, kinds: list[str] | None = None) -> list[dict]:
    """One events row per handled message, each closed with a real decision and a duration. The
    orchestrator seeds the column with a placeholder when it opens the row, so "not null" proves
    nothing — what matters is that some exit replaced it."""
    evs = rows(bot, "events")
    for e in evs:
        decided = json.loads(e["decision"])["kind"]
        assert decided != "OPEN", f"event {e['id']} ({e['kind']}) was closed still unresolved"
        assert e["duration_ms"] is not None, f"event {e['id']} has no duration"
    if kinds is not None:
        assert [e["kind"] for e in evs] == kinds
    return evs


def executions(bot) -> list[dict]:
    return rows(bot, "executions")


def task(bot, text: str = "зубы") -> None:
    bot.claude.answers = [{"actions": [{"action": "task", "text": text, "heading": "дом"}]}]


async def test_a_message_is_written_to_the_vault_and_undo_takes_it_back(bot):
    task(bot)

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert reply.text.startswith("✅ Obsidian —")
    assert "- [ ] зубы" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    [row] = executions(bot)
    assert json.loads(row["undo"])["kind"] == "vault"
    assert [[b.id for b in r] for r in reply.buttons] == [[f"u:{row['id']}", f"f:{row['id']}"]]
    assert reply.undo_id == row["id"]

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{row['id']}")
    assert undone.text == texts.UNDONE
    assert "зубы" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_obsidian_off_says_so_rather_than_swallowing_the_message(bot, tmp_path):
    bot.orch._switches = Switches(tmp_path / "switches.json", {"obsidian": False})
    reply = await bot.orch.handle_text(CHAT, USER, "зубы")
    assert reply.text == texts.ERRORS["NOTHING_ENABLED"]


async def test_an_account_out_of_credit_is_named_in_the_reply(bot):
    """The Obsidian line says what went wrong in words, and the reply carries the warning
    once — not once per message for the rest of the evening."""
    error = anthropic.APIError("boom", request=None,  # type: ignore[arg-type]
                               body={"error": {"message": "Your credit balance is too low"}})
    error.status_code = 400  # type: ignore[attr-defined]
    bot.claude.answers = [error, error]

    first = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert texts.LLM_DOWN_SHORT["credit"] in first.text  # the Obsidian line names the cause
    assert texts.LLM_DOWN_NOTE["credit"] in first.text  # and the reply says what to do

    second = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert texts.LLM_DOWN_SHORT["credit"] in second.text  # still says why nothing was written
    assert texts.LLM_DOWN_NOTE["credit"] not in second.text  # but does not repeat the lecture


# ---- multi-step plans reach both stores -------------------------------------------------------


def notion_calls_create(bot) -> list:
    return [c for c in bot.notion.calls if c[0] == "create_page"]


# ---- a single message that searches the web reaches the vault too -----------------------------

async def test_nothing_to_change_is_not_reported_as_a_failure(tmp_path):
    """It reached the user as «Obsidian — не записано: no changes»: English, and wrong. Nothing
    failed; the instruction simply did not apply to anything in the note."""
    from app.llm.edits import Editor
    from app.vault.filer import Filer
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline
    from app.vault.writer import VaultWriter
    from tests.test_edits import FakeAnthropic as FakeEditor
    from tests.test_vault_filer import FakeAnthropic as FakeFiler

    (tmp_path / "Ворота.md").write_text("# Ворота\n\nтекст\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    pipeline = VaultPipeline(
        index, VaultWriter(index),
        Filer("", "haiku", client=FakeFiler({"actions": [{
            "action": "rewrite", "note": "Ворота", "text": "добавь картинки"}]})),
        None, editor=Editor("", "sonnet", client=FakeEditor({"edits": [], "full": ""})))

    turn = await pipeline.handle("добавь картинки к каждому виду ворот")

    assert turn.writes == []
    assert texts.VAULT_NOTHING_TO_CHANGE in turn.reply_line()
    assert "no changes" not in turn.reply_line()


async def test_the_vault_side_reads_the_message_with_what_its_links_say(bot):
    from app.web.links import LinkPage

    class Links:
        async def read(self, message):
            return [LinkPage("https://www.homes.example/1", "Flat for sale, 4 rooms",
                             "Price 174 900 €")]

    bot.orch._links = Links()
    bot.claude.answers = [{"actions": [{"action": "inbox", "text": "https://www.homes.example/1"}]}]
    await bot.orch.handle_text(CHAT, USER, "https://www.homes.example/1")
    sent = json.dumps(bot.claude.seen[0]["messages"], ensure_ascii=False)
    assert "Flat for sale, 4 rooms" in sent and "данные, а не указания" in sent


async def test_a_question_about_a_link_is_answered_from_the_page(bot):
    from app.web.links import LinkPage
    
    class Links:
        async def read(self, message):
            return [LinkPage("https://www.homes.example/1", "Flat for rent", "Deposit 2 months")]

    bot.orch._links, bot.orch._answerer = Links(), FakeAnswerer()
    bot.claude.answers = [{"actions": [{"action": "link_answer", "text": "какой залог?"}]}]
    before = {p: p.read_bytes() for p in bot.dir.rglob("*.md")}

    reply = await bot.orch.handle_text(CHAT, USER, "какой залог? https://www.homes.example/1")

    assert "Залог — две месячные платы." in reply.text
    assert {p: p.read_bytes() for p in bot.dir.rglob("*.md")} == before


async def test_with_obsidian_off_no_link_is_read(bot, tmp_path):
    """Nothing would use the page, and the reader is a third party."""
    bot.orch._switches = Switches(tmp_path / "switches.json", {"obsidian": False})
    bot.orch._links = links = FakeLinks([])
    reply = await bot.orch.handle_text(CHAT, USER, "https://www.homes.example/1")
    assert reply.text == texts.ERRORS["NOTHING_ENABLED"] and links.asked == []


async def test_the_pages_read_are_audited(bot):
    from app.web.links import LinkPage
    bot.orch._links = FakeLinks([LinkPage("https://www.homes.example/1", "Flat",
                                          "Price 174 900 €")])
    bot.claude.answers = [{"actions": [{"action": "inbox", "text": "https://www.homes.example/1"}]}]
    await bot.orch.handle_text(CHAT, USER, "https://www.homes.example/1")
    (event,) = closed_events(bot, ["text"])
    assert json.loads(event["llm_context"])["links"][0]["title"] == "Flat"


# ---- undo takes back one message, not the file ----------------------------------------------

async def test_undo_of_the_first_of_two_messages_keeps_the_second(bot, tmp_path):
    bot.claude.answers = [
        {"actions": [{"action": "task", "text": "лампочки", "heading": "дом"}]},
        {"actions": [{"action": "task", "text": "позвонить маме", "heading": "дом"}]},
    ]
    first = await bot.orch.handle_text(CHAT, USER, "лампочки")
    await bot.orch.handle_text(CHAT, USER, "позвонить маме")

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{first.undo_id}")

    assert undone.text == texts.UNDONE
    tasks = bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "лампочки" not in tasks and "- [ ] позвонить маме" in tasks


async def test_undo_says_so_when_the_line_was_changed_by_hand_since(bot, tmp_path):
    bot.claude.answers = [{"actions": [{"action": "task", "text": "лампочки",
                                         "heading": "дом"}]}]
    reply = await bot.orch.handle_text(CHAT, USER, "лампочки")
    path = bot.dir / f"{texts.VAULT_TASKS_NOTE}.md"
    edited = path.read_text(encoding="utf-8").replace("- [ ] лампочки", "- [x] лампочки")
    path.write_text(edited, encoding="utf-8", newline="\n")

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")

    assert undone.text == texts.VAULT_UNDO_LEFT.format(notes=f"«{texts.VAULT_TASKS_NOTE}»")
    assert path.read_text(encoding="utf-8") == edited


# ---- fixing part of a write ---------------------------------------------------------------------

@pytest.fixture
def staged_bot(bot, tmp_path):
    """The same bot with the staged reader, scripted per stage."""
    from app.vault.staged import StagedFiler
    from tests.test_vault_fix import Script

    script = Script()
    bot.vault._filer = StagedFiler(script, "haiku")
    bot.script = script
    return bot


def _milk_task(script) -> None:
    script.answers.update(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                          TASKS_PROMPT={"items": [{"text": "молоко", "due": "", "repeat": "",
                                                   "heading": "дом", "tag": "",
                                                   "countdown": False}], "lookup": ""})


MOVE_TO_GROCERIES = {"changes": [{"key": "a1", "op": "move", "field": "", "prop": "",
                                  "value": "", "to": "g"}], "add": [], "unclear": False}


async def test_the_fix_button_asks_then_the_next_message_moves_the_task(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")

    asked = await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")
    assert asked.text == texts.FIX_ASK
    bot.script.answers["FIX_PROMPT"] = MOVE_TO_GROCERIES
    fixed = await bot.orch.handle_text(CHAT, USER, "не в задачи, а в продукты")

    assert fixed.text.startswith(texts.FIX_DONE.split("{")[0])
    assert "молоко" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")
    assert [b.id for b in fixed.buttons[0]] == [f"u:{fixed.undo_id}", f"f:{fixed.undo_id}"]
    # The first message's buttons are done with.
    stale = await bot.orch.handle_callback(CHAT, USER, f"u:{first.undo_id}")
    assert stale.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)


async def test_a_reply_to_the_writes_message_is_a_fix_and_undo_then_removes_everything(
        staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.store.set_reply_message_id(first.undo_id, 5001)  # what the transport records

    bot.script.answers["FIX_PROMPT"] = MOVE_TO_GROCERIES
    fixed = await bot.orch.handle_text(CHAT, USER, "в продукты", reply_to=5001)
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")

    bot.script.answers["FIX_PROMPT"] = {"changes": [{"key": "a1", "op": "set", "field": "text",
                                                     "prop": "", "value": "молоко овсяное",
                                                     "to": ""}], "add": [], "unclear": False}
    again = await bot.orch.handle_callback(CHAT, USER, f"f:{fixed.undo_id}")
    assert again.text == texts.FIX_ASK
    twice = await bot.orch.handle_text(CHAT, USER, "молоко овсяное")
    assert "- [ ] молоко овсяное" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{twice.undo_id}")
    assert undone.text == texts.UNDONE
    assert "молоко" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    # The grocery page did not exist before the first message: the fix made it, and Undo
    # takes it to the trash with everything else.
    assert not (bot.dir / f"{texts.VAULT_GROCERIES_NOTE}.md").exists()


async def test_a_reply_to_an_expired_write_is_too_late_and_writes_nothing(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.store.set_reply_message_id(first.undo_id, 5001)
    bot.orch._clock = Clock(NOW + timedelta(minutes=6))

    reply = await bot.orch.handle_text(CHAT, USER, "в продукты", reply_to=5001)

    assert reply.text == texts.FIX_EXPIRED.format(minutes=5)
    assert "FIX_PROMPT" not in bot.script.stages
    assert "в продукты" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_a_reply_to_a_message_that_reported_no_write_is_an_ordinary_message(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    reply = await bot.orch.handle_text(CHAT, USER, "надо молоко", reply_to=9999)
    assert reply.text.startswith("✅ Obsidian —")
    assert "FIX_PROMPT" not in bot.script.stages


async def test_a_fix_the_model_cannot_place_changes_nothing_and_can_be_tried_again(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.script.answers["FIX_PROMPT"] = {"changes": [], "add": [], "unclear": True}
    await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")

    reply = await bot.orch.handle_text(CHAT, USER, "ну это")

    assert reply.text == texts.FIX_FAILED.format(error=texts.FIX_UNCLEAR)
    assert "- [ ] молоко" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    # Pressing Поправить took the buttons off the write's message: the answer gives them
    # back, without claiming the message (the write's own message stays the one recorded).
    assert [b.id for b in reply.buttons[0]] == [f"u:{first.undo_id}", f"f:{first.undo_id}"]
    assert reply.undo_id is None
    assert (await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")).text \
        == texts.FIX_ASK


async def test_a_fix_asked_for_and_left_is_forgotten_when_the_window_closes(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")
    bot.orch._clock = Clock(NOW + timedelta(minutes=6))

    bot.script.answers.update(TARGET_PROMPT={"target": "g"},
                              GROCERY_PROMPT={"names": ["хлеб"]})
    reply = await bot.orch.handle_text(CHAT, USER, "купить хлеб")

    assert reply.text.startswith("✅ Obsidian —")
    assert "FIX_PROMPT" not in bot.script.stages
    assert "хлеб" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")


async def test_a_correction_typed_just_after_the_window_is_too_late_not_a_new_note(staged_bot):
    """Поправить pressed at 4 minutes, the correction sent at 6: the write's window (5) has
    closed, but the press is fresh — «в продукты» is a correction, not a grocery line."""
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.orch._clock = Clock(NOW + timedelta(minutes=4))
    assert (await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")).text \
        == texts.FIX_ASK
    bot.orch._clock = Clock(NOW + timedelta(minutes=6))
    bot.script.answers.update(TARGET_PROMPT={"target": "g"},
                              GROCERY_PROMPT={"names": ["продукты"]})

    reply = await bot.orch.handle_text(CHAT, USER, "в продукты")

    assert reply.text == texts.FIX_EXPIRED.format(minutes=5)
    assert "FIX_PROMPT" not in bot.script.stages
    assert not (bot.dir / f"{texts.VAULT_GROCERIES_NOTE}.md").exists()
    assert "в продукты" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_undo_that_undid_nothing_can_be_pressed_again(bot, tmp_path):
    """The line was edited by hand, so Undo left it. Once the edit is reverted, Undo works:
    the row was not spent on a take-back that did nothing."""
    bot.claude.answers = [{"actions": [{"action": "task", "text": "лампочки",
                                         "heading": "дом"}]}]
    reply = await bot.orch.handle_text(CHAT, USER, "лампочки")
    path = bot.dir / f"{texts.VAULT_TASKS_NOTE}.md"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("- [ ] лампочки", "- [x] лампочки"), encoding="utf-8",
                    newline="\n")

    left = await bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")
    assert left.text == texts.VAULT_UNDO_LEFT.format(notes=f"«{texts.VAULT_TASKS_NOTE}»")

    path.write_text(original, encoding="utf-8", newline="\n")
    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")
    assert undone.text == texts.UNDONE
    assert "лампочки" not in path.read_text(encoding="utf-8")


async def test_a_reply_wins_over_a_pending_button_fix(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    bot.script.answers["TASKS_PROMPT"] = {"items": [{"text": "хлеб", "due": "", "repeat": "",
                                                     "heading": "дом", "tag": "",
                                                     "countdown": False}], "lookup": ""}
    second = await bot.orch.handle_text(CHAT, USER, "надо хлеб")
    bot.store.set_reply_message_id(first.undo_id, 5001)
    bot.store.set_reply_message_id(second.undo_id, 5002)
    await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")

    bot.script.answers["FIX_PROMPT"] = {"changes": [{"key": "a1", "op": "set", "field": "text",
                                                     "prop": "", "value": "хлеб ржаной",
                                                     "to": ""}], "add": [], "unclear": False}
    await bot.orch.handle_text(CHAT, USER, "хлеб ржаной", reply_to=5002)

    tasks = bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] хлеб ржаной" in tasks and "- [ ] молоко" in tasks
    assert bot.script.stages.count("FIX_PROMPT") == 1
    # The reply used up the pending fix: the next message is an ordinary one.
    bot.script.answers.update(TARGET_PROMPT={"target": "g"},
                              GROCERY_PROMPT={"names": ["яйца"]})
    later = await bot.orch.handle_text(CHAT, USER, "купить яйца")
    assert later.text.startswith("✅ Obsidian —")
    assert bot.script.stages.count("FIX_PROMPT") == 1


async def test_a_reply_to_an_older_versions_notion_write_is_an_ordinary_message(staged_bot):
    bot = staged_bot
    event = bot.store.new_event(telegram_user_id=USER, chat_id=CHAT, kind="text")
    row_id = bot.store.add_execution(
        event, CHAT, None, OLD_NOTION_ROW, NOW + timedelta(minutes=5))
    bot.store.set_reply_message_id(row_id, 5001)
    bot.script.answers.update(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "g"},
                              GROCERY_PROMPT={"names": ["хлеб"]})

    reply = await bot.orch.handle_text(CHAT, USER, "купить хлеб", reply_to=5001)

    assert reply.text.startswith("✅ Obsidian —")
    assert "FIX_PROMPT" not in bot.script.stages
    assert "хлеб" in bot.index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")


# ---- rows written before the bot was vault-only -------------------------------------------------

OLD_NOTION_ROW = json.dumps({"kind": "archive", "page_id": "p", "properties": None, "block_ids": [],
                             "partial": False, "markdown": [], "edits": [], "batch": [],
                             "vault": [], "reminders": []})


async def test_undo_of_an_older_versions_notion_write_says_it_cannot(bot):
    event = bot.store.new_event(telegram_user_id=USER, chat_id=CHAT, kind="text")
    row_id = bot.store.add_execution(event, CHAT, None, OLD_NOTION_ROW,
                                     NOW + timedelta(minutes=5))

    reply = await bot.orch.handle_callback(CHAT, USER, f"u:{row_id}")

    assert reply.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)
    assert executions(bot)[0]["undone"] == 0


@pytest.mark.parametrize("data", ["a:token:opt", "i:12", "zz:1"])
async def test_a_button_from_an_older_version_says_it_no_longer_works(bot, data):
    reply = await bot.orch.handle_callback(CHAT, USER, data)
    assert reply.text == texts.ERRORS["BUTTON_EXPIRED"]
    closed_events(bot, ["callback"])


# ---- the audit row ------------------------------------------------------------------------------

async def test_a_message_leaves_one_closed_row_saying_what_happened(bot):
    task(bot)
    await bot.orch.handle_text(CHAT, USER, "зубы")

    (event,) = closed_events(bot, ["text"])
    assert event["raw_input"] == "зубы" and event["error"] is None
    assert json.loads(event["decision"])["kind"] == "VAULT"
    assert event["llm_model"] == "claude-haiku-4-5"
    assert json.loads(event["llm_response"])[0]["kind"] == "vault"


async def test_voice_message_audits_the_transcript(bot):
    task(bot)
    await bot.orch.handle_text(CHAT, USER, "зубы", kind="voice", transcript="зубы")
    (event,) = closed_events(bot, ["voice"])
    assert event["transcription"] == "зубы"


# ---- undo ---------------------------------------------------------------------------------------

async def test_two_presses_of_the_same_undo_button_revert_once(bot):
    task(bot)
    created = await bot.orch.handle_text(CHAT, USER, "зубы")

    first, second = await asyncio.gather(
        bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}"),
        bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}"),
    )

    assert first.text == texts.UNDONE
    assert second.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)
    assert executions(bot)[0]["undone"] == 1


async def test_the_undo_command_undoes_the_latest_write(bot):
    task(bot)
    await bot.orch.handle_text(CHAT, USER, "зубы")

    assert (await bot.orch.undo(CHAT)).text == texts.UNDONE
    assert "зубы" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    closed_events(bot, ["text", "undo"])


async def test_the_undo_command_without_a_write_is_refused(bot):
    reply = await bot.orch.undo(CHAT)
    assert reply.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)
    closed_events(bot, ["undo"])


async def test_undo_after_the_window_is_refused(bot):
    task(bot)
    created = await bot.orch.handle_text(CHAT, USER, "зубы")
    bot.clock.advance(301)

    reply = await bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}")

    assert reply.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)
    assert "- [ ] зубы" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_the_undo_window_starts_when_the_write_happens(bot):
    """A web search can spend minutes before anything is written, and undo used to expire
    during it."""
    task(bot)
    real = bot.vault.handle

    async def slow(*args, **kwargs):
        turn = await real(*args, **kwargs)
        bot.clock.advance(240)
        return turn

    bot.vault.handle = slow
    created = await bot.orch.handle_text(CHAT, USER, "зубы")
    bot.clock.advance(120)  # 6 minutes after the message, 2 after the write

    assert (await bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}")).text \
        == texts.UNDONE


async def test_an_undo_forgets_a_fix_that_was_asked_for(staged_bot):
    bot = staged_bot
    _milk_task(bot.script)
    first = await bot.orch.handle_text(CHAT, USER, "надо молоко")
    await bot.orch.handle_callback(CHAT, USER, f"f:{first.undo_id}")
    await bot.orch.undo(CHAT)

    bot.script.answers.update(TARGET_PROMPT={"target": "g"},
                              GROCERY_PROMPT={"names": ["хлеб"]})
    reply = await bot.orch.handle_text(CHAT, USER, "купить хлеб")
    assert "FIX_PROMPT" not in bot.script.stages
    assert reply.text.startswith("✅ Obsidian —")


# ---- it never raises, and one chat never holds up another ---------------------------------------

async def test_two_chats_are_not_serialised_against_each_other(bot):
    """The lock is per chat_id: one chat waiting on a model must not hold up another."""
    entered: list[str] = []
    release = asyncio.Event()

    async def handle(message, **kwargs):
        entered.append(message)
        if len(entered) == 1:  # the first chat parks inside the guarded section
            await release.wait()
        return VaultTurn()

    bot.vault.handle = handle
    first = asyncio.create_task(bot.orch.handle_text(CHAT, USER, "первое"))
    await asyncio.sleep(0)
    second = asyncio.create_task(bot.orch.handle_text(CHAT + 1, USER, "второе"))
    await asyncio.wait_for(second, timeout=1)  # would deadlock behind one global lock

    release.set()
    await asyncio.wait_for(first, timeout=1)
    assert len(entered) == 2


async def test_the_lock_map_does_not_grow_with_the_number_of_chats(bot):
    for chat in range(5):
        await bot.orch.undo(chat)
    assert bot.orch._locks == {} and bot.orch._waiting == {}


async def test_an_unexpected_vault_failure_becomes_an_internal_reply(bot):
    async def boom(*args, **kwargs):
        raise RuntimeError("kaput")

    bot.vault.handle = boom
    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert reply.text == texts.ERRORS["INTERNAL"] and reply.buttons == []
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "INTERNAL"


async def test_an_audit_store_that_cannot_open_the_event_still_replies(bot):
    bot.store.close()  # every insert now raises: locked database, full disk, ...

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert reply.text == texts.ERRORS["INTERNAL"]
    assert rows(bot, "events") == [] and bot.claude.seen == []


async def test_an_audit_store_that_cannot_close_the_event_still_replies(bot):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    task(bot)
    bot.store.update_event = boom

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert reply.text.startswith("✅ Obsidian —")  # the work is done and the answer is earned


def test_the_orchestrator_holds_no_russian_literals():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "conversation" / "orchestrator.py").read_text(encoding="utf-8")
    assert not re.search(r"[Ѐ-ӿ]", source), "every user string belongs in app/texts.py"
