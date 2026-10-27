"""A chat's part of the vault (app/vault/scope.py): what its readers see, what its gate lets
through, and what its writer may touch."""

from __future__ import annotations

import pytest

from app import texts
from app.chats import ChatScope
from app.vault.index import VaultIndex
from app.vault.scope import ScopedIndex, allows_path, gate
from app.vault.writer import VaultAction

CLUB = ChatScope(chat_id=-1, title="club", rights=frozenset({"ask", "add", "tasks"}),
                 tag="клуб", folder="Клуб", instruction="Встречи книжного клуба.")


@pytest.fixture
def index(tmp_path):
    (tmp_path / "Клуб").mkdir()
    (tmp_path / "Книги").mkdir()
    (tmp_path / "Клуб/Встреча клуба.md").write_text(
        "---\ntags: [клуб]\ndate: 2026-11-01\n---\n\nобщие планы\n", encoding="utf-8")
    (tmp_path / "Клуб/Доктор Живаго.md").write_text(  # at home, made without the tag
        "---\nbook: Доктор Живаго\n---\n\nмои выводы\n", encoding="utf-8")
    (tmp_path / "Клуб/Архив").mkdir()
    (tmp_path / "Клуб/Архив/Сатанинское танго.md").write_text(
        "---\nbook: Сатанинское танго\n---\n", encoding="utf-8")
    (tmp_path / "Клубный.md").write_text("не папка клуба\n", encoding="utf-8")
    (tmp_path / "Книги/Дюна.md").write_text("---\nstatus: Read\n---\n", encoding="utf-8")
    (tmp_path / "Заметка с тегом в тексте.md").write_text(
        "личное #клуб\n", encoding="utf-8")  # inline only: not shared as a whole note
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(
        "## дом\n\n- [ ] купить лампочки #home\n\n## club_my\n\n- [ ] выводы запостить #club_my\n"
        "\n## клуб\n\n- [ ] принести печенье #клуб\n- [ ] #клуб_старое не это\n",
        encoding="utf-8")
    (tmp_path / "_bot.md").write_text("личная инструкция\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return index


@pytest.fixture
def scoped(index):
    return ScopedIndex(index, CLUB)


def test_the_chat_sees_only_its_notes_and_its_task_lines(scoped):
    # Its tag in the properties, or anywhere in its home folder (the meetings a template made
    # without the tag); the tasks note for its lines.
    assert sorted(n.name for n in scoped.notes) == sorted([
        "Встреча клуба", "Доктор Живаго", "Сатанинское танго", texts.VAULT_TASKS_NOTE])
    assert "мои выводы" in scoped.read("Клуб/Доктор Живаго.md")
    tasks = scoped.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "принести печенье" in tasks and "## клуб" in tasks  # its own section's heading
    # Nothing of the owner's: no task, not even the names of the owner's sections.
    assert "лампочки" not in tasks and "club_my" not in tasks and "## дом" not in tasks
    assert "не это" not in tasks  # #клуб_старое is another tag
    assert scoped.open_tasks("печенье лампочки") == ["принести печенье #клуб"]
    with pytest.raises(FileNotFoundError):
        scoped.read("Книги/Дюна.md")
    assert scoped.guide() == "Встречи книжного клуба." and scoped.groceries() == ""
    assert scoped.folders() == ["Клуб"] and scoped.tags() == ["клуб"]
    assert scoped.by_name("Дюна") is None and scoped.by_name("Клубный") is None


def test_rights_decide_what_passes(scoped):
    actions = [
        VaultAction(action="search", text="встреча"),
        VaultAction(action="task", text="купить чай", tags=["home"], heading="дом",
                    countdown=True),
        VaultAction(action="update", note=texts.VAULT_TASKS_NOTE,
                    task="принести печенье #клуб", done=True),  # tick: not granted
        VaultAction(action="log", text="сегодня встречались"),
        VaultAction(action="grocery", body=["чай"]),
        VaultAction(action="inbox", text="что-то"),
        VaultAction(action="rewrite", note="Встреча клуба", text="сократи"),
        VaultAction(action="append", note="Встреча клуба", body=["ещё план"]),
        VaultAction(action="append", note="Дюна", body=["личное"]),  # out of scope
    ]
    kept, refused = gate(actions, scoped)
    assert [a.action for a in kept] == ["search", "task", "append"]
    task = kept[1]
    assert task.tags == ["клуб"] and task.heading == "клуб" and not task.countdown
    assert kept[2].note == "Встреча клуба"
    assert refused == ["update", "log", "grocery", "inbox", "rewrite", "append"]


def test_a_new_note_goes_home_with_the_tag_and_never_into_the_owners_namesake(scoped):
    kept, _ = gate([VaultAction(action="note", folder="Книги", title="Дюна",
                                tags=["книги"])], scoped)
    [note] = kept
    assert note.folder == "Клуб" and note.tags == ["клуб", "книги"]
    assert note.title == "Дюна (клуб)"
    kept, _ = gate([VaultAction(action="note", title="Новая встреча")], scoped)
    assert kept[0].title == "Новая встреча"


def test_ticking_only_the_chats_own_tasks_when_the_right_is_given(index):
    scoped = ScopedIndex(index, ChatScope(chat_id=-1, rights=frozenset({"tick"}), tag="клуб",
                                          folder="Клуб"))
    mine = VaultAction(action="update", note=texts.VAULT_TASKS_NOTE,
                       task="принести печенье #клуб", done=True)
    owners = VaultAction(action="update", note=texts.VAULT_TASKS_NOTE,
                         task="купить лампочки #home", done=True)
    kept, refused = gate([mine, owners], scoped)
    assert kept == [mine] and refused == ["update"]


def test_a_full_access_chat_is_not_narrowed(index):
    scoped = ScopedIndex(index, ChatScope(chat_id=-1, full=True))
    action = VaultAction(action="log", text="x")
    assert gate([action], scoped) == ([action], [])


def test_the_writer_may_touch_only_the_chats_files(scoped):
    allow = allows_path(scoped)
    assert allow(f"{texts.VAULT_TASKS_NOTE}.md") and allow("Клуб/Встреча клуба.md")
    assert allow("Клуб/Доктор Живаго.md")  # at home: the chat's, tag or not
    assert not allow("Книги/Дюна.md") and not allow("Дневник/2026-10-15.md")
    assert not allow("Клубный.md")
    assert allow("Клуб/Новая встреча.md")  # a new note at home ...
    assert allow("Клуб/Новая встреча.md")  # ... which the turn may write again
    assert not allow("Клуб/sub/deeper.md") and not allow("../outside.md")


# ---- from the branch review ------------------------------------------------------------------

def test_a_tick_is_pinned_to_the_chats_own_line_not_the_owners_namesake(index, tmp_path):
    path = tmp_path / f"{texts.VAULT_TASKS_NOTE}.md"
    path.write_text("## дом\n\n- [ ] Позвонить\n\n## клуб\n\n- [ ] Позвонить Ане #клуб\n",
                    encoding="utf-8")
    index.refresh()
    scoped = ScopedIndex(index, ChatScope(chat_id=-1, rights=frozenset({"tick"}),
                                          tag="клуб", folder="Клуб"))
    [kept], _ = gate([VaultAction(action="update", note=texts.VAULT_TASKS_NOTE,
                                  task="Позвонить", done=True)], scoped)
    assert kept.task == "Позвонить Ане #клуб"  # the writer's exact match is the chat's line


def test_the_task_file_never_takes_a_section_or_properties_from_a_chat(index):
    scoped = ScopedIndex(index, ChatScope(chat_id=-1, rights=frozenset({"change", "tick"}),
                                          tag="клуб", folder="Клуб"))
    section = VaultAction(action="update", note=texts.VAULT_TASKS_NOTE,
                          task="принести печенье #клуб", heading="дом", body=["стёрто"])
    props = VaultAction(action="update", note=texts.VAULT_TASKS_NOTE,
                        task="принести печенье #клуб", props={"x": "y"})
    assert gate([section, props], scoped)[0] == []


def test_the_writer_guard_lets_only_the_chats_own_lines_change_in_the_task_file(scoped):
    allow = allows_path(scoped)
    rel = f"{texts.VAULT_TASKS_NOTE}.md"
    old = "## дом\n\n- [ ] купить лампочки #home\n"
    assert allow(rel, old, old + "\n## клуб\n\n- [ ] чай #клуб\n")
    assert allow(rel, old + "- [ ] чай #клуб\n", old + "- [x] чай #клуб ✅ 2026-10-15\n")
    assert not allow(rel, old, old.replace("- [ ]", "- [x]"))  # the owner's task
    assert not allow(rel, old, old.replace("## дом", "## стёрто"))


def test_links_to_the_owners_notes_read_as_their_words(index, tmp_path):
    (tmp_path / "Клуб/Встреча клуба.md").write_text(
        "---\ntags: [клуб]\n---\n\nкак в [[Дюна|той книге]] и [[Дюна]], см. "
        "[[Встреча клуба]]\n", encoding="utf-8")
    index.refresh()
    text = ScopedIndex(index, CLUB).read("Клуб/Встреча клуба.md")
    assert "как в той книге и Дюна, см. [[Встреча клуба]]" in text


def test_a_tasks_words_lose_the_owners_hashtags(scoped):
    [task], _ = gate([VaultAction(action="task", text="купить чай #home #club_my")], scoped)
    assert task.text == "купить чай" and task.tags == ["клуб"]


def test_a_refused_reminder_takes_its_task_with_it(scoped):
    """«напомни 11 ноября …» in a chat without the reminder right: the reminders were refused
    and their two tasks written anyway — a «remind me» never leaves a lone task behind."""
    actions = [VaultAction(action="reminder", text="у сони презентация", due="2026-11-11"),
               VaultAction(action="task", text="у сони презентация", due="2026-11-11"),
               VaultAction(action="task", text="купить чай")]
    kept, refused = gate(actions, scoped)
    assert [a.text for a in kept] == ["купить чай"] and refused == ["reminder"]
