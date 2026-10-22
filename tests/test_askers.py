"""The names people go by in the owner's records (app/vault/askers.py)."""

from __future__ import annotations

from app.vault.askers import WAIT_S, Askers


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_a_short_reply_to_the_question_back_is_the_name_and_returns_the_question(tmp_path):
    clock = Clock()
    askers = Askers(tmp_path / "askers.json", clock=clock)
    askers.asked(1, 7, "какую оценку я поставил?")

    assert askers.reply(1, 7, "«саша в».") == "какую оценку я поставил?"
    assert askers.known(7) == ("саша в",)
    assert Askers(tmp_path / "askers.json").known(7) == ("саша в",)  # kept across restarts
    assert askers.reply(1, 7, "саша в") is None  # asked once, answered once


def test_anything_else_is_a_new_message_and_drops_the_question_back(tmp_path):
    clock = Clock()
    askers = Askers(tmp_path / "askers.json", clock=clock)
    for later, said in ((0, "а что у меня на пятницу?"),
                        (0, "купи молоко, хлеб и масло, и ещё яйца"),
                        (WAIT_S + 1, "саша в")):
        askers.asked(1, 7, "какую оценку я поставил?")
        clock.t += later
        assert askers.reply(1, 7, said) is None
    assert askers.reply(2, 7, "саша в") is None  # another chat was never asked
    assert askers.known(7) == ()


def test_an_unreadable_file_means_asking_again(tmp_path):
    path = tmp_path / "askers.json"
    path.write_text("{not json", encoding="utf-8")
    assert Askers(path).known(7) == ()


def test_the_words_before_a_name_are_not_part_of_it():
    askers = Askers()
    for said, name in (("я женя", "женя"), ("Меня зовут Саша.", "Саша"), ("я — Оль", "Оль"),
                       ("Яна", "Яна"), ("саша в", "саша в")):
        askers.asked(1, 7, "q")
        assert askers.reply(1, 7, said) == "q"
        assert askers.known(7)[-1] == name



def test_a_reply_with_two_names_keeps_both():
    """Asked how she is listed, she wrote «соня или лиза»: one name «соня или лиза» was kept,
    and nothing was found under it."""
    askers = Askers()
    askers.asked(-1, 7, "сколько раз я приходила?")
    assert askers.reply(-1, 7, "соня или лиза") == "сколько раз я приходила?"
    assert askers.known(7) == ("соня", "лиза") and askers.given(7) == ("соня", "лиза")
    askers.asked(-1, 8, "моя оценка?")
    askers.reply(-1, 8, "я Лиза, Лизок / Лиза К")
    assert askers.known(8) == ("Лиза", "Лизок", "Лиза К")
