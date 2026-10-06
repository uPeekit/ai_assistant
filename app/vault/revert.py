"""Taking one write back out of a file that has changed since.

Undo used to put a file's whole previous text back. Two messages a minute apart each add a
line to the task file; undoing the first restored the text from before *both*, and the second
task was gone. A write is taken back as a change instead: the lines it added are removed and
the lines it removed are put back, wherever they now are. When those lines are no longer there
as the write left them, nothing is changed and the caller says so."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# [[note|phrase]] and [[note]]: what the linker wraps a phrase in after the write.
_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")


def _key(line: str) -> str:
    """A line as it compares. The linker links a phrase a moment after the write — and when
    the phrase is the note's own name, in the note's spelling — which is still the same line."""
    return _LINK.sub(r"\1", line).casefold()


def take_back(previous: str, written: str, current: str) -> str | None:
    """`current` without the change that turned `previous` into `written`.

    None when a line that change added or replaced is not in `current` as it was written, or
    the place a removed line came from cannot be found: guessing there would damage text that
    somebody wrote after the bot did."""
    if current == written:
        return previous
    before, after, now = previous.split("\n"), written.split("\n"), current.split("\n")
    # Where each line of the written text is in the file as it is now.
    at: dict[int, int] = {}
    matcher = SequenceMatcher(None, [_key(x) for x in after], [_key(x) for x in now],
                              autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            at[a + k] = b + k
    changes: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, after, before,
                                               autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i1 == i2:
            # The write removed lines here and added none: they go back between the lines
            # that stood around them, one of which has to be found.
            if i1 in at:
                start = at[i1]
            elif i1 - 1 in at:
                start = at[i1 - 1] + 1
            else:
                return None
            end = start
        else:
            if any(k not in at for k in range(i1, i2)) or at[i2 - 1] - at[i1] != i2 - 1 - i1:
                return None  # not all there, or no longer next to each other
            start, end = at[i1], at[i2 - 1] + 1
        changes.append((start, end, before[j1:j2]))
    for start, end, lines in sorted(changes, key=lambda c: c[0], reverse=True):
        now[start:end] = lines
    return "\n".join(now)
