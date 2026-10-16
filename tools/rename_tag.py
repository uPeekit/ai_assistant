"""Rename a tag across the vault: `#from` in text and `tag`/`tags` in note properties.

    uv run python -m tools.rename_tag --vault path/to/vault --from club --to club_my --dry-run
    uv run python -m tools.rename_tag --vault path/to/vault --from club --to club_my

Only the tag changes: `#club` becomes `#club_my` (task lines, Tasks queries such as
`tags include #club`, the guide's backticked tag), and a property `tag: club` or a `tags` list
entry `club` becomes `club_my`. Links (`[[club]]`), words and addresses (`t.me/clubhouse`) are
left alone, as is `#club_old` or `#club/sub`. Every changed file is copied to a backup folder first.
Syncthing's archive, the trash and Obsidian's settings are skipped."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

SKIP = {".stversions", ".trash", ".obsidian", ".git"}
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _inline(tag: str) -> re.Pattern:
    # Not inside a heading link or an address: [[Note#club]], https://x/page#club.
    return re.compile(rf"(?<![\w/#\]])#{re.escape(tag)}(?![\w/-])")


def _front(block: str, old: str, new: str) -> str:
    """`tag: old`, `tags: [a, old]`, `tags: old` and `  - old` under `tags:` / `tag:`."""
    out: list[str] = []
    in_list = False
    word = re.compile(rf"(?<![\w#-]){re.escape(old)}(?![\w/-])")
    for line in block.split("\n"):
        key = re.match(r"^(tags?)\s*:(.*)$", line)
        if key:
            in_list = not key.group(2).strip()
            line = f"{key.group(1)}:{word.sub(new, key.group(2))}"
        elif in_list and re.match(r"^\s*-\s", line):
            line = word.sub(new, line)
        else:
            in_list = False
        out.append(line)
    return "\n".join(out)


def rename(text: str, old: str, new: str) -> str:
    m = _FRONT.match(text)
    if m:
        text = f"---\n{_front(m.group(1), old, new)}\n---\n" + text[m.end():]
    return _inline(old).sub(f"#{new}", text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vault", type=Path, required=True)
    p.add_argument("--from", dest="old", required=True)
    p.add_argument("--to", dest="new", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backup", type=Path)
    a = p.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    old, new = a.old.lstrip("#"), a.new.lstrip("#")
    backup = a.backup or a.vault.parent / (
        f"{a.vault.name}-tag-{old}-{datetime.now():%Y%m%d-%H%M%S}")
    changed = 0
    for path in sorted(a.vault.rglob("*.md")):
        rel = path.relative_to(a.vault)
        if any(part in SKIP for part in rel.parts):
            continue
        raw = path.read_bytes().decode("utf-8")
        text = raw.replace("\r\n", "\n")
        result = rename(text, old, new)
        if result == text:
            continue
        changed += 1
        print(f"{rel}: {sum(1 for x, y in zip(text.split(chr(10)), result.split(chr(10)), strict=False) if x != y)} line(s)")  # noqa: E501
        if a.dry_run:
            continue
        target = backup / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if "\r\n" in raw:
            result = result.replace("\n", "\r\n")
        path.write_bytes(result.encode("utf-8"))
    print(f"{changed} file(s) {'would change' if a.dry_run else 'changed'}"
          + ("" if a.dry_run or not changed else f"; backup in {backup}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
