"""Watched folders (app/watch/folders.py): new finished files, read with the owner's instruction."""

from __future__ import annotations

import os
import time

import pytest

from app import texts
from app.llm import prompts as P
from app.vault.filer import FilerError
from app.watch.folders import FOLDER_SCHEMA, Folders, FolderWatch

INSTRUCTION = "tell me if something needs me"


class Clock:
    def __init__(self) -> None:
        self.t = time.time() + 3600  # well after every file the test writes

    def __call__(self) -> float:
        return self.t


class Ask:
    """The model: answers in turn and records what it was shown."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.shown: list[str] = []

    async def __call__(self, system, schema, content, max_tokens=0):
        assert system == P.FOLDER_PROMPT and schema == FOLDER_SCHEMA
        self.shown.append(content)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, 100, 10


OK = {"attention": False, "text": ""}


def setup(tmp_path, *answers, pattern="*", folders=None):
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    registry = Folders(tmp_path / "folders.json")
    registry.save(folders or [{"path": str(reports), "title": "Runs", "pattern": pattern,
                               "instruction": INSTRUCTION}])
    ask = Ask(*answers)
    clock = Clock()
    watch = FolderWatch(registry, tmp_path / "state.json", ask, clock=clock)
    return reports, watch, ask, clock


def write(folder, name, text, *, age_s=3600.0):
    path = folder / name
    path.write_text(text, encoding="utf-8")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


async def test_the_first_run_takes_a_baseline_and_sends_nothing(tmp_path):
    reports, watch, ask, _ = setup(tmp_path)
    write(reports, "run-1.md", "old report")

    assert await watch.run() == ""
    assert ask.shown == []
    assert await watch.run() == ""  # and the old file stays old


async def test_a_new_finished_file_is_read_with_the_owners_instruction(tmp_path):
    reports, watch, ask, _ = setup(tmp_path, {"attention": True, "text": "run-2: pay the bill"})
    await watch.run()
    write(reports, "run-2.md", "## Needs the owner\n- pay the bill")

    message = await watch.run()

    [shown] = ask.shown
    assert INSTRUCTION in shown and "run-2.md" in shown and "pay the bill" in shown
    assert message == texts.FOLDER_ATTENTION.format(title="Runs", text="run-2: pay the bill")


async def test_an_unfinished_file_waits_for_the_next_run(tmp_path):
    reports, watch, ask, clock = setup(tmp_path, OK)
    await watch.run()
    path = write(reports, "run-2.md", "half", age_s=0)
    os.utime(path, (clock.t - 60, clock.t - 60))  # changed a minute ago

    assert await watch.run() == ""
    clock.t += 20 * 60
    assert await watch.run() == texts.FOLDER_OK.format(title="Runs", n=1)


async def test_a_file_is_reported_once_even_if_it_changes_later(tmp_path):
    reports, watch, ask, _ = setup(tmp_path, OK)
    await watch.run()
    write(reports, "run-2.md", "first")
    await watch.run()
    write(reports, "run-2.md", "first, and more")

    assert await watch.run() == ""
    assert len(ask.shown) == 1


async def test_nothing_new_is_silence(tmp_path):
    reports, watch, ask, _ = setup(tmp_path)
    await watch.run()
    assert await watch.run() == ""


async def test_all_fine_is_one_short_line(tmp_path):
    reports, watch, _, _ = setup(tmp_path, OK)
    await watch.run()
    write(reports, "run-2.md", "fine")
    write(reports, "run-3.md", "fine too")

    assert await watch.run() == texts.FOLDER_OK.format(title="Runs", n=2)


async def test_a_failed_model_call_keeps_the_files_for_next_time(tmp_path):
    reports, watch, ask, _ = setup(tmp_path, FilerError("down"), OK)
    await watch.run()
    write(reports, "run-2.md", "report")

    assert await watch.run() == ""
    assert await watch.run() == texts.FOLDER_OK.format(title="Runs", n=1)
    assert len(ask.shown) == 2


async def test_a_missing_folder_is_a_status_error_and_others_still_run(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    folders = [{"path": str(tmp_path / "gone"), "title": "Gone", "instruction": "x"},
               {"path": str(good), "title": "Good", "instruction": "y"}]
    _, watch, _, _ = setup(tmp_path, OK, folders=folders)
    await watch.run()
    write(good, "a.md", "text")

    assert await watch.run() == texts.FOLDER_OK.format(title="Good", n=1)
    status = {s["title"]: s for s in watch.status()}
    assert status["Gone"]["ok"] is False and status["Gone"]["detail"] == texts.FOLDER_MISSING
    assert status["Good"]["ok"] is True


async def test_several_folders_with_news_make_one_message(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    folders = [{"path": str(a), "title": "A", "instruction": "x"},
               {"path": str(b), "title": "B", "instruction": "y"}]
    _, watch, _, _ = setup(tmp_path, OK, {"attention": True, "text": "fix it"},
                           folders=folders)
    await watch.run()
    write(a, "1.md", "one")
    write(b, "2.md", "two")

    assert await watch.run() == "\n\n".join([
        texts.FOLDER_OK.format(title="A", n=1),
        texts.FOLDER_ATTENTION.format(title="B", text="fix it")])


async def test_a_binary_file_is_skipped_and_a_long_one_cut(tmp_path):
    reports, watch, ask, _ = setup(tmp_path, OK)
    watch._max_chars = 50
    await watch.run()
    (reports / "img.bin").write_bytes(b"\x89PNG\x00\x01\x02")
    os.utime(reports / "img.bin", (time.time() - 3600,) * 2)
    write(reports, "long.md", "x" * 500)

    await watch.run()

    [shown] = ask.shown
    assert "img.bin" not in shown and "PNG" not in shown
    assert "x" * 50 in shown and "x" * 51 not in shown and texts.FOLDER_CUT in shown


async def test_the_pattern_keeps_other_files_out(tmp_path):
    reports, watch, ask, _ = setup(tmp_path, OK, pattern="run-*.md")
    await watch.run()
    write(reports, "README.md", "index")
    write(reports, "run-2.md", "report")

    await watch.run()

    assert "README" not in ask.shown[0] and "run-2.md" in ask.shown[0]


async def test_a_switched_off_folder_is_not_read(tmp_path):
    folders = [{"path": str(tmp_path / "reports"), "title": "Runs", "instruction": "x",
                "enabled": False}]
    reports, watch, ask, _ = setup(tmp_path, folders=folders)
    await watch.run()
    write(reports, "run-2.md", "report")
    assert await watch.run() == "" and ask.shown == []


def test_folders_registry_round_trip_and_bad_entry_raises(tmp_path):
    registry = Folders(tmp_path / "folders.json")
    assert registry.save([{"path": "C:/some/where", "title": "T", "instruction": "i"}]) == 1
    [folder] = Folders(tmp_path / "folders.json").all()
    assert folder.path == "C:/some/where" and folder.pattern == "*" and folder.enabled
    assert registry.dump() == [{"id": folder.id, "path": "C:/some/where", "title": "T",
                                "pattern": "*", "instruction": "i", "enabled": True}]
    with pytest.raises(ValueError):
        registry.save([{"title": "no path"}])


async def test_files_past_the_size_cap_wait_for_the_next_run(tmp_path):
    reports, watch, ask, _ = setup(tmp_path, OK, OK)
    watch._max_chars = 50
    await watch.run()
    write(reports, "a.md", "a" * 60, age_s=7200)
    write(reports, "b.md", "b" * 10, age_s=3600)

    await watch.run()
    await watch.run()

    assert "a.md" in ask.shown[0] and "b.md" not in ask.shown[0]
    assert "b.md" in ask.shown[1]
