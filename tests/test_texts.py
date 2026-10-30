"""Every user-visible Russian string lives in app.texts. This test checks completeness (every
error code has a message) and that every placeholder-bearing template can actually be filled in
without leaving stray braces."""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

from app import texts

# What each error message may ask its caller for. A placeholder outside these sets makes that
# code degrade — silently — to ERRORS["INTERNAL"] for every user who hits it.
ERROR_PLACEHOLDERS: dict[str, set[str]] = {
    "NOTHING_ENABLED": set(), "VAULT_NOT_SET": set(), "STT_EMPTY": set(), "STT_FAILED": set(),
    "UNDO_EXPIRED": {"minutes"}, "UNDO_FAILED": {"message"}, "BUTTON_EXPIRED": set(),
    "INTERNAL": set(), "LINK_ANSWER_FAILED": set(),
}

# Files allowed to carry literal Cyrillic text outside app/texts.py: the prompt modules, which
# speak Russian to the model, not to the user.
ALLOWED_CYRILLIC_FILES = {
    Path("app/texts.py"), Path("app/llm/prompts.py"), Path("app/llm/staged_prompts.py"),
}
CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def placeholders(template: str) -> set[str]:
    return {name.split(".")[0].split("[")[0]
            for _, name, _, _ in Formatter().parse(template) if name}


def test_no_stray_cyrillic_outside_texts_and_llm_modules():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "app").rglob("*.py"):
        rel = path.relative_to(root)
        if rel in ALLOWED_CYRILLIC_FILES:
            continue
        if CYRILLIC.search(path.read_text(encoding="utf-8")):
            offenders.append(str(rel))
    assert offenders == []


def test_error_templates_ask_only_for_what_their_callers_can_supply():
    assert set(texts.ERRORS) == set(ERROR_PLACEHOLDERS)
    for code, template in texts.ERRORS.items():
        assert placeholders(template) == ERROR_PLACEHOLDERS[code], code


def test_every_error_has_a_message_and_fills_in_cleanly():
    for code, template in texts.ERRORS.items():
        assert template.strip(), code
        assert "{" not in template.format(minutes=5, message="OSError"), code


def test_the_command_list_and_the_help_agree():
    """A command the menu offers is one /help explains, and the other way round."""
    named = set(re.findall(r"^/(\w+) —", texts.HELP_TEXT, re.M))
    assert named == set(texts.COMMANDS)


def test_nothing_the_user_reads_mentions_notion():
    for name, value in vars(texts).items():
        if name.startswith("_"):
            continue
        assert "notion" not in repr(value).casefold(), name
