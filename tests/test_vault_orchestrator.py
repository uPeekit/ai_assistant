"""Both stores on one message: Notion as before, Obsidian next to it, and one Undo for both."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import anthropic
import pytest

from app import texts
from app.audit.store import AuditStore
from app.commands.executor import Executor
from app.config import Settings
from app.conversation.orchestrator import Orchestrator
from app.conversation.session import SessionStore
from app.llm.context import ContextBuilder
from app.llm.health import Health
from app.switches import Switches
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator
from app.vault.filer import Filer
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.writer import VaultWriter
from tests.fakes import FakeDiscovery, FakeLLM, FakeNotionProvider
from tests.helpers import cand, make_interp, val
from tests.test_orchestrator import CHAT, NOW, TOKEN, USER, Clock, flagged
from tests.test_vault_filer import FakeAnthropic

VAULT_NOW = datetime(2026, 9, 22, 18, 30)


@pytest.fixture
def bot(tmp_path, env):
    vault_dir = tmp_path / "vault"
    (vault_dir / texts.VAULT_AREAS_DIR).mkdir(parents=True)
    (vault_dir / f"{texts.VAULT_AREAS_DIR}/дом.md").write_text("---\ntag: home\n---\n",
                                                                encoding="utf-8")
    (vault_dir / f"{texts.VAULT_TASKS_NOTE}.md").write_text("## дом\n\n- [ ] счета #home\n",
                                                             encoding="utf-8")
    snap = flagged("t3")
    db = tmp_path / "bot.sqlite"
    store = AuditStore(db)
    store.migrate()
    settings = Settings(notion_token=TOKEN, db_path=db, timezone="Europe/Tallinn",
                        items_per_target=15, session_ttl_s=900, undo_window_s=300)
    notion, llm = FakeNotionProvider(), FakeLLM()
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
    builder = ContextBuilder(settings.timezone, settings.items_per_target)
    orch = Orchestrator(settings, FakeDiscovery(snap), builder, llm, SemanticValidator(),
                        Policy(Thresholds.from_settings(settings)), Executor(notion), store,
                        SessionStore(store), clock=Clock(NOW), vault=vault, health=health)
    yield type("VaultBot", (), {
        "orch": orch, "llm": llm, "claude": claude, "index": index, "store": store,
        "notion": notion,
        "ctx": builder.build(snap, now=NOW), "db": db, "vault": vault, "dir": vault_dir,
    })()
    store.close()


def executions(bot) -> list[dict]:
    con = sqlite3.connect(bot.db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM executions ORDER BY id")]
    finally:
        con.close()


async def test_one_message_reaches_both_stores_and_one_undo_reverts_both(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("купить лампочки", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))
    bot.claude.answers = [{"actions": [{"action": "task", "text": "купить лампочки",
                                         "heading": "дом", "tags": ["home"]}]}]

    reply = await bot.orch.handle_text(CHAT, USER, "купить лампочки")

    assert "Obsidian —" in reply.text  # the Notion answer, plus one line about the vault
    tasks = bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] купить лампочки #home" in tasks
    assert any(c[0] == "create_page" for c in bot.notion.calls)  # and Notion has its row

    [row] = executions(bot)
    undo = json.loads(row["undo"])
    assert undo["kind"] == "archive" and len(undo["vault"]) == 1

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{row['id']}")
    assert undone.text == texts.UNDONE
    assert "купить лампочки" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_the_vault_is_written_even_when_notion_asks_a_question(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95,
                                              fields={"t3.f1": val("зубы", 1.0)})))
    bot.claude.answers = [{"actions": [{"action": "task", "text": "зубы", "heading": "дом"}]}]

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert reply.buttons and "Obsidian —" in reply.text  # Notion is still asking
    assert "- [ ] зубы" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    # Nothing was written to Notion, so the vault's undo gets a row of its own for /undo.
    [row] = executions(bot)
    undo = json.loads(row["undo"])
    assert undo["kind"] == "vault" and len(undo["vault"]) == 1

    undone = await bot.orch.undo(CHAT)
    assert undone.text == texts.UNDONE
    assert "зубы" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_an_answer_to_a_question_does_not_reach_the_vault_again(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95,
                                              fields={"t3.f1": val("зубы", 1.0)})))
    bot.claude.answers = [{"actions": [{"action": "task", "text": "зубы", "heading": "дом"}]}]
    question = await bot.orch.handle_text(CHAT, USER, "зубы")
    assert question.buttons

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("зубы", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "приоритет A")

    assert len(bot.claude.seen) == 1  # the filer read the message, not the answer to Notion
    assert bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md").count("- [ ] зубы") == 1


async def test_notion_off_leaves_a_working_obsidian_bot(bot, tmp_path):
    """The switch the whole design exists for: no Notion call, no question, the vault answers."""
    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
    bot.claude.answers = [{"actions": [{"action": "task", "text": "зубы", "heading": "дом"}]}]

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert reply.text.startswith("✅ Obsidian —")
    assert bot.llm.calls == 0 and bot.notion.calls == []
    assert "- [ ] зубы" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    [row] = executions(bot)
    assert json.loads(row["undo"])["kind"] == "vault"
    # No question, so the only button is Undo for the vault's own write.
    assert [[b.id for b in r] for r in reply.buttons] == [[f"u:{row['id']}"]]
    assert reply.undo_id == row["id"]

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{row['id']}")
    assert undone.text == texts.UNDONE
    assert "зубы" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_obsidian_off_leaves_the_notion_bot_exactly_as_it_was(bot, tmp_path):
    bot.orch._switches = Switches(tmp_path / "switches.json", {"obsidian": False})
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("зубы", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert "✅" in reply.text and "Obsidian" not in reply.text
    assert bot.claude.seen == []  # the filer was never asked
    assert "зубы" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_both_off_says_so_rather_than_swallowing_the_message(bot, tmp_path):
    bot.orch._switches = Switches(tmp_path / "switches.json",
                                  {"notion": False, "obsidian": False})
    reply = await bot.orch.handle_text(CHAT, USER, "зубы")
    assert reply.text == texts.ERRORS["NOTHING_ENABLED"]


async def test_a_vault_failure_never_costs_the_notion_answer(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("зубы", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))
    bot.claude.answers = [RuntimeError("vault on fire")]

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert "✅" in reply.text and reply.undo_id is not None  # Notion wrote and can be undone
    assert "Obsidian" not in reply.text  # nothing to report: the vault side never got that far
    assert json.loads(executions(bot)[0]["undo"])["vault"] == []


async def test_an_account_out_of_credit_is_named_in_the_reply(bot):
    """The whole of feature A, end to end: Notion still answers (the local model read the
    message), the Obsidian line says what went wrong in words, and the reply carries the
    warning once — not once per message for the rest of the evening."""
    error = anthropic.APIError("boom", request=None,  # type: ignore[arg-type]
                               body={"error": {"message": "Your credit balance is too low"}})
    error.status_code = 400  # type: ignore[attr-defined]
    for _ in range(2):
        bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
            "t3.f1": val("зубы", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))
    bot.claude.answers = [error, error]

    first = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert "✅ Notion —" in first.text  # the Notion side is unharmed
    assert texts.LLM_DOWN_SHORT["credit"] in first.text  # the Obsidian line names the cause
    assert texts.LLM_DOWN_NOTE["credit"] in first.text  # and the reply says what to do

    second = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert texts.LLM_DOWN_SHORT["credit"] in second.text  # still says why nothing was written
    assert texts.LLM_DOWN_NOTE["credit"] not in second.text  # but does not repeat the lecture


# ---- multi-step plans reach both stores -------------------------------------------------------


class ByMessage:
    """A filer that answers by what it was asked, not by the order it was asked in.

    A plan runs the goal's own vault call next to its steps' — deliberately, so nothing waits —
    so a queue of answers by position is a race, and a test written on one would pass or fail
    by scheduling luck."""

    def __init__(self, answers: dict[str, object], default: object = None) -> None:
        self.answers = answers
        self.default = default if default is not None else {"actions": []}
        self.seen: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        sent = kwargs["messages"][0]["content"]
        answer = next((a for k, a in self.answers.items() if k in sent), self.default)
        if isinstance(answer, Exception):
            raise answer
        return type("Resp", (), {
            "content": [type("Block", (), {"type": "text", "text": json.dumps(answer)})()],
            "stop_reason": "end_turn",
            "usage": type("U", (), {"input_tokens": 100, "output_tokens": 20})(),
        })()

    async def close(self) -> None:
        pass


def use(bot, answers: dict[str, object], default: object = None) -> ByMessage:
    client = ByMessage(answers, default)
    bot.orch._vault._filer._client = client
    bot.claude = client
    return client


async def test_a_plan_reaches_the_vault_once_from_the_message_and_one_button_undoes_both(bot):
    """The vault reads the message itself, once — not the planner's steps one by one.

    Steps are written for Notion («Добавь в TODO задачу «покушать»») and lose whatever the
    message said around them: «список дел на сегодня» became seven undated tasks. The filer
    already turns one message into several actions, with the dates it says."""
    from tests.test_orchestrator import FakePlanner, collect

    bot.orch._planner = FakePlanner(["добавь в покупки хлеб", "добавь в покупки молоко"])
    bot.llm.queue(make_interp("plan", cand(bot.ctx, "t3", 0.9)))
    for title in ("Хлеб", "Молоко"):
        bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                                 fields={"t2.f1": val(title, 1.0)})))
    client = use(bot, {"на сегодня": {"actions": [
        {"action": "task", "text": "хлеб", "heading": "дом", "due": "2026-09-22"},
        {"action": "task", "text": "молоко", "heading": "дом", "due": "2026-09-22"}]}})

    reply = await bot.orch.handle_text(CHAT, USER, "на сегодня купи хлеб и молоко",
                                       progress=collect([]))

    assert len(client.seen) == 1  # one filer call for the message, none per step
    tasks = bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] хлеб 📅 2026-09-22" in tasks and "- [ ] молоко 📅 2026-09-22" in tasks
    assert "Obsidian —" in reply.text  # reported once, under the plan's closing line

    # The vault's undo sits on the plan's own batch row: "Отменить всё" reverts both stores.
    batch_row = executions(bot)[-1]
    undo = json.loads(batch_row["undo"])
    assert undo["kind"] == "batch" and len(undo["batch"]) == 2 and undo["vault"]

    await bot.orch.handle_callback(CHAT, USER, reply.buttons[0][0].id)

    after = bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "хлеб" not in after and "молоко" not in after


async def test_an_answer_that_becomes_a_plan_does_not_write_the_vault_a_second_time(bot):
    """«добавь область таймлапсы» → Notion asked where; the vault had already written its
    note. The answer «новая страница в проектах» was read as a one-step plan, and the step
    wrote the vault again: «таймлапсы» and «Таймлапсы 2»."""
    from tests.test_orchestrator import FakePlanner, collect

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95,
                                              fields={"t3.f1": val("таймлапсы", 1.0)})))
    client = use(bot, {"таймлапсы": {"actions": [{
        "action": "note", "folder": texts.VAULT_AREAS_DIR, "title": "таймлапсы"}]}})
    question = await bot.orch.handle_text(CHAT, USER, "добавь область таймлапсы")
    assert question.buttons

    bot.orch._planner = FakePlanner(["создай в проектах страницу Таймлапсы"])
    bot.llm.queue(make_interp("plan", cand(bot.ctx, "t3", 0.9)))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Таймлапсы", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "новая страница в проектах", progress=collect([]))

    assert len(client.seen) == 1
    notes = sorted(p.name for p in (bot.dir / texts.VAULT_AREAS_DIR).glob("*.md"))
    assert notes == ["дом.md", "таймлапсы.md"]


async def test_text_a_step_already_found_is_written_to_the_vault_without_searching_again(bot):
    """The research is paid for once: the words the Notion side wrote are handed to the vault,
    which only decides where they go — in one write at the end of the plan."""
    from tests.test_orchestrator import FakePlanner, collect

    found = "Шведская стенка\nВысота 220 см\nШирина 80 см"
    bot.orch._planner = FakePlanner(["допиши на страницу Идеи что нашёл"])
    bot.llm.queue(make_interp("plan", cand(bot.ctx, "t3", 0.9)))
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, content=found)))
    client = use(bot, {"про стенку": {"actions": [{
        "action": "note", "folder": texts.VAULT_NOTES_DIR,
        "title": "Шведская стенка", "body": ["что-то своё"]}]}})

    await bot.orch.handle_text(CHAT, USER, "найди про стенку и допиши", progress=collect([]))

    notes = sorted(p.name for p in (bot.dir / texts.VAULT_NOTES_DIR).glob("*.md"))
    assert notes == ["Шведская стенка.md"]  # one note, not one per call
    note = (bot.dir / texts.VAULT_NOTES_DIR / "Шведская стенка.md").read_text(encoding="utf-8")
    assert "Высота 220 см" in note and "Ширина 80 см" in note
    assert "что-то своё" not in note  # the filer picked the place, not the words
    # The model was never shown what the search found: that text is data, not a prompt.
    assert found not in json.dumps(client.seen, ensure_ascii=False)


async def test_a_vault_failure_inside_a_plan_never_stops_the_plan(bot):
    from tests.test_orchestrator import FakePlanner, collect

    bot.orch._planner = FakePlanner(["добавь в покупки хлеб", "добавь в покупки молоко"])
    bot.llm.queue(make_interp("plan", cand(bot.ctx, "t3", 0.9)))
    for title in ("Хлеб", "Молоко"):
        bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                                 fields={"t2.f1": val(title, 1.0)})))
    use(bot, {}, default=RuntimeError("vault on fire"))

    reply = await bot.orch.handle_text(CHAT, USER, "купи хлеб и молоко", progress=collect([]))

    assert reply.text.startswith("🏁")  # the plan finished
    assert len(notion_calls_create(bot)) == 2  # both Notion steps ran


def notion_calls_create(bot) -> list:
    return [c for c in bot.notion.calls if c[0] == "create_page"]


# ---- a single message that searches the web reaches the vault too -----------------------------

async def test_what_a_search_found_is_written_to_the_vault_as_well(bot):
    """The reported failure: «найди картинки и добавь на страницу» filled the Notion page with
    images and left the note empty. The vault ran in parallel from the start, so it never saw a
    word of what the search found — it asked its editor to add pictures with no addresses to add,
    and got "no changes" back.

    Only a *plan* step used to hand its research over. A single message does now too."""
    from tests.test_orchestrator import FakeResearcher

    found = "## Изображения\n![тории](https://example.com/torii.jpg)"
    bot.orch._researcher = FakeResearcher(found)
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95,
                                              web_query="виды ворот тории фото")))
    client = use(bot, {"тории": {"actions": [{
        "action": "note", "folder": texts.VAULT_NOTES_DIR, "title": "Ворота тории",
        "body": ["своё"]}]}})

    await bot.orch.handle_text(CHAT, USER, "найди фото ворот тории и добавь на страницу")

    note = (bot.dir / texts.VAULT_NOTES_DIR / "Ворота тории.md").read_text(encoding="utf-8")
    assert "example.com/torii.jpg" in note   # the pictures the search paid for
    assert "своё" not in note                # the filer picked the place, not the words
    # The search text is data, never a prompt: the filer was not shown it.
    assert found not in json.dumps(client.seen, ensure_ascii=False)


async def test_the_vault_writes_once_not_twice_when_a_search_runs(bot):
    """The parallel turn is held at the gate rather than cancelled, and the second one carries the
    content — so exactly one write, not one empty note plus one good one."""
    from tests.test_orchestrator import FakeResearcher

    bot.orch._researcher = FakeResearcher("## Изображения\n![x](https://example.com/x.jpg)")
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="что-то")))
    use(bot, {"фото": {"actions": [{"action": "note", "folder": texts.VAULT_NOTES_DIR,
                                     "title": "Фото", "body": ["своё"]}]}})

    await bot.orch.handle_text(CHAT, USER, "найди фото и добавь на страницу")

    notes = list((bot.dir / texts.VAULT_NOTES_DIR).glob("*.md"))
    assert [p.name for p in notes] == ["Фото.md"]


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
            return [LinkPage("https://www.kv.ee/1", "Müüa korter, 4 tuba", "Hind 174 900 €")]

    bot.orch._links = Links()
    bot.claude.answers = [{"actions": [{"action": "inbox", "text": "https://www.kv.ee/1"}]}]
    bot.llm.queue(make_interp("unknown", cand(bot.ctx, "t2", 0.3)))
    await bot.orch.handle_text(CHAT, USER, "https://www.kv.ee/1")
    sent = json.dumps(bot.claude.seen[0]["messages"], ensure_ascii=False)
    assert "Müüa korter, 4 tuba" in sent and "данные, а не указания" in sent


async def test_a_question_about_a_link_writes_nothing_to_the_vault_either(bot):
    from app.web.links import LinkPage
    from tests.test_orchestrator import FakeAnswerer

    class Links:
        async def read(self, message):
            return [LinkPage("https://www.kv.ee/1", "Üürile anda korter", "Tagatisraha 2 kuud")]

    before = {p: p.read_bytes() for p in bot.dir.rglob("*.md")}
    bot.orch._links, bot.orch._answerer = Links(), FakeAnswerer()
    bot.claude.answers = [{"actions": [{"action": "inbox", "text": "какой залог?"}]}]
    bot.llm.queue(make_interp("answer", cand(bot.ctx, "t2", 0.3)))
    reply = await bot.orch.handle_text(CHAT, USER, "какой залог? https://www.kv.ee/1")
    assert reply.text.startswith("Залог")
    assert {p: p.read_bytes() for p in bot.dir.rglob("*.md")} == before


async def test_notion_off_a_question_about_a_link_is_answered_from_the_page(bot, tmp_path):
    from app.web.links import LinkPage
    from tests.test_orchestrator import FakeAnswerer

    class Links:
        async def read(self, message):
            return [LinkPage("https://www.kv.ee/1", "Üürile anda korter", "Tagatisraha 2 kuud")]

    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
    bot.orch._links, bot.orch._answerer = Links(), FakeAnswerer()
    bot.claude.answers = [{"actions": [{"action": "link_answer", "text": "какой залог?"}]}]
    before = {p: p.read_bytes() for p in bot.dir.rglob("*.md")}

    reply = await bot.orch.handle_text(CHAT, USER, "какой залог? https://www.kv.ee/1")

    assert "Залог — две месячные платы." in reply.text
    assert {p: p.read_bytes() for p in bot.dir.rglob("*.md")} == before


async def test_with_both_sides_off_no_link_is_read(bot, tmp_path):
    """Nothing would use the page, and the reader is a third party."""
    from tests.test_orchestrator import FakeLinks

    bot.orch._switches = Switches(tmp_path / "switches.json",
                                  {"notion": False, "obsidian": False})
    bot.orch._links = links = FakeLinks([])
    reply = await bot.orch.handle_text(CHAT, USER, "https://www.kv.ee/1")
    assert reply.text == texts.ERRORS["NOTHING_ENABLED"] and links.asked == []


async def test_notion_off_the_pages_read_are_audited_too(bot, tmp_path):
    from app.web.links import LinkPage
    from tests.test_orchestrator import FakeLinks, closed_events

    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
    bot.orch._links = FakeLinks([LinkPage("https://www.kv.ee/1", "Korter", "Hind 174 900 €")])
    bot.claude.answers = [{"actions": [{"action": "inbox", "text": "https://www.kv.ee/1"}]}]
    await bot.orch.handle_text(CHAT, USER, "https://www.kv.ee/1")
    (event,) = closed_events(bot, ["text"])
    assert json.loads(event["llm_context"])["links"][0]["title"] == "Korter"


# ---- undo takes back one message, not the file ----------------------------------------------

async def test_undo_of_the_first_of_two_messages_keeps_the_second(bot, tmp_path):
    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
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
    bot.orch._switches = Switches(tmp_path / "switches.json", {"notion": False})
    bot.claude.answers = [{"actions": [{"action": "task", "text": "лампочки",
                                         "heading": "дом"}]}]
    reply = await bot.orch.handle_text(CHAT, USER, "лампочки")
    path = bot.dir / f"{texts.VAULT_TASKS_NOTE}.md"
    edited = path.read_text(encoding="utf-8").replace("- [ ] лампочки", "- [x] лампочки")
    path.write_text(edited, encoding="utf-8", newline="\n")

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")

    assert undone.text == texts.VAULT_UNDO_LEFT.format(notes=f"«{texts.VAULT_TASKS_NOTE}»")
    assert path.read_text(encoding="utf-8") == edited
