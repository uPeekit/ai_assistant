"""A fetched page cut down to what a lookup needs, before any model reads it.

A shop's search page is 300k characters of menus, cookie text and footers around a few dozen
lines that matter, and sending it whole is how one research call read 645k tokens. This keeps the
lines a lookup is about — prices, sizes, headings, the words asked for — with a line of context
each side, and the links on those lines, so a results page still says where to read next. Under
the limit, the start of the page fills the rest: a listing says most of what matters at the top.
Pure: no model, no network.
"""

from __future__ import annotations

import re

from app.vault.index import related, words

LIMIT = 6000
GAP = "…"
PRICE = re.compile(r"\d[\d  ]*(?:[.,]\d{1,2})?\s?(?:€|eur\b)|€\s?\d", re.I)
UNITS = re.compile(r"\d+(?:[.,]\d+)?\s?(?:m²|m2|tuba|toa|korrus|rooms?)\b|ehitusaasta|built in",
                   re.I)
HEADING = re.compile(r"^#{1,6}\s")
IMAGE = re.compile(r"^\s*!\[")
CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]")  # a search form's options, not page content
ONLY_LINK = re.compile(r"^\s*(?:[-*]\s*)?\[[^\]]*\]\([^)]*\)\s*$")
BOILERPLATE = re.compile(r"cookie|küpsis|consent|privacy policy|nõustun|accept all", re.I)
NUMBER = re.compile(r"\d{2,}")
# An image as its alt text, without Jina's "Image 16:" numbering: a shop named only by its logo
# keeps its name, and the long image address goes.
IMAGE_ALT = re.compile(r"!\[(?:Image \d+:\s*)?([^\]]*)\]\([^)]*\)")


def relevant(text: str, terms: str, limit: int = LIMIT) -> str:
    """The lines of `text` that matter for `terms`, in page order, at most `limit` characters."""
    lines = [IMAGE_ALT.sub(r"\1", line).rstrip() for line in text.splitlines()]
    want = words(terms)
    numbers = set(NUMBER.findall(terms))
    priced = {i for i, line in enumerate(lines) if PRICE.search(line)}
    useful = [i for i, line in enumerate(lines) if line.strip()
              and not _noise(line, want, near_price=bool({i - 1, i + 1} & priced))]
    usable = set(useful)
    picked: set[int] = set()
    for i in useful:
        if _matters(lines[i], want, numbers):
            picked.update(j for j in (i - 1, i, i + 1) if j in usable)
    chosen = _fit(sorted(picked), lines, limit)
    room = limit - sum(_cost(lines[i]) for i in chosen)
    for i in useful:  # top up from the start of the page
        if i in chosen or _cost(lines[i]) > room:
            continue
        chosen.add(i)
        room -= _cost(lines[i])
    out = _render(sorted(chosen), lines)
    return out if len(out) <= limit else out[:limit].rsplit("\n", 1)[0]


def _cost(line: str) -> int:
    """A line's share of the limit: itself, its newline, and room for a gap marker before it —
    without that room, the markers pushed the page over the limit and the final cut dropped the
    lines that mattered most."""
    return len(line) + 1 + len(GAP) + 1


def _matters(line: str, want: set[str], numbers: set[str]) -> bool:
    return bool(HEADING.match(line) or PRICE.search(line) or UNITS.search(line)
                or _mentions(line, want) or numbers & set(NUMBER.findall(line)))


def _mentions(line: str, want: set[str]) -> bool:
    return bool(want) and any(related(w, x) for x in words(line) for w in want)


def _noise(line: str, want: set[str], near_price: bool = False) -> bool:
    if IMAGE.match(line) or CHECKBOX.match(line):
        return True
    # "küpsis" is a cookie banner's word and also Estonian for biscuit: a product line sits next
    # to its price, a banner does not.
    if BOILERPLATE.search(line) and not (near_price or PRICE.search(line)):
        return True
    # A bare link is a menu entry, unless it names what is looked for, carries a price, or sits
    # right next to one — a results page puts the shop or product link beside its price.
    return (bool(ONLY_LINK.match(line))
            and not (near_price or PRICE.search(line) or _mentions(line, want)))


def _fit(indexes: list[int], lines: list[str], limit: int) -> set[int]:
    chosen: set[int] = set()
    used = 0
    for i in indexes:
        if used + _cost(lines[i]) > limit:
            break
        chosen.add(i)
        used += _cost(lines[i])
    return chosen


def _render(indexes: list[int], lines: list[str]) -> str:
    out: list[str] = []
    previous = None
    for i in indexes:
        if previous is not None and i != previous + 1:
            out.append(GAP)
        out.append(lines[i])
        previous = i
    return "\n".join(out)
