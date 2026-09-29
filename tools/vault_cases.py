"""Build a filer case set from the messages the bot has actually handled.

    uv run python -m tools.vault_cases --db C:\\apps\\ai_assistant\\data\\bot.sqlite

Real messages beat invented ones: the phrasing, the Russian and the ambiguity are the user's
own. The database is opened read-only and never written to.

Each case freezes the `VaultContext` the filer would have been given — folders, tags, the
candidate notes for *that* message, open tasks, groceries, and the day the message was sent.
Freezing it is what makes a run reproducible: the vault keeps changing, the case file does not.

The expected answer is left empty here. `tools/label_cases.py` drafts it with Claude and the
user corrects it; a case with no `expect` block is reported as unlabelled, never as a pass.

The output holds real note and message text: it goes under data/ (git-ignored) by default.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from app.vault.filer import context
from app.vault.index import VaultIndex

TZ = ZoneInfo("Europe/Tallinn")
KINDS = ("text", "voice")


def messages(db: Path, limit: int = 0) -> list[tuple[int, str, str]]:
    """(event id, the user's words, ISO timestamp) for every message the bot interpreted.

    A voice note's transcription is what the filer saw, so it wins over raw_input. Callbacks
    are button presses, not messages, and are skipped: they carry no text to interpret.
    """
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, ts, kind, COALESCE(transcription, ''), COALESCE(raw_input, '') "
            "FROM events WHERE kind IN (?, ?) ORDER BY id", KINDS).fetchall()
    finally:
        con.close()
    out: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for eid, ts, _kind, transcription, raw in rows:
        text = (transcription or raw).strip()
        if not text or text.startswith("/"):  # commands are not filer input
            continue
        key = text.casefold()
        if key in seen:  # the same errand typed twice teaches the benchmark nothing
            continue
        seen.add(key)
        out.append((eid, text, ts))
    return out[:limit] if limit else out


def case(eid: int, text: str, ts: str, index: VaultIndex) -> dict:
    when = datetime.fromisoformat(ts).astimezone(TZ)
    ctx = context(index, text, when)
    return {
        "id": f"e{eid}",
        "text": text,
        "sent": when.isoformat(timespec="seconds"),
        "context": asdict(ctx),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path("C:/apps/ai_assistant/data/bot.sqlite"))
    p.add_argument("--vault", type=Path, default=Path("C:/data/obsidian"))
    p.add_argument("--out", type=Path, default=Path("data/eval/vault_cases.yaml"))
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args(argv)

    if not a.db.exists():
        print(f"no database at {a.db}", file=sys.stderr)
        return 2
    if not a.vault.is_dir():
        print(f"no vault at {a.vault}", file=sys.stderr)
        return 2

    index = VaultIndex(a.vault)
    index.refresh()
    rows = messages(a.db, a.limit)
    cases = [case(eid, text, ts, index) for eid, text, ts in rows]

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(
        yaml.safe_dump({"cases": cases}, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8")
    print(f"{len(cases)} case(s) from {len(rows)} message(s) -> {a.out}")
    print(f"vault: {len(index.notes)} notes, {len(index.folders())} folders, "
          f"{len(index.tags())} tags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
