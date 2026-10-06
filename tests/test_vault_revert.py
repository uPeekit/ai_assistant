"""Taking one write back out of a text that has changed since (app/vault/revert.py)."""

from __future__ import annotations

from app.vault.revert import take_back

BEFORE = "## дом\n\n- [ ] счета\n\n## разное\n\n- [ ] посылка\n"


def test_an_untouched_file_goes_back_exactly():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    assert take_back(BEFORE, written, written) == BEFORE


def test_a_line_added_elsewhere_since_is_kept():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    now = written.replace("- [ ] посылка\n", "- [ ] посылка\n- [ ] позвонить\n")
    assert take_back(BEFORE, written, now) == BEFORE.replace(
        "- [ ] посылка\n", "- [ ] посылка\n- [ ] позвонить\n")


def test_a_line_added_right_after_ours_is_kept():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    now = written.replace("- [ ] лампочки\n", "- [ ] лампочки\n- [ ] позвонить\n")
    assert take_back(BEFORE, written, now) == BEFORE.replace(
        "- [ ] счета\n", "- [ ] счета\n- [ ] позвонить\n")


def test_a_tick_is_put_back_when_another_line_was_ticked_since():
    before = "- [x] молоко\n- [x] яйца\n- [x] хлеб\n"
    written = before.replace("- [x] молоко", "- [ ] молоко")
    now = written.replace("- [x] хлеб", "- [ ] хлеб")
    assert take_back(before, written, now) == "- [x] молоко\n- [x] яйца\n- [ ] хлеб\n"


def test_our_line_changed_since_is_a_conflict():
    before = "- [x] молоко\n- [x] яйца\n"
    written = before.replace("- [x] молоко", "- [ ] молоко")
    now = written.replace("- [ ] молоко", "- [ ] молоко 2 л")
    assert take_back(before, written, now) is None


def test_our_line_deleted_since_is_a_conflict():
    written = BEFORE.replace("- [ ] счета\n", "- [ ] счета\n- [ ] лампочки\n")
    now = written.replace("- [ ] лампочки\n", "")
    assert now == BEFORE and take_back(BEFORE, written, now) is None


def test_a_removed_line_goes_back_where_it_was():
    written = BEFORE.replace("- [ ] счета\n", "")
    now = written + "- [ ] новое\n"
    assert take_back(BEFORE, written, now) == BEFORE + "- [ ] новое\n"


def test_a_removed_line_with_nothing_left_around_it_is_a_conflict():
    assert take_back("старое", "", "совсем другое") is None


def test_links_added_since_do_not_hide_our_line():
    written = BEFORE + "- [ ] дочитать Чапаев и Пустота\n"
    now = written.replace("Чапаев и Пустота", "[[Чапаев и Пустота]]")
    assert take_back(BEFORE, written, now) == BEFORE
    aliased = written.replace("Чапаев и Пустота", "[[Чапаев и Пустота|чапаев и пустота]]")
    assert take_back(BEFORE, written, aliased) == BEFORE


def test_a_whole_rewrite_changed_since_is_a_conflict():
    before = "один\nдва\nтри\n"
    written = "совсем\nновый\nтекст\n"
    assert take_back(before, written, written + "приписка\n") == before + "приписка\n"
    assert take_back(before, written, "совсем\nдругой\nтекст\n") is None


def test_a_created_file_is_emptied_of_what_the_write_put_there():
    written = "- первое\n"
    assert take_back("", written, written + "- второе\n").strip() == "- второе"
    assert take_back("", written, written) == ""
