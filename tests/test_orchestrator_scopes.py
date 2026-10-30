"""A group chat with its own scope, through the orchestrator: vault only, narrowed; undo and fix
only for the member who asked (or an owner)."""

from __future__ import annotations

import pytest

from app import texts
from app.chats import ChatScope
from tests.test_orchestrator import bot, staged_bot  # noqa: F401 (fixtures)

GROUP = -100777
ALICE, BOB, OWNER = 501, 502, 1  # OWNER is in TELEGRAM_ALLOWED_USER_IDS (tests/conftest.py)
CLUB = ChatScope(chat_id=GROUP, title="club", rights=frozenset({"ask", "add", "tasks"}),
                 tag="клуб", folder="Клуб")


def task(script, text: str) -> None:
    script.answers.update(INTENT_PROMPT={"intent": "add"}, TARGET_PROMPT={"target": "t"},
                          TASKS_PROMPT={"items": [{"text": text, "due": "", "time": "",
                                                   "repeat": "", "heading": "дом", "tag": "home",
                                                   "countdown": False}], "lookup": ""})


@pytest.fixture
def group(staged_bot):  # noqa: F811
    staged_bot.orch._scopes = lambda chat_id: CLUB if chat_id == GROUP else None
    # The club's section exists, as it will after its first task. (Undoing the write that
    # made a heading, once another write went under it, is refused as a hand edit would be.)
    path = staged_bot.dir / f"{texts.VAULT_TASKS_NOTE}.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n## клуб\n\n- [ ] план #клуб\n",
                    encoding="utf-8")
    staged_bot.index.refresh()
    return staged_bot


def tasks(b) -> str:
    return b.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_a_group_message_reaches_only_the_vault_and_only_as_the_clubs(group):
    task(group.script, "купить чай")
    reply = await group.orch.handle_text(GROUP, ALICE, "купить чай")
    assert "- [ ] купить чай #клуб" in tasks(group)
    assert reply.undo_id


async def test_only_the_member_who_asked_or_an_owner_may_undo(group):
    task(group.script, "купить чай")
    first = await group.orch.handle_text(GROUP, ALICE, "купить чай")

    refused = await group.orch.handle_callback(GROUP, BOB, f"u:{first.undo_id}")
    assert refused.text == texts.SCOPE_NOT_YOURS and "купить чай" in tasks(group)

    done = await group.orch.handle_callback(GROUP, ALICE, f"u:{first.undo_id}")
    assert done.text == texts.UNDONE and "купить чай" not in tasks(group)

    task(group.script, "принести печенье")
    second = await group.orch.handle_text(GROUP, ALICE, "принести печенье")
    by_owner = await group.orch.handle_callback(GROUP, OWNER, f"u:{second.undo_id}")
    assert by_owner.text == texts.UNDONE


async def test_undo_in_a_group_takes_back_the_senders_own_latest_write(group):
    task(group.script, "купить чай")
    await group.orch.handle_text(GROUP, ALICE, "купить чай")
    task(group.script, "принести печенье")
    await group.orch.handle_text(GROUP, BOB, "принести печенье")

    await group.orch.undo(GROUP, user_id=ALICE)
    assert "купить чай" not in tasks(group) and "принести печенье" in tasks(group)


async def test_after_fix_is_pressed_only_the_pressers_next_message_is_the_fix(group):
    task(group.script, "купить чай")
    first = await group.orch.handle_text(GROUP, ALICE, "купить чай")
    asked = await group.orch.handle_callback(GROUP, ALICE, f"f:{first.undo_id}")
    assert asked.text == texts.FIX_ASK

    # Bob speaks in between: his message is his own, not Alice's correction.
    task(group.script, "принести печенье")
    await group.orch.handle_text(GROUP, BOB, "принести печенье")
    assert "принести печенье #клуб" in tasks(group)

    group.script.answers["FIX_PROMPT"] = {
        "changes": [{"key": "a1", "op": "set", "field": "text", "prop": "",
                     "value": "купить зелёный чай", "to": ""}], "add": [], "unclear": False}
    fixed = await group.orch.handle_text(GROUP, ALICE, "купить зелёный чай")
    assert fixed.text.startswith(texts.FIX_DONE.split("{")[0])
    assert "купить зелёный чай #клуб" in tasks(group)


async def test_another_member_may_not_press_fix_on_alices_write(group):
    task(group.script, "купить чай")
    first = await group.orch.handle_text(GROUP, ALICE, "купить чай")
    refused = await group.orch.handle_callback(GROUP, BOB, f"f:{first.undo_id}")
    assert refused.text == texts.SCOPE_NOT_YOURS


async def test_the_owners_private_chat_is_untouched_by_the_groups_scope(group):
    task(group.script, "купить лампочки")
    await group.orch.handle_text(OWNER, OWNER, "купить лампочки")
    assert "- [ ] купить лампочки #home" in tasks(group)


async def test_a_reply_to_another_members_write_is_a_new_message_not_a_refused_fix(group):
    task(group.script, "купить чай")
    first = await group.orch.handle_text(GROUP, ALICE, "купить чай")
    group.store.set_reply_message_id(first.undo_id, 6001)
    task(group.script, "принести печенье")
    await group.orch.handle_text(GROUP, BOB, "принести печенье", reply_to=6001)
    assert "принести печенье #клуб" in tasks(group) and "купить чай #клуб" in tasks(group)


async def test_one_members_fix_press_does_not_cancel_anothers(group):
    task(group.script, "купить чай")
    alices = await group.orch.handle_text(GROUP, ALICE, "купить чай")
    task(group.script, "принести печенье")
    bobs = await group.orch.handle_text(GROUP, BOB, "принести печенье")
    await group.orch.handle_callback(GROUP, ALICE, f"f:{alices.undo_id}")
    await group.orch.handle_callback(GROUP, BOB, f"f:{bobs.undo_id}")  # Bob's own press

    group.script.answers["FIX_PROMPT"] = {
        "changes": [{"key": "a1", "op": "set", "field": "text", "prop": "",
                     "value": "купить зелёный чай", "to": ""}], "add": [], "unclear": False}
    fixed = await group.orch.handle_text(GROUP, ALICE, "купить зелёный чай")
    assert fixed.text.startswith(texts.FIX_DONE.split("{")[0])


async def test_refusing_another_members_write_is_a_notice_for_the_presser_alone(group):
    task(group.script, "купить чай")
    first = await group.orch.handle_text(GROUP, ALICE, "купить чай")
    refused = await group.orch.handle_callback(GROUP, BOB, f"u:{first.undo_id}")
    assert refused.notice and refused.text == texts.SCOPE_NOT_YOURS


async def test_a_group_no_longer_listed_is_never_treated_as_a_private_chat(group):
    group.orch._scopes = lambda chat_id: None  # removed between the gate and the turn
    task(group.script, "купить чай")
    reply = await group.orch.handle_text(GROUP, ALICE, "купить чай")
    assert reply.text == "" and "купить чай" not in tasks(group)
