"""The vault index, the markdown edits, and the writer that puts them on disk."""

from __future__ import annotations

from datetime import datetime

import pytest

from app import texts
from app.vault import frontmatter, mdedit
from app.vault.index import VaultIndex, parse
from app.vault.writer import VaultAction, VaultUndo, VaultWriter

NOW = datetime(2026, 9, 22, 18, 30)


@pytest.fixture
def vault(tmp_path):
    (tmp_path / texts.VAULT_AREAS_DIR).mkdir()
    (tmp_path / texts.VAULT_BOOKS_DIR).mkdir()
    (tmp_path / f"{texts.VAULT_AREAS_DIR}/дом.md").write_text(
        "---\ntag: home\n---\n\n## Покупки\n\n- [ ] лампочки\n- [ ] шуруповёрт\n\n## Заметки\n\n"
        "тут просто текст\n", encoding="utf-8")
    (tmp_path / f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md").write_text(
        "---\nstatus: To read\nauthor: Пелевин\naliases:\n  - Чапаев\n---\n\nзаметки о книге\n",
        encoding="utf-8")
    (tmp_path / f"{texts.VAULT_TASKS_NOTE}.md").write_text(
        "## дом\n\n- [ ] платить счета #home 📅 2026-09-15\n\n## моя херня\n\n"
        "- [ ] забрать посылку #personal\n", encoding="utf-8")
    (tmp_path / "_bot.md").write_text("# Правила\n\nЗадачи — в Задачи.md\n", encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian/app.json").write_text("{}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def index(vault):
    index = VaultIndex(vault)
    index.refresh()
    return index


@pytest.fixture
def writer(index):
    return VaultWriter(index, now=lambda: NOW)


# ---- index -------------------------------------------------------------------------------

def test_index_reads_names_aliases_tags_headings_and_skips_settings(index):
    assert {n.name for n in index.notes} == {"дом", "Чапаев и Пустота", texts.VAULT_TASKS_NOTE,
                                             "_bot"}
    book = index.by_name("чапаев")  # an alias, and case does not matter
    assert book is not None and book.name == "Чапаев и Пустота"
    assert book.props["status"] == "To read"
    home = index.by_name("дом")
    assert home.tags == ("home",) and home.headings == ("Покупки", "Заметки")
    assert texts.VAULT_AREAS_DIR in index.folders() and "home" in index.tags()
    assert index.guide().startswith("# Правила")
    assert index.by_name("нет такой") is None


def test_index_refresh_is_incremental_and_notices_changes(index, vault):
    first = index.get(f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md")
    index.refresh()
    assert index.get(f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md") is first  # untouched
    (vault / f"{texts.VAULT_BOOKS_DIR}/новая.md").write_text("# привет\n", encoding="utf-8")
    (vault / f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md").unlink()
    index.refresh()
    assert index.by_name("новая") is not None and index.by_name("Чапаев") is None


def test_index_candidates_rank_by_shared_words(index):
    names = [n.name for n in index.candidates("дочитал чапаева и пустоту")]
    assert names and names[0] == "Чапаев и Пустота"
    assert index.candidates("qqq") == []


def test_parse_reads_inline_tags_but_not_headings_in_code():
    note = parse("x.md", "---\ntags: [книга]\n---\n\n#дом дела\n\n```\n# не заголовок\n```\n")
    assert set(note.tags) == {"книга", "дом"} and note.headings == ()


# ---- markdown edits ----------------------------------------------------------------------

def test_append_joins_the_list_under_the_named_heading(vault, index):
    text = index.read(f"{texts.VAULT_AREAS_DIR}/дом.md")
    out = mdedit.append_to(text, ["- розетка"], "Покупки")
    assert "- [ ] шуруповёрт\n- [ ] розетка\n" in out  # fitted to the tick boxes already there
    assert out.index("розетка") < out.index("## Заметки")


def test_append_without_a_list_adds_a_paragraph_and_can_make_the_heading(vault, index):
    text = index.read(f"{texts.VAULT_AREAS_DIR}/дом.md")
    out = mdedit.append_to(text, ["ещё строка"], "Заметки")
    assert out.endswith("тут просто текст\n\nещё строка\n")
    assert mdedit.append_to(text, ["строка"], "Нет такого") is None
    made = mdedit.append_to(text, ["строка"], "Нет такого", create_heading=True)
    assert made.endswith("## Нет такого\n\nстрока\n")


def test_numbered_lists_keep_counting():
    out = mdedit.append_to("1. раз\n2. два\n", ["- три"])
    assert out == "1. раз\n2. два\n3. три\n"


def test_a_new_task_keeps_its_own_tick_whatever_the_line_above_says():
    """The new line takes the list's *style* from its last item — and it used to take that
    item's tick too. The last task under «дом» was one the user had done, so every task filed
    there afterwards was written done: a conference on the 9th and a flat viewing on the 1st,
    missing from «Скоро» on the home page."""
    done_last = "- [ ] счета\n- [x] постирать ✅ 2026-09-28\n"
    out = mdedit.append_to(done_last, ["- [ ] Конференция 📅 2026-10-09"])
    assert out.endswith("- [x] постирать ✅ 2026-09-28\n- [ ] Конференция 📅 2026-10-09\n")
    # Plain words joining a task list become an open task, never a done one.
    assert mdedit.append_to(done_last, ["позвонить"]).endswith("\n- [ ] позвонить\n")
    # An update that ticks something keeps its tick.
    assert mdedit.append_to("- [ ] a\n", ["- [x] b"]).endswith("- [x] b\n")
    # A tick box never leaks into a plain list either.
    assert mdedit.append_to("- раз\n", ["- [ ] два"]).endswith("- [ ] два\n")


def test_find_task_refuses_to_guess_between_two():
    text = "- [ ] купить молоко\n- [ ] купить хлеб\n"
    assert mdedit.find_task(text, "купить молоко") == 0
    assert mdedit.find_task(text, "молоко купить") == 0  # same words, other order
    assert mdedit.find_task(text, "купить") is None  # matches both: no guess
    assert mdedit.find_task(text, "виски") is None


def test_set_task_ticks_and_redates_without_touching_the_rest():
    text = "- [ ] зубы #personal 🔁 every day 📅 2026-09-13\n"
    out = mdedit.set_task(text, 0, checked=True)
    assert out == "- [x] зубы #personal 🔁 every day 📅 2026-09-13\n"
    out = mdedit.set_task(text, 0, due="2026-10-01")
    assert out == "- [ ] зубы #personal 🔁 every day 📅 2026-10-01\n"


def test_replace_section_and_set_props():
    text = "---\nstatus: To read\n---\n\n## Итог\n\nстарое\n"
    assert "новое" in mdedit.replace_section(text, "Итог", ["новое"])
    assert mdedit.replace_section(text, "нет", ["x"]) is None
    out = mdedit.set_props(text, {"status": "Read", "author": "Пелевин"})
    assert "status: Read" in out and "author: Пелевин" in out
    assert "status" not in mdedit.set_props(out, {"status": None})


# ---- writer ------------------------------------------------------------------------------

def test_task_goes_under_its_area_heading_with_its_signifiers(writer, index):
    write = writer.run(VaultAction(action="task", text="купить лампочки", heading="дом",
                                    tags=["home"], due="2026-09-30", repeat="every month",
                                    countdown=True))
    text = index.read(write.path)
    line = "- [ ] купить лампочки #home #отсчёт 🔁 every month 📅 2026-09-30"
    assert line in text
    assert text.index(line) < text.index("## моя херня")  # under its own heading
    assert write.kind == "task" and write.undo.previous is not None


def test_task_with_an_unknown_heading_starts_one(writer, index):
    write = writer.run(VaultAction(action="task", text="покрасить забор", heading="дача"))
    assert index.read(write.path).endswith("## дача\n\n- [ ] покрасить забор\n")


def test_note_is_created_with_properties_and_never_overwrites(writer, index):
    writer.run(VaultAction(action="note", folder=texts.VAULT_BOOKS_DIR, title="Чапаев и Пустота",
                           body=["другая книга"]))
    assert index.by_name("Чапаев и Пустота 2") is not None
    assert "заметки о книге" in index.read(f"{texts.VAULT_BOOKS_DIR}/Чапаев и Пустота.md")


def test_tags_given_as_a_property_are_written_as_obsidian_tags(writer, index):
    """«добавь область таймлапсы» made a note with `tags: '#timelapse'` — one string with a
    hash, which Obsidian does not read as a tag at all."""
    write = writer.run(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR,
                                   title="таймлапсы", props={"tags": "#timelapse, #video"},
                                   tags=["timelapse"]))
    props, _ = frontmatter.split(index.read(write.path))
    assert props["tags"] == ["timelapse", "video"]


def test_a_new_note_gets_the_tick_boxes_every_note_in_its_folder_has(tmp_path):
    """A meeting the bot filed had `event_posted: 'false'` — text, which a base shows as a
    word rather than a tick box — or no such property at all when the model left it out. What
    every note in a folder has as a tick box, a new note there gets too, unticked; that is read
    from the folder, not asked of a model."""
    from app.vault.index import VaultIndex
    from app.vault.writer import VaultWriter

    club = tmp_path / "Кнуб"
    club.mkdir()
    for name in ("2026-08-31 Сатанинское танго", "2026-09-21 Чёрные кувшинки"):
        (club / f"{name}.md").write_text(
            "---\nbook: x\nauthor: y\ndate: '2026-09-21'\nevent_posted: true\n"
            "vyvody_posted: false\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    writer = VaultWriter(index)

    write = writer.run(VaultAction(action="note", folder="Кнуб", title="Доктор Живаго",
                                   props={"book": "Доктор Живаго", "event_posted": "false"}))
    props, _ = frontmatter.split(index.read(write.path))
    assert props == {"book": "Доктор Живаго", "event_posted": False, "vyvody_posted": False}

    # A folder whose notes share no tick box adds nothing.
    other = writer.run(VaultAction(action="note", folder="Заметки", title="идея"))
    assert frontmatter.split(index.read(other.path))[0] == {}


def test_update_sets_properties_and_ticks_a_task(writer, index):
    write = writer.run(VaultAction(action="update", note="Чапаев",
                                    props={"status": "Read", "author": ""}))
    text = index.read(write.path)
    assert "status: Read" in text and "author" not in text

    write = writer.run(VaultAction(action="update", note=texts.VAULT_TASKS_NOTE,
                                    task="забрать посылку", done=True))
    assert "- [x] забрать посылку #personal" in index.read(write.path)


def test_an_action_that_cannot_be_carried_out_becomes_an_inbox_line(writer, index):
    write = writer.run(VaultAction(action="update", note="нет такой заметки", text="что-то"))
    assert write.kind == "inbox" and write.note == texts.VAULT_INBOX_NOTE
    assert "- что-то" in index.read(write.path)
    # an update naming a real note but changing nothing is not silently a no-op either
    assert writer.run(VaultAction(action="update", note="дом")).kind == "inbox"


def test_log_writes_todays_daily_note(writer, index):
    write = writer.run(VaultAction(action="log", text="ходили в лес"))
    assert write.path == f"{texts.VAULT_DAILY_DIR}/2026-09-22.md"
    assert index.read(write.path).strip() == "- 18:30 ходили в лес"


def test_undo_restores_text_and_trashes_what_was_created(writer, index, vault):
    before = index.read(f"{texts.VAULT_AREAS_DIR}/дом.md")
    write = writer.run(VaultAction(action="append", note="дом", heading="Покупки",
                                    body=["- розетка"]))
    writer.undo(write.undo)
    assert index.read(write.path) == before

    created = writer.run(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR, title="Идея",
                                      body=["текст"]))
    writer.undo(created.undo)
    assert not (vault / created.path).exists()
    assert (vault / ".trash/Идея.md").read_text(encoding="utf-8").strip() == "текст"
    assert index.by_name("Идея") is None


def test_the_writer_stays_inside_the_vault(writer):
    for bad in ("../x.md", ".obsidian/app.json"):
        with pytest.raises(ValueError):
            writer.undo(VaultUndo(path=bad, previous="x"))


def test_syncthings_archive_is_not_a_note(tmp_path):
    """Found live: Syncthing keeps every file it replaced in `.stversions`, named
    "Главная~20260923-170805.md". Indexed, those show up as notes of their own — the filer
    offers a week-old copy as a place to write, and a search answers with three stale versions
    of the same page."""
    from app.vault.index import VaultIndex

    (tmp_path / ".stversions").mkdir()
    (tmp_path / ".stversions/Главная~20260923-170805.md").write_text("старое", encoding="utf-8")
    (tmp_path / "Главная.md").write_text("новое", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()

    assert [n.name for n in index.notes] == ["Главная"]


def test_templates_are_not_notes(tmp_path):
    """A Templater template is full of `<% %>` and empty properties: offered to the filer as a
    place to write, or returned by a search, it is noise at best — and a write into it breaks
    every note made from it afterwards."""
    from app.vault.index import VaultIndex
    from app.vault.writer import VaultWriter

    (tmp_path / texts.VAULT_TEMPLATES_DIR).mkdir()
    (tmp_path / f"{texts.VAULT_TEMPLATES_DIR}/Встреча Кнуба.md").write_text(
        "<%* const book = await tp.system.prompt('Книга'); -%>", encoding="utf-8")
    (tmp_path / "Главная.md").write_text("текст", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()

    assert [n.name for n in index.notes] == ["Главная"]
    with pytest.raises(ValueError):
        VaultWriter(index)._path(f"{texts.VAULT_TEMPLATES_DIR}/новая.md")


def test_the_writer_refuses_to_write_into_a_sync_folder(tmp_path):
    from app.vault.index import VaultIndex
    from app.vault.writer import VaultWriter

    writer = VaultWriter(VaultIndex(tmp_path))
    for rel in (".stversions/что-то.md", ".obsidian/plugins/x.md", ".stfolder/y.md"):
        with pytest.raises(ValueError):
            writer._path(rel)
    # Its own bin is the one exception: that is where undo puts a created note.
    assert writer._path(".trash/что-то.md").name == "что-то.md"



# ---- undo takes back one write, not the file ---------------------------------------------------

def test_undo_of_an_older_write_keeps_the_newer_one(writer, index):
    """Two tasks a minute apart, then Undo on the first: restoring the file's whole previous
    text also erased the second task."""
    first = writer.run(VaultAction(action="task", text="купить лампочки", heading="дом"))
    writer.run(VaultAction(action="task", text="позвонить маме", heading="дом"))

    assert writer.undo(first.undo) is True

    text = index.read(first.path)
    assert "купить лампочки" not in text
    assert "- [ ] позвонить маме" in text
    assert "- [ ] платить счета" in text  # what was there before either is untouched


def test_undo_leaves_a_file_alone_when_its_own_line_was_changed_since(writer, index):
    first = writer.run(VaultAction(action="task", text="купить лампочки", heading="дом"))
    edited = index.read(first.path).replace("- [ ] купить лампочки", "- [x] купить лампочки")
    (index.root / first.path).write_text(edited, encoding="utf-8", newline="\n")

    assert writer.undo(first.undo) is False
    assert index.read(first.path) == edited


def test_undo_of_the_write_that_made_a_file_keeps_what_came_after(writer, index, vault):
    """The inbox note did not exist; the first message made it, the second added a line. Undo
    of the first used to move the whole file — second line included — to the trash."""
    first = writer.run(VaultAction(action="inbox", text="первое"))
    writer.run(VaultAction(action="inbox", text="второе"))

    assert writer.undo(first.undo) is True

    assert index.read(first.path).strip() == "- второе"
    assert not (vault / ".trash" / f"{texts.VAULT_INBOX_NOTE}.md").exists()


def test_undo_survives_the_links_the_linker_added(writer, index):
    """The linker rewrites a line the bot wrote a moment later: `[[note|phrase]]` around a
    phrase. That is still the line this write added."""
    write = writer.run(VaultAction(action="append", note="дом", heading="Заметки",
                                    body=["спросить про Чапаев и Пустота"]))
    linked = index.read(write.path).replace("Чапаев и Пустота", "[[Чапаев и Пустота]]")
    (index.root / write.path).write_text(linked, encoding="utf-8", newline="\n")

    assert writer.undo(write.undo) is True
    assert "спросить про" not in index.read(write.path)


def test_undo_of_a_note_that_is_gone_is_not_an_error(writer, vault):
    created = writer.run(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR, title="Идея",
                                      body=["текст"]))
    (vault / created.path).unlink()
    assert writer.undo(created.undo) is True


def test_undo_reads_through_line_endings_another_program_wrote(writer, index):
    """Obsidian on the phone, through Syncthing, can write the file back with CRLF endings.
    The lines are the same lines."""
    first = writer.run(VaultAction(action="task", text="купить лампочки", heading="дом"))
    text = index.read(first.path) + "- [ ] позвонить маме\n"
    (index.root / first.path).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    assert writer.undo(first.undo) is True
    after = index.read(first.path)
    assert "купить лампочки" not in after and "позвонить маме" in after


# ---- each write remembers what it did --------------------------------------------------------

def test_a_write_remembers_the_action_it_carried_out(writer):
    write = writer.run(VaultAction(action="task", text="лампочки", heading="дом", due="2026-10-09"))
    assert write.undo.action == {"action": "task", "text": "лампочки", "heading": "дом",
                                 "due": "2026-10-09"}


def test_an_append_that_became_an_inbox_line_remembers_the_inbox_line(writer):
    write = writer.run(VaultAction(action="append", note="нет такой", body=["строка"]))
    assert write.kind == "inbox"
    assert write.undo.action == {"action": "inbox", "text": "строка"}


def test_a_grocery_write_remembers_its_products(writer):
    write = writer.run(VaultAction(action="grocery", body=["молоко", "хлеб"]))
    assert write.undo.action == {"action": "grocery", "body": ["молоко", "хлеб"]}


def test_replace_takes_back_old_writes_and_makes_new_ones_as_one_step(writer, index):
    old = writer.run(VaultAction(action="task", text="молоко", heading="дом"))
    writes, failed, left = writer.replace_writes(
        [old.undo], [VaultAction(action="grocery", body=["молоко"])])

    assert left == [] and failed == [] and [w.kind for w in writes] == ["grocery"]
    assert "молоко" not in index.read(old.path)
    assert "- [ ] молоко" in index.read(f"{texts.VAULT_GROCERIES_NOTE}.md")


def test_replace_touches_nothing_when_one_line_was_edited_since(writer, index, vault):
    a = writer.run(VaultAction(action="task", text="молоко", heading="дом"))
    b = writer.run(VaultAction(action="task", text="позвонить", heading="дом"))
    edited = index.read(b.path).replace("- [ ] позвонить", "- [x] позвонить")
    (vault / b.path).write_text(edited, encoding="utf-8", newline="\n")

    writes, failed, left = writer.replace_writes(
        [a.undo, b.undo], [VaultAction(action="log", text="сходил")])

    assert writes == [] and left == [texts.VAULT_TASKS_NOTE]
    assert index.read(a.path) == edited  # the milk task is still there too
    assert not (vault / texts.VAULT_DAILY_DIR).exists()  # and nothing new was written
