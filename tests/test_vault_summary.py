"""What the Obsidian line says about each write: a short account built from the action itself
— the task, its tags and dates; the note, its folder and properties — not «задача в Задачи»."""

from __future__ import annotations

from datetime import date, datetime

from app import texts
from app.vault import summary
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultTurn
from app.vault.writer import VaultAction, VaultWrite, VaultWriter

TODAY = date(2026, 10, 8)  # a Thursday


def test_a_date_reads_as_today_tomorrow_or_a_weekday_and_day():
    assert summary.when("2026-10-08", TODAY) == "сегодня"
    assert summary.when("2026-10-09", TODAY) == "завтра"
    assert summary.when("2026-10-10", TODAY) == "сб 10.10"
    assert summary.when("2027-01-04", TODAY) == "пн 04.01.2027"  # another year says so
    assert summary.when("not a date", TODAY) == ""


def test_a_task_says_its_words_tags_date_and_repeat():
    action = VaultAction(action="task", text="платить счета", tags=["home"],
                         due="2026-10-10", repeat="every month", countdown=True)
    assert summary.task(action, TODAY, "отсчёт") == (
        "«платить счета» · #home #отсчёт · 📅 сб 10.10 · 🔁 every month")
    assert summary.task(VaultAction(action="task", text="позвонить маме"), TODAY, "x") == \
        "«позвонить маме»"


def test_a_note_says_its_folder_and_the_properties_it_was_given():
    props = {"author": "[[Авторы/Борис Пастернак]]", "status": "To read",
             "tags": ["books"], "event_posted": False}
    assert summary.note("Доктор Живаго", "Книги", props) == (
        "«Доктор Живаго» в «Книги» · Борис Пастернак · To read · #books")


def test_appended_lines_are_quoted_shortly():
    assert summary.lines(["Uncharted"]) == ": «Uncharted»"
    assert summary.lines(["первая", "вторая", "третья"]) == ": «первая» (+2)"
    long = "очень длинная строка " * 10
    assert len(summary.lines([long])) < 70


def test_an_update_says_what_changed():
    assert summary.update(VaultAction(action="update", note="Задачи", task="платить счета",
                                      done=True), TODAY) == ": ✓ «платить счета»"
    assert summary.update(VaultAction(action="update", note="Задачи", task="счета",
                                      due="2026-10-09"), TODAY) == ": «счета» → 📅 завтра"
    assert summary.update(VaultAction(action="update", note="Бесы",
                                      props={"status": "Read"}), TODAY) == ": status → Read"


def test_one_write_is_one_line_and_several_are_a_list(tmp_path):
    (tmp_path / "Задачи.md").write_text("## дом\n\n- [ ] старое\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()
    writer = VaultWriter(index, now=lambda: datetime(2026, 10, 8, 12, 0))
    one = writer.run(VaultAction(action="task", text="постирать", tags=["home"],
                                 due="2026-10-08", heading="дом"))
    assert VaultTurn(writes=[one]).reply_line() == \
        "✅ Obsidian — задача «постирать» · #home · 📅 сегодня"

    many = [writer.run(VaultAction(action="task", text=f"дело {i}", due="2026-10-09"))
            for i in range(10)]
    reply = VaultTurn(writes=many).reply_line().splitlines()
    assert reply[0] == "✅ Obsidian:"
    assert reply[1] == "• задача «дело 0» · 📅 завтра"
    assert len(reply) == 1 + summary.MAX_LISTED + 1
    assert reply[-1] == texts.VAULT_LIST_MORE.format(n=10 - summary.MAX_LISTED)


def test_a_write_without_a_detail_still_reads_as_before():
    """Writes made elsewhere (the fix flow, older undo records) carry no detail."""
    assert VaultWrite(kind="task", path="Задачи.md", note="Задачи").what == "задача в «Задачи»"
