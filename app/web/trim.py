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
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
TABLE_ROW = re.compile(r"^\s*\|(.*)\|\s*$")
FACT_LINE = 300  # longer than this, a line with a price is a disclaimer or prose, not a fact
PROSE = 40


def page_facts(text: str, terms: str, limit: int = LIMIT) -> str:
    """The page a link points to, at most `limit` characters, in page order. Unlike a search
    hit, the page is the subject, and the message about it may share no word with it (a
    question in Russian about an Estonian listing). So its facts come first wherever they sit:
    table rows, prices and sizes, headings, the words asked about. Its prose comes next, then
    the rest. Menus, link lists and other listings go, and a link keeps only its text."""
    lines = [IMAGE_ALT.sub(r"\1", line).rstrip() for line in text.splitlines()]
    want = words(terms)
    tiers: tuple[list[int], ...] = ([], [], [])
    for i, line in enumerate(lines):
        tier = _tier(line, want)
        if tier is not None:
            tiers[tier].append(i)
        lines[i] = LINK.sub(r"\1", line)
    chosen: set[int] = set()
    room = limit
    for tier in tiers:
        for i in tier:
            if _cost(lines[i]) <= room:
                chosen.add(i)
                room -= _cost(lines[i])
    out = _render(sorted(chosen), lines)
    return out if len(out) <= limit else out[:limit].rsplit("\n", 1)[0]


def _tier(line: str, want: set[str]) -> int | None:
    """0 a fact, 1 prose, 2 anything else worth keeping, None noise."""
    if not line.strip() or IMAGE.match(line) or CHECKBOX.match(line) or BOILERPLATE.search(line):
        return None
    row = TABLE_ROW.match(line)
    if row:  # a key and its value; a one-cell row is a caption or a link, `| --- |` a rule
        cells = [cell.strip() for cell in row.group(1).split("|")]
        return 0 if sum(1 for c in cells if c.strip("-: ")) >= 2 else None
    visible = LINK.sub(r"\1", line).strip()
    if LINK.search(line) and len(LINK.sub("", line).strip()) * 2 < len(visible):
        return None  # mostly links: a menu, or another listing with its price
    if HEADING.match(line) or (len(visible) <= FACT_LINE and (
            PRICE.search(visible) or UNITS.search(visible) or _mentions(visible, want))):
        return 0
    return 1 if len(visible) >= PROSE else 2


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
