"""Google documents as titled text sections — the unit the answerer is shown.

- A doc is cut the way its source is configured (the owner's choice on the admin page):
  into overlapping pieces, or at its Heading 1/2 paragraphs, one section per heading.
- A sheet: each tab a section of `header: value` rows, split into blocks when long.
- A form: its questions (with the correct answer of a quiz) and every response's score and
  answers, in question order. What the answers mean is the owner's instruction for the
  source, passed to the model as it is — not something this code knows."""

from __future__ import annotations

from dataclasses import dataclass

from app import texts

PIECE = 2000  # characters of a piece of a doc without headings
OVERLAP = 300
MAX_BLOCK = 3000  # characters of one block of sheet rows
MAX_RESPONSES = 200
MAX_PART = 5000  # characters of one heading's section before it is split into parts
SUMMARY_LINE = 160  # characters of a response shown in a quiz's score summary
EXTREMES = 5  # rows shown at each end of a number column in a tab's summary
MIN_NUMBERS = 3  # numbers a column needs to be summarised
MIN_TOTAL_ROWS = 5  # rows a column must total the others in, to be taken for a total
TOTAL_SHARE = 0.9  # of its rows a total column must match


@dataclass(frozen=True)
class Section:
    title: str
    text: str
    # Figures computed over a whole table, ranked next to that table's own rows.
    summary: bool = False
    # The table a section belongs to (a sheet's tab): a summary goes with its own rows, not
    # with every tab of the file.
    group: str = ""

    def as_dict(self) -> dict:
        return {"title": self.title, "text": self.text, "summary": self.summary,
                "group": self.group}


def _paragraph_text(paragraph: dict) -> str:
    return "".join(e.get("textRun", {}).get("content", "")
                   for e in paragraph.get("elements", [])).rstrip("\n")


def _lines(content: list[dict]) -> list[tuple[str, str]]:
    """(style, text) per paragraph, table cells included (one line per row)."""
    out: list[tuple[str, str]] = []
    for element in content:
        if "paragraph" in element:
            p = element["paragraph"]
            out.append((p.get("paragraphStyle", {}).get("namedStyleType", ""),
                        _paragraph_text(p)))
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                cells = [" ".join(t for _, t in _lines(cell.get("content", []))).strip()
                         for cell in row.get("tableCells", [])]
                out.append(("NORMAL_TEXT", " | ".join(c for c in cells)))
    return out


def doc_sections(document: dict, *, by_headings: bool = False) -> list[Section]:
    title = document.get("title", "")
    lines = _lines(document.get("body", {}).get("content", []))
    if not by_headings or not any(style in ("HEADING_1", "HEADING_2") for style, _ in lines):
        return _pieces(title, "\n".join(t for _, t in lines))
    out: list[Section] = []
    current, body = title, []
    for style, text in lines:
        if style in ("HEADING_1", "HEADING_2") and text.strip():
            if any(b.strip() for b in body):
                out += _parts(current, "\n".join(body).strip())
            current, body = text.strip(), []
        elif style == "HEADING_3" and text.strip():
            body.append(f"### {text.strip()}")
        else:
            body.append(text)
    if any(b.strip() for b in body):
        out += _parts(current, "\n".join(body).strip())
    return out


def _parts(title: str, text: str) -> list[Section]:
    """One heading's section, in parts when it is longer than one may be — not cut short."""
    if len(text) <= MAX_PART:
        return [Section(title, text)]
    return _pieces(title, text)


def _pieces(title: str, text: str) -> list[Section]:
    text = text.strip()
    if not text:
        return []
    out = []
    start, n = 0, 1
    while start < len(text):
        end = min(len(text), start + PIECE)
        if end < len(text):
            cut = text.rfind("\n", start + PIECE // 2, end)  # end on a paragraph when possible
            end = cut if cut > 0 else end
        out.append(Section(texts.GOOGLE_PIECE.format(title=title, n=n),
                           text[start:end].strip()))
        if end >= len(text):
            break
        start, n = max(end - OVERLAP, start + 1), n + 1
    return out


def _name(header: list[str], i: int) -> str:
    return header[i] if i < len(header) and header[i] else texts.GOOGLE_COLUMN.format(n=i + 1)


def _row(header: list[str], row: list[str], totals: list[int] = ()) -> str:
    """One row as "name — column: value; …". The first cell names the row when its column has
    no header of its own (a list of books down the left); otherwise every cell is a pair. A row
    with numbers ends with how many it holds, the total columns left out: a model counting
    "how many rated it" down a row of fifteen counted ten, min, max and mean among them."""
    cells = list(enumerate(row))
    named = bool(row and row[0] and not (header and header[0]))
    n = sum(1 for i, v in cells[1 if named else 0:]
            if i not in totals and _number(v) is not None)
    tag = "" if not n else texts.GOOGLE_ROW_NUMBERS_BUT.format(
        n=n, columns=", ".join(_name(header, i) for i in totals)) if totals else \
        texts.GOOGLE_ROW_NUMBERS.format(n=n)
    if named:
        # Next to the row's name: at the end of a long row it was read past.
        rest = "; ".join(f"{_name(header, i)}: {v}" for i, v in cells[1:] if v)
        head = f"{row[0]} {tag}" if tag else row[0]
        return f"{head} — {rest}" if rest else head
    line = "; ".join(f"{_name(header, i)}: {v}" for i, v in cells if v)
    return f"{tag} {line}" if tag else line


def _matches(value: float, text: str, others: list[float]) -> bool:
    """Is `value` the min, max, mean or sum of `others`, to the decimals it is written with?"""
    decimals = len(text.replace(",", ".").partition(".")[2])
    near = 0.5 * 10 ** -decimals + 1e-9
    return any(abs(value - stat) <= near for stat in (
        min(others), max(others), sum(others) / len(others), sum(others)))


def _totals(header: list[str], rows: list[list[str]]) -> list[int]:
    """The columns that only total each row's other numbers (its min, max, mean or sum) — told
    by their values, not their names. Found in rounds: the mean is the mean of the people once
    min and max are set aside, not before."""
    width = max((len(r) for r in rows), default=0)
    named = not (header and header[0])
    found: list[int] = []
    while True:
        new = []
        for i in range(1 if named else 0, width):
            if i in found:
                continue
            checked = hits = 0
            for r in rows:
                value = _number(r[i]) if i < len(r) else None
                others = [n for j, c in enumerate(r) if j != i and j not in found
                          and (j or not named) and (n := _number(c)) is not None]
                if value is None or len(others) < MIN_NUMBERS:
                    continue
                checked += 1
                hits += _matches(value, r[i], others)
            if checked >= MIN_TOTAL_ROWS and hits >= TOTAL_SHARE * checked:
                new.append(i)
        if not new:
            return sorted(found)
        found += new


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ".").replace(" ", "").replace("\xa0", ""))
    except ValueError:
        return None


def _extremes(header: list[str], rows: list[list[str]]) -> str:
    """For every column that is mostly numbers: the rows with its highest and lowest values,
    computed here. A question about the best or the most needs the whole column, and the rows
    reach the model only a block at a time."""
    if not rows:
        return ""
    label = (lambda r: r[0]) if not (header and header[0]) else (lambda r: "; ".join(r[:2]))
    lines = []
    counts = []
    for i in range(1, max(len(r) for r in rows)):
        values = [(n, label(r)) for r in rows if i < len(r) and (n := _number(r[i])) is not None
                  and label(r)]
        name = header[i] if i < len(header) and header[i] else \
            texts.GOOGLE_COLUMN.format(n=i + 1)
        # How many rows a column has a number in, and their mean, for every column with any:
        # "how many did X rate", "X's average" need the whole column, sparse ones too
        # (someone who joined late). Read a block at a time, a model made the mean up.
        if values:
            counts.append(texts.GOOGLE_COUNT.format(
                column=name, n=len(values), mean=f"{sum(n for n, _ in values) / len(values):.2f}"))
        if len(values) < MIN_NUMBERS or len(values) * 2 < len(rows):
            continue
        values.sort(key=lambda v: -v[0])
        shown = lambda vs: ", ".join(f"{name} {n:g}" for n, name in vs)  # noqa: E731
        lines.append(texts.GOOGLE_EXTREMES.format(
            column=name, top=shown(values[:EXTREMES]), bottom=shown(values[-EXTREMES:])))
    if not counts or (not lines and len(rows) < MIN_NUMBERS):
        return ""  # a table that small is read whole anyway
    return "\n".join([texts.GOOGLE_COUNTS.format(rows=len(rows), counts="; ".join(counts)),
                      *lines])


def sheet_sections(title: str, tabs: list[tuple[str, list[list]]]) -> list[Section]:
    out: list[Section] = []
    for tab, rows in tabs:
        rows = [[str(c).strip() for c in r] for r in rows]
        header_at = next((i for i, r in enumerate(rows) if sum(1 for c in r if c) >= 2), None)
        if header_at is None:
            continue
        header = rows[header_at]
        body = [row for row in rows[header_at + 1:] if any(row)]
        totals = _totals(header, body)
        lines = [_row(header, row, totals) for row in body]
        summary = _extremes(header, body)
        if summary:
            out.append(Section(texts.GOOGLE_SUMMARY.format(title=f"{title} / {tab}"), summary,
                               summary=True, group=tab))
        name = f"{title} / {tab}"
        block: list[str] = []
        size, part = 0, 1
        for line in lines:
            if block and size + len(line) > MAX_BLOCK:
                out.append(Section(texts.GOOGLE_ROWS.format(title=name, n=part),
                                   "\n".join(block), group=tab))
                block, size, part = [], 0, part + 1
            block.append(line)
            size += len(line) + 1
        if block:
            out.append(Section(texts.GOOGLE_ROWS.format(title=name, n=part) if part > 1
                               else name, "\n".join(block), group=tab))
    return out


def _question_items(form: dict) -> list[tuple[str, str, str]]:
    """(question id, question title, correct answer) in the form's order."""
    out = []
    for item in form.get("items", []):
        question = item.get("questionItem", {}).get("question")
        if not question:
            continue
        correct = ", ".join(a.get("value", "") for a in
                            question.get("grading", {}).get("correctAnswers", {})
                            .get("answers", []))
        out.append((question.get("questionId", ""), item.get("title", "").strip(), correct))
    return out


def form_sections(form: dict, responses: list[dict]) -> list[Section]:
    """The questions with the first responses, the other responses in blocks, and — for a quiz
    — a summary of the highest and lowest scores, computed here: "who did best" needs every
    response, and they reach the model a block at a time."""
    title = form.get("info", {}).get("title", "") or form.get("info", {}).get("documentTitle", "")
    questions = _question_items(form)
    head = [texts.GOOGLE_QUESTIONS]
    for i, (_, q, correct) in enumerate(questions, start=1):
        head.append(f"{i}. {q}" + (f" ({texts.GOOGLE_CORRECT}: {correct})" if correct else ""))
    head.append(texts.GOOGLE_RESPONSES.format(n=len(responses)))
    lines = [_response(n, r, questions) for n, r in enumerate(responses[:MAX_RESPONSES], 1)]
    out: list[Section] = []
    summary = _scores(responses[:MAX_RESPONSES], questions)
    if summary:
        out.append(Section(texts.GOOGLE_SUMMARY.format(title=title), summary, summary=True))
    block, size, part = list(head), sum(len(x) + 1 for x in head), 1
    for line in lines:
        if size + len(line) > MAX_BLOCK and len(block) > len(head) * (part == 1):
            out.append(Section(title if part == 1 else
                               texts.GOOGLE_ROWS.format(title=title, n=part), "\n".join(block)))
            block, size, part = [], 0, part + 1
        block.append(line)
        size += len(line) + 1
    out.append(Section(title if part == 1 else texts.GOOGLE_ROWS.format(title=title, n=part),
                       "\n".join(block)))
    return out


def _response(n: int, response: dict, questions: list[tuple[str, str, str]]) -> str:
    answers = response.get("answers", {})
    values = []
    for qid, _, _ in questions:
        given = answers.get(qid, {}).get("textAnswers", {}).get("answers", [])
        values.append(", ".join(a.get("value", "") for a in given) or "—")
    score = response.get("totalScore")
    who = response.get("respondentEmail", "")
    head = texts.GOOGLE_RESPONSE.format(n=n) + (f" {who}" if who else "")
    if score is not None:
        head += " · " + texts.GOOGLE_SCORE.format(score=score)
    return f"{head}: " + " | ".join(values)


def _scores(responses: list[dict], questions: list[tuple[str, str, str]]) -> str:
    """The responses with the highest and lowest totals, each with the start of its answers —
    which of them says who answered is the owner's instruction to know, not this code's."""
    scored = [(r["totalScore"], n, r) for n, r in enumerate(responses, 1)
              if isinstance(r.get("totalScore"), (int, float))]
    if len(scored) < 2:
        return ""
    scored.sort(key=lambda s: -s[0])

    def shown(items) -> str:
        return "; ".join(_response(n, r, questions)[:SUMMARY_LINE] for _, n, r in items)

    return texts.GOOGLE_SCORES.format(top=shown(scored[:EXTREMES]),
                                      bottom=shown(scored[-EXTREMES:]))
