"""Going through the undated tasks one by one, and the weekly archive of done ones
(app/vault/cleanup.py): which tasks are asked about, and what each answer does to the file."""

from __future__ import annotations

from datetime import date

from app import texts
from app.vault import cleanup as c

TODAY = date(2026, 11, 21)
TASKS = """## дом

- [ ] смазать двери #home
- [ ] 18:00 посадить растения #home
- [ ] пылесос #home 📅 2026-11-25
- [ ] зубы #personal 🔁 every day 📅 2026-11-21
- [ ] стрижка #personal 🔁 every month
- [x] хлеб #home ✅ 2026-11-14

## моя херня

- [ ] врачи #personal
- [x] сегодняшнее #personal ✅ 2026-11-21
- [x] без даты сделано #personal
"""


def test_only_open_undated_unrepeated_tasks_are_asked_about_in_file_order():
    cards = c.cards(TASKS, kept=set())
    assert [(x.heading, x.line) for x in cards] == [
        ("дом", "смазать двери #home"), ("дом", "18:00 посадить растения #home"),
        ("моя херня", "врачи #personal")]


def test_a_task_kept_without_a_date_is_not_asked_about_again():
    kept = {c.key("смазать двери  #home")}  # spacing and case do not make it another task
    assert [x.line for x in c.cards(TASKS, kept)] == ["18:00 посадить растения #home",
                                                      "врачи #personal"]


def test_done_ticks_it_with_todays_date():
    text = c.done(TASKS, "смазать двери #home", TODAY)
    assert "- [x] смазать двери #home ✅ 2026-11-21" in text
    assert text.count("- [") == TASKS.count("- [")


def test_a_date_and_a_time_are_set_on_the_line():
    text = c.dated(TASKS, "смазать двери #home", "2026-11-28", "10:00")
    assert "- [ ] 10:00 смазать двери #home 📅 2026-11-28" in text


def test_a_rename_keeps_the_tags_and_the_time():
    text = c.renamed(TASKS, "18:00 посадить растения #home", "посадить кактус")
    assert "- [ ] 18:00 посадить кактус #home" in text


def test_a_move_puts_it_under_the_other_heading_with_its_tag():
    text = c.moved(TASKS, "смазать двери #home", "моя херня", "personal")
    head, tail = text.split("## моя херня")
    assert "смазать двери" not in head
    assert "- [ ] смазать двери #personal" in tail


def test_a_sections_tag_is_the_one_its_tasks_carry_most():
    assert c.section_tag(TASKS, "моя херня") == "personal"
    assert c.section_tag(TASKS, "дом") == "home"
    assert c.section_tag(TASKS, "нет такого") == ""


def test_dropping_it_takes_it_out_and_hands_back_the_cancelled_line():
    text, archived = c.dropped(TASKS, "врачи #personal", TODAY)
    assert "врачи" not in text
    assert archived == "- [-] врачи #personal ❌ 2026-11-21"


def test_a_line_changed_since_is_not_touched():
    assert c.done(TASKS, "смазать окна #home", TODAY) is None
    assert c.dropped(TASKS, "смазать окна #home", TODAY) is None


def test_the_archive_takes_every_ticked_task_but_todays():
    text, moved = c.archive_done(TASKS, TODAY)
    assert moved == ["- [x] хлеб #home ✅ 2026-11-14", "- [x] без даты сделано #personal"]
    assert "хлеб" not in text and "без даты сделано" not in text
    assert "- [x] сегодняшнее #personal ✅ 2026-11-21" in text  # Undo still works on it
    assert "- [ ] зубы #personal 🔁 every day" in text  # the open next one of a repeat stays


def test_a_batch_goes_on_top_of_the_archive_as_it_is():
    archive = "- [x] старое #home\n"
    assert c.prepended(archive, ["- [x] новое #home", "- [-] лишнее ❌ 2026-11-21"]) == (
        "- [x] новое #home\n- [-] лишнее ❌ 2026-11-21\n- [x] старое #home\n")
    assert c.prepended("", ["- [x] одно"]) == "- [x] одно\n"


def test_the_weekly_time_reads_a_day_and_a_time():
    assert c.weekly("пн 04:00") == (0, "04:00")
    assert c.weekly("Вс 22:30") == (6, "22:30")
    assert c.weekly("04:00") == (0, "04:00")  # no day: Monday
    assert c.weekly("") is None and c.weekly("когда-нибудь") is None


def test_the_archive_run_is_due_only_on_its_weekday():
    monday, tuesday = date(2026, 11, 23), date(2026, 11, 24)
    assert c.due_on("пн 04:00", monday) and not c.due_on("пн 04:00", tuesday)
    assert c.due_on("вт 22:00", tuesday)
    assert not c.due_on("", monday)  # empty: never


# ---- the weekly run, through the pipeline -------------------------------------------------------

def _pipeline(tmp_path, tasks: str):
    from app.vault.filer import Filer
    from app.vault.index import VaultIndex
    from app.vault.pipeline import VaultPipeline
    from app.vault.writer import VaultWriter
    from tests.test_vault_filer import FakeAnthropic

    (tmp_path / "Задачи.md").write_text(tasks, encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    return VaultPipeline(index, VaultWriter(index), Filer("", "haiku", client=FakeAnthropic()),
                         None, archive_log=tmp_path / "archive_state.json")


def test_the_weekly_run_moves_the_done_tasks_on_top_of_the_archive(tmp_path):
    (tmp_path / "Задачи — архив.md").write_text("- [x] старое #home\n", encoding="utf-8")
    pipeline = _pipeline(tmp_path, TASKS)
    assert pipeline.archive_week(TODAY) == 2
    archive = (tmp_path / "Задачи — архив.md").read_text(encoding="utf-8")
    assert archive.splitlines() == ["- [x] хлеб #home ✅ 2026-11-14",
                                    "- [x] без даты сделано #personal", "- [x] старое #home"]
    tasks = (tmp_path / "Задачи.md").read_text(encoding="utf-8")
    assert "хлеб" not in tasks and "- [ ] смазать двери #home" in tasks


def test_the_morning_says_how_many_went_that_day_and_only_that_day(tmp_path):
    pipeline = _pipeline(tmp_path, TASKS)
    pipeline.archive_week(TODAY)
    assert texts.ARCHIVE_LINE.format(n=2) in pipeline.digest(TODAY)
    assert texts.ARCHIVE_LINE.format(n=2) not in pipeline.digest(date(2026, 11, 22))


def test_nothing_done_moves_nothing_and_says_nothing(tmp_path):
    pipeline = _pipeline(tmp_path, "- [ ] одно #home\n")
    assert pipeline.archive_week(TODAY) == 0
    assert not (tmp_path / "Задачи — архив.md").exists()
    assert "📦" not in pipeline.digest(TODAY)
