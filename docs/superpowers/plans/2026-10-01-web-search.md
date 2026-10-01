# Web Search: Bounded Research Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the server-side web research loop with a loop the bot drives — `web_search`, `site_search`, `read` — whose budget is enforced in code, so a lookup always ends in an answer within ~90 s and a fraction of a dollar.

**Architecture:** Three small units under `app/web/` (site table, page trimming, page reader via Jina Reader with a plain-fetch fallback) feed a new loop in `app/llm/research.py`. The new loop lives beside the old one (`engine="loop"` vs `"server"`) until a live benchmark decides the switch; then the old path is deleted. Callers (`SharedResearch`, orchestrator, vault) see the same `WebResearcher.research(request, query, media)`.

**Tech Stack:** Python 3.12, `uv`, `anthropic` 1.x (tests mock it with `httpx2`), `httpx` for our own HTTP, `pytest` + `pytest-asyncio` (auto mode), `ruff`.

**Spec:** `docs/superpowers/specs/2026-10-01-web-search-design.md`

## Global Constraints

- Run `uv run pytest -q` and `uv run ruff check .`; both clean before every commit.
- No Cyrillic in `app/` outside `app/texts.py`, `app/llm/prompts.py`, `app/llm/context.py`, `app/llm/staged_prompts.py` (`tests/test_reply.py`). All Russian model-facing text goes in `app/llm/prompts.py`.
- No message text and no URLs in INFO logs; URLs only at DEBUG.
- Our own HTTP uses `httpx`; Anthropic SDK mocks in tests use `httpx2`, as `tests/test_research.py` does.
- Never write file contents through a shell heredoc containing backslashes — this shell eats one level. Use the editor tools.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (use `git commit -m "<subject>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`).
- Budget values: 4 web searches, 8 site searches + reads together, 90 s soft deadline, 180 s hard stop, 6000 chars per trimmed page, Jina timeout 20 s, plain-fetch timeout 15 s, at most 5 redirects.
- Jina: `https://r.jina.ai/<url>`, header `X-Retain-Images: none`, optional `Authorization: Bearer <JINA_API_KEY>`.
- Only public http(s) addresses are read, every redirect hop checked with `app.notion.images.is_public_url`.
- Web search tool: `web_search_20250305` only — no sandbox versions.

## Review Focus

1. **A public page redirecting to a private address** (`http://127.0.0.1:8787`) in the plain fetch must be refused, not followed — Task 3 test `test_a_redirect_to_a_private_address_is_refused`.
2. **Jina answers 200 but the target refused it** ("Warning: Target URL returned error 403", or a challenge page as content) must count as a failure and fall back — Task 3 test `test_a_jina_failure_falls_back_to_a_direct_fetch`.
3. **A huge page** in the plain fetch must be cut at the byte cap, not downloaded whole — Task 3 test `test_a_huge_page_is_cut_at_the_byte_cap`.
4. **A malformed tool call** (unknown site, rent on a site without rent, `read` without an http(s) URL) must come back as an error the model can read, cost no budget, and not stop the loop — Task 4 test `test_bad_tool_input_costs_nothing_and_says_why`.
5. **An answer cut off at `max_tokens`** keeps the text it has; an empty one is a `ResearchError` — Task 4 test `test_a_cut_off_answer_is_kept_and_an_empty_one_is_an_error`.

---

### Task 1: The site table

**Files:**
- Create: `app/web/__init__.py` (empty)
- Create: `app/web/sites.py`
- Test: `tests/test_web_sites.py`

**Interfaces:**
- Produces: `Site(name, kind, template, rent_template="", probe="")`; `SITES: tuple[Site, ...]`; `BY_NAME: dict[str, Site]`; `SALE = "sale"`, `RENT = "rent"`; `class SiteError(ValueError)`; `search_url(name: str, query: str, deal: str = SALE) -> str`.

- [ ] **Step 1: Write the failing test** — `tests/test_web_sites.py`:

```python
import pytest

from app.web.sites import BY_NAME, RENT, SITES, SiteError, search_url


def test_every_site_has_a_template_a_probe_and_a_unique_name():
    assert len(BY_NAME) == len(SITES)
    for s in SITES:
        assert s.template.startswith("https://") and "{q}" in s.template
        assert s.probe
        if s.rent_template:
            assert "{q}" in s.rent_template


def test_the_query_is_quoted_with_estonian_letters_and_spaces():
    assert search_url("kv.ee", "Kalevipoja  põik 3") == (
        "https://www.kv.ee/search?deal_type=1&keyword=Kalevipoja%20p%C3%B5ik%203")


def test_rent_uses_the_rent_search():
    assert search_url("kv.ee", "Lasnamäe", RENT) == (
        "https://www.kv.ee/search?deal_type=2&keyword=Lasnam%C3%A4e")


def test_errors_say_what_is_wrong_in_words_the_model_can_act_on():
    with pytest.raises(SiteError, match="unknown site"):
        search_url("bauhof.ee", "liimpuit")
    with pytest.raises(SiteError, match="no rent search"):
        search_url("city24.ee", "Lasnamäe", RENT)
    with pytest.raises(SiteError, match="empty"):
        search_url("rimi.ee", "   ")
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_web_sites.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web'`.

- [ ] **Step 3: Implement** — create an empty `app/web/__init__.py`, then `app/web/sites.py`:

```python
"""The shops and real-estate sites the research loop can search directly.

Search engines are slow to index a new listing or a shop's stock; the site's own search page has
them today. Every template here was checked on 2026-10-01 to come back through Jina Reader with
the query and prices on the page (tools/check_sites.py runs the same check), so the model is only
offered searches that work. A site not listed is still reachable through web_search and read.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

SALE, RENT = "sale", "rent"


@dataclass(frozen=True)
class Site:
    name: str
    kind: str  # what it is for; app.llm.prompts says it to the model in words
    template: str  # "{q}" becomes the quoted query
    rent_template: str = ""
    probe: str = ""  # a query tools/check_sites.py expects results and prices for


SITES: tuple[Site, ...] = (
    Site("kv.ee", "real_estate", "https://www.kv.ee/search?deal_type=1&keyword={q}",
         rent_template="https://www.kv.ee/search?deal_type=2&keyword={q}", probe="Lasnamäe"),
    Site("city24.ee", "real_estate",
         "https://www.city24.ee/real-estate-search/apartments-for-sale?search={q}",
         probe="Lasnamäe"),
    Site("rimi.ee", "groceries", "https://www.rimi.ee/epood/ee/otsing?query={q}", probe="piim"),
    Site("selver.ee", "groceries", "https://www.selver.ee/search?q={q}", probe="piim"),
    Site("k-rauta.ee", "building", "https://www.k-rauta.ee/otsing?q={q}", probe="liimpuit"),
    Site("ehituseabc.ee", "building", "https://www.ehituseabc.ee/search?q={q}",
         probe="liimpuit"),
    Site("kaup24.ee", "goods", "https://kaup24.ee/et/search?q={q}", probe="iphone"),
    Site("euronics.ee", "electronics", "https://www.euronics.ee/search?q={q}", probe="iphone"),
    Site("hinnavaatlus.ee", "prices",
         "https://www.hinnavaatlus.ee/search/?Type=products&Query={q}", probe="iphone"),
)
BY_NAME = {s.name: s for s in SITES}


class SiteError(ValueError):
    """A search the table cannot make. The message goes to the model as the tool's answer."""


def search_url(name: str, query: str, deal: str = SALE) -> str:
    site = BY_NAME.get(name)
    if site is None:
        raise SiteError(f"unknown site {name!r}; use one of: {', '.join(BY_NAME)}")
    query = " ".join(query.split())
    if not query:
        raise SiteError("empty query")
    if deal == RENT:
        if not site.rent_template:
            raise SiteError(f"{name} has no rent search here; use web_search instead")
        template = site.rent_template
    else:
        template = site.template
    return template.format(q=quote(query, safe=""))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_web_sites.py -q && uv run ruff check app/web tests/test_web_sites.py`
Expected: 4 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add app/web/__init__.py app/web/sites.py tests/test_web_sites.py
git commit -m "feat: a verified table of shop and real-estate site searches" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Trimming a page

**Files:**
- Create: `app/web/trim.py`
- Test: `tests/test_web_trim.py`

**Interfaces:**
- Consumes: `app.vault.index.words(text) -> set[str]`, `app.vault.index.related(a, b) -> bool` (existing).
- Produces: `relevant(text: str, terms: str, limit: int = LIMIT) -> str`; `LIMIT = 6000`; `GAP = "…"`.

- [ ] **Step 1: Write the failing test** — `tests/test_web_trim.py`:

```python
from app.web.trim import GAP, relevant

PAGE = "\n".join([
    "Kasutame küpsiseid, et leht töötaks",          # cookie banner: dropped
    "[Avaleht](https://rimi.ee/)",                  # menu link: dropped
    "[Kontakt](https://rimi.ee/kontakt)",
    "![logo](https://rimi.ee/logo.png)",            # image: dropped
    *[f"Sissejuhatus {n}" for n in range(30)],     # the start of the page
    "## Otsingu tulemused",                         # heading: kept
    "[Piim 2,5% Alma 1 l](https://rimi.ee/p/1)",    # the term, with its link: kept
    "1,39 €",                                       # price: kept
    "Lisa korvi",                                   # context around the price
    *[f"Muu tekst {n}" for n in range(300)],        # filler
    "[Piim 3,5% Farmi 1 l](https://rimi.ee/p/2)",
    "1,49 €",
])


def test_prices_terms_and_headings_are_kept_with_their_links():
    out = relevant(PAGE, "piim", limit=600)
    assert "[Piim 2,5% Alma 1 l](https://rimi.ee/p/1)" in out
    assert "1,39 €" in out and "1,49 €" in out
    assert "## Otsingu tulemused" in out
    assert "Lisa korvi" in out  # a line of context


def test_cookie_menu_and_image_lines_are_dropped():
    out = relevant(PAGE, "piim", limit=6000)
    assert "küpsis" not in out
    assert "Avaleht" not in out and "Kontakt" not in out
    assert "logo.png" not in out


def test_the_limit_is_held_and_skipped_lines_are_marked():
    out = relevant(PAGE, "piim", limit=300)
    assert len(out) <= 300
    assert GAP in out


def test_words_match_in_other_forms():
    page = "\n".join(["вступление", *["шум"] * 200, "Купить молока в магазине", *["шум"] * 200])
    assert "Купить молока" in relevant(page, "молоко", limit=200)


def test_a_number_in_the_request_finds_its_line():
    page = "\n".join(["algus", *["muu"] * 300, "Korter nr 120, Kalevipoja põik 3", *["muu"] * 300])
    assert "Korter nr 120" in relevant(page, "Kalevipoja põik 3-120", limit=200)


def test_a_page_with_nothing_matching_is_filled_from_its_start():
    assert relevant("first line\nsecond\nthird", "zzzz", limit=100) == "first line\nsecond\nthird"
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_web_trim.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web.trim'`.

- [ ] **Step 3: Implement** — `app/web/trim.py`:

```python
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
ONLY_LINK = re.compile(r"^\s*(?:[-*]\s*)?\[[^\]]*\]\([^)]*\)\s*$")
BOILERPLATE = re.compile(r"cookie|küpsis|consent|privacy policy|nõustun|accept all", re.I)
NUMBER = re.compile(r"\d{2,}")


def relevant(text: str, terms: str, limit: int = LIMIT) -> str:
    """The lines of `text` that matter for `terms`, in page order, at most `limit` characters."""
    lines = [line.rstrip() for line in text.splitlines()]
    want = words(terms)
    numbers = set(NUMBER.findall(terms))
    useful = [i for i, line in enumerate(lines) if line.strip() and not _noise(line, want)]
    usable = set(useful)
    picked: set[int] = set()
    for i in useful:
        if _matters(lines[i], want, numbers):
            picked.update(j for j in (i - 1, i, i + 1) if j in usable)
    chosen = _fit(sorted(picked), lines, limit)
    room = limit - sum(len(lines[i]) + 1 for i in chosen)
    for i in useful:  # top up from the start of the page
        cost = len(lines[i]) + 1
        if i in chosen or cost > room:
            continue
        chosen.add(i)
        room -= cost
    out = _render(sorted(chosen), lines)
    return out if len(out) <= limit else out[:limit].rsplit("\n", 1)[0]


def _matters(line: str, want: set[str], numbers: set[str]) -> bool:
    return bool(HEADING.match(line) or PRICE.search(line) or UNITS.search(line)
                or _mentions(line, want) or numbers & set(NUMBER.findall(line)))


def _mentions(line: str, want: set[str]) -> bool:
    return bool(want) and any(related(w, x) for x in words(line) for w in want)


def _noise(line: str, want: set[str]) -> bool:
    if IMAGE.match(line) or BOILERPLATE.search(line):
        return True
    # A bare link is a menu entry unless it names what is looked for or carries a price.
    return bool(ONLY_LINK.match(line)) and not (PRICE.search(line) or _mentions(line, want))


def _fit(indexes: list[int], lines: list[str], limit: int) -> set[int]:
    chosen: set[int] = set()
    used = 0
    for i in indexes:
        cost = len(lines[i]) + 1
        if used + cost > limit:
            break
        chosen.add(i)
        used += cost
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_web_trim.py tests/test_reply.py -q && uv run ruff check app/web tests/test_web_trim.py`
Expected: all pass (the Cyrillic test included — `trim.py` has none); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add app/web/trim.py tests/test_web_trim.py
git commit -m "feat: trim a fetched page to the lines a lookup needs" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Reading a page

**Files:**
- Create: `app/web/reader.py`
- Test: `tests/test_web_reader.py`

**Interfaces:**
- Consumes: `app.notion.images.is_public_url(url) -> Awaitable[bool]` (existing).
- Produces: `Page(url: str, title: str, text: str, via: str)` (`via` is `"jina"` or `"direct"`); `class PageUnreadable(Exception)` (message is for the model); `Reader(*, jina_key="", transport=None, is_public=is_public_url)` with `async read(url) -> Page` and `async aclose()`; `html_to_text(raw, base="") -> tuple[str, str]`; module constants `MAX_BYTES`, `JINA`.

- [ ] **Step 1: Write the failing test** — `tests/test_web_reader.py`:

```python
import httpx
import pytest

from app.web import reader as reader_mod
from app.web.reader import PageUnreadable, Reader

JINA_OK = ("Title: Korter\nURL Source: https://kv.ee/1\n\n"
           "Markdown Content:\n## Korter\n149 990 €")
SHOP = ("<html><head><title>Pood</title><script>x = 1</script></head><body>"
        "<div><a href='/p/1'>Piim</a></div><div>1,39 €</div></body></html>")


async def public(url: str) -> bool:
    return not any(h in url for h in ("127.0.0.1", "localhost", "192.168."))


def transport(jina=None, site=None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            return jina(request) if jina else httpx.Response(500)
        return site(request) if site else httpx.Response(500)
    return httpx.MockTransport(handler)


def reader(**kw) -> Reader:
    return Reader(transport=transport(**kw), is_public=public)


async def test_jina_reads_the_page_and_carries_the_key():
    seen = []

    def jina(req):
        seen.append(req)
        return httpx.Response(200, text=JINA_OK)

    r = Reader(jina_key="k", transport=transport(jina=jina), is_public=public)
    page = await r.read("https://kv.ee/1")
    assert (page.title, page.via) == ("Korter", "jina")
    assert "149 990 €" in page.text
    assert str(seen[0].url) == "https://r.jina.ai/https://kv.ee/1"
    assert seen[0].headers["authorization"] == "Bearer k"
    assert seen[0].headers["x-retain-images"] == "none"


async def test_a_jina_failure_falls_back_to_a_direct_fetch():
    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    refused = ("Title: kv.ee\nWarning: Target URL returned error 403: Forbidden\n\n"
               "Markdown Content:\nJust a moment...")
    for jina in (lambda r: httpx.Response(429), slow,
                 lambda r: httpx.Response(200, text=refused)):
        page = await reader(jina=jina, site=lambda r: httpx.Response(200, html=SHOP)).read(
            "https://shop.ee/")
        assert (page.via, page.title) == ("direct", "Pood")
        assert "[Piim](https://shop.ee/p/1)" in page.text
        assert "x = 1" not in page.text  # scripts dropped


async def test_a_challenge_on_both_paths_is_unreadable():
    challenge = "<html><head><title>Just a moment...</title></head><body>wait</body></html>"
    r = reader(jina=lambda r: httpx.Response(
        200, text="Title: Just a moment...\n\nMarkdown Content:\nchecking"),
        site=lambda r: httpx.Response(200, html=challenge))
    with pytest.raises(PageUnreadable, match="protected"):
        await r.read("https://kv.ee/1")


async def test_an_error_status_on_the_direct_fetch_is_unreadable():
    r = reader(jina=lambda r: httpx.Response(503), site=lambda r: httpx.Response(403))
    with pytest.raises(PageUnreadable, match="403"):
        await r.read("https://kv.ee/1")


async def test_a_private_address_is_refused_without_any_request():
    def boom(req):
        raise AssertionError("no request may be made")

    with pytest.raises(PageUnreadable, match="not a public"):
        await reader(jina=boom, site=boom).read("http://127.0.0.1:8787/api/targets")


async def test_a_redirect_to_a_private_address_is_refused():
    def site(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8787/api/targets"})

    with pytest.raises(PageUnreadable, match="not public"):
        await reader(jina=lambda r: httpx.Response(503), site=site).read("https://evil.ee/go")


async def test_a_huge_page_is_cut_at_the_byte_cap(monkeypatch):
    monkeypatch.setattr(reader_mod, "MAX_BYTES", 10_000)
    big = "<html><body>" + "<p>rida 1,00 €</p>" * 20_000 + "</body></html>"
    page = await reader(jina=lambda r: httpx.Response(503),
                        site=lambda r: httpx.Response(200, html=big)).read("https://big.ee/")
    assert len(page.text) < 10_000
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_web_reader.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web.reader'`.

- [ ] **Step 3: Implement** — `app/web/reader.py`:

```python
"""A web page as text, for the research loop.

Jina Reader (r.jina.ai) first: it renders the page in a browser on its side and gets past the
Cloudflare challenge that kv.ee and K-Rauta put in front of anything that is not a browser — a
plain fetch from this machine, and even a headless Edge, get a 403 there. When Jina fails, a
plain fetch from here: most shops answer it. Only public http(s) addresses are read, every
redirect included: the plain fetch runs on the user's own machine, next to the admin page and
the home network.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from app.notion.images import is_public_url

log = logging.getLogger(__name__)

JINA = "https://r.jina.ai/"
JINA_TIMEOUT_S = 20.0
FETCH_TIMEOUT_S = 15.0
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
CHALLENGE = re.compile(r"just a moment|security verification|verify you are human|"
                       r"attention required|checking your browser|"
                       r"target url returned error (?:401|403|429|503)", re.I)


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str
    via: str  # "jina" or "direct"


class PageUnreadable(Exception):
    """The page could not be read. The message goes to the model: "protected", "not public"."""


class Reader:
    def __init__(self, *, jina_key: str = "", transport: httpx.AsyncBaseTransport | None = None,
                 is_public: Callable[[str], Awaitable[bool]] = is_public_url) -> None:
        self._key = jina_key
        self._is_public = is_public
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read(self, url: str) -> Page:
        if not await self._is_public(url):
            raise PageUnreadable("not a public web address")
        try:
            return await self._jina(url)
        except (httpx.HTTPError, PageUnreadable) as e:
            log.info("jina could not read a page (%s); fetching it directly", type(e).__name__)
        try:
            return await self._direct(url)
        except httpx.HTTPError as e:
            raise PageUnreadable(f"could not be fetched ({type(e).__name__})") from None

    async def _jina(self, url: str) -> Page:
        headers = {"X-Retain-Images": "none"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        resp = await self._client.get(JINA + url, headers=headers, timeout=JINA_TIMEOUT_S,
                                      follow_redirects=True)
        resp.raise_for_status()
        head, title, body = _jina_parts(resp.text)
        if not body or any(CHALLENGE.search(part) for part in (head, title, body[:2000])):
            raise PageUnreadable("protected")
        return Page(url, title, body, "jina")

    async def _direct(self, url: str) -> Page:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            async with self._client.stream("GET", current, headers={"User-Agent": BROWSER_UA},
                                           timeout=FETCH_TIMEOUT_S) as resp:
                if resp.is_redirect:
                    target = urljoin(current, resp.headers.get("location", ""))
                    if not await self._is_public(target):
                        raise PageUnreadable("redirected to an address that is not public")
                    current = target
                    continue
                if resp.status_code >= 400:
                    raise PageUnreadable(f"the site answered {resp.status_code}")
                raw = await _capped(resp)
                encoding = resp.encoding or "utf-8"
            title, text = html_to_text(raw.decode(encoding, errors="replace"), current)
            if CHALLENGE.search(title) or CHALLENGE.search(text[:2000]):
                raise PageUnreadable("protected")
            return Page(current, title, text, "direct")
        raise PageUnreadable("too many redirects")


def _jina_parts(raw: str) -> tuple[str, str, str]:
    """Jina answers with a few header lines ("Title: ...", "URL Source: ...", sometimes
    "Warning: Target URL returned error 403"), then "Markdown Content:" and the page."""
    head, sep, body = raw.partition("Markdown Content:")
    if not sep:
        return "", "", raw.strip()
    title = next((line[len("Title:"):].strip() for line in head.splitlines()
                  if line.startswith("Title:")), "")
    return head, title, body.strip()


async def _capped(resp: httpx.Response) -> bytes:
    data = bytearray()
    async for chunk in resp.aiter_bytes():
        data += chunk
        if len(data) >= MAX_BYTES:
            break
    return bytes(data[:MAX_BYTES])


_BLOCKS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section",
           "article", "header", "footer", "ul", "ol", "table", "dd", "dt"}
_SKIP = {"script", "style", "noscript", "svg", "template", "iframe"}


class _Text(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._links: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            self._links.append(urljoin(self.base, href) if href else None)
            self.parts.append("[")
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._links:
            href = self._links.pop()
            self.parts.append(f"]({href})" if href else "]")
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.parts.append(data)


def html_to_text(raw: str, base: str = "") -> tuple[str, str]:
    """(title, text) of an HTML page: scripts and styles dropped, one block per line, links kept
    as Markdown so a results page still says where each result is."""
    parser = _Text(base)
    parser.feed(raw)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    text = "\n".join(line for line in lines if line and line not in ("[]", "[ ]"))
    return " ".join(parser.title.split()), text
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_web_reader.py tests/test_reply.py -q && uv run ruff check app/web tests/test_web_reader.py`
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add app/web/reader.py tests/test_web_reader.py
git commit -m "feat: read a page through Jina Reader, falling back to a guarded direct fetch" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The research loop, beside the old one

**Files:**
- Modify: `app/llm/prompts.py` (add the loop's Russian text, next to `RESEARCH_PROMPT`)
- Modify: `app/llm/research.py` (new loop; `final_text` boundary; constructor parameters)
- Test: `tests/test_research_loop.py`

**Interfaces:**
- Consumes: `Reader`, `Page`, `PageUnreadable` (Task 3); `relevant` (Task 2); `SITES`, `SALE`, `RENT`, `search_url`, `SiteError` (Task 1).
- Produces:
  - `WebResearcher(..., max_searches=4, reader: Reader | None = None, max_reads=8, soft_deadline_s=90.0, clock=time.monotonic, engine="server")` — `engine="loop"` selects the new text half; `"server"` stays the default until Task 8.
  - `WebResearcher.last: Lookup | None` — the most recent loop lookup (read by the benchmark).
  - `Lookup` dataclass (budget and counters); `loop_tools(max_searches) -> list[dict]`; `estimate_cost(model, *, input_tokens, output_tokens, cache_read=0, cache_write=0, searches=0) -> float`.
  - In `app/llm/prompts.py`: `research_loop_prompt(searches: int, reads: int) -> str`, `site_search_tool(sites) -> str`, `READ_TOOL`, `READS_LEFT`, `READ_REFUSED`, `READ_FAILED`, `ALREADY_READ`, `FORCE_ANSWER`, `TOOL_INPUT_ERROR`.

- [ ] **Step 1: Write the failing test** — `tests/test_research_loop.py`:

```python
import asyncio
import json
import logging

import anthropic
import httpx2
import pytest

from app.llm.prompts import FORCE_ANSWER
from app.llm.research import ResearchError, ResearchQuestion, ResearchTimeout, WebResearcher
from app.web.reader import Page, PageUnreadable

KEY = "sk-ant-test-key"


def message(content, stop_reason="end_turn"):
    return httpx2.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 2000, "output_tokens": 300},
    })


def tool_use(id_, name, **inp):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


class FakeReader:
    def __init__(self, fail=(), delay=0.0, on_read=None):
        self.calls: list[str] = []
        self.fail, self.delay, self.on_read = set(fail), delay, on_read

    async def read(self, url):
        self.calls.append(url)
        if self.on_read:
            self.on_read()
        if self.delay:
            await asyncio.sleep(self.delay)
        if url in self.fail:
            raise PageUnreadable("protected")
        return Page(url, "Pealkiri", "## Korter\n149 990 €\n3 tuba, 60 m²", "jina")

    async def aclose(self):
        pass


def scripted(*responses):
    """A handler answering each request with the next response; the bodies it saw."""
    bodies, queue = [], list(responses)

    def handler(req):
        bodies.append(json.loads(req.content))
        return queue.pop(0)
    return handler, bodies


def loop(handler, reader, model="claude-sonnet-5", **kw) -> WebResearcher:
    sdk = anthropic.AsyncAnthropic(
        api_key=KEY, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return WebResearcher(KEY, model, client=sdk, reader=reader, engine="loop", **kw)


async def test_tool_calls_run_and_are_answered_in_one_message():
    handler, bodies = scripted(
        message([text("Сейчас поищу."), tool_use("t1", "read", url="https://a.ee/1"),
                 tool_use("t2", "read", url="https://a.ee/2", look_for="цена")], "tool_use"),
        message([text("## Итог\n- 149 990 €")]))
    reader = FakeReader()
    answer = await loop(handler, reader).research("найди квартиру", "квартира", "text")
    assert answer == "## Итог\n- 149 990 €"  # the narration before the tools is not in it
    assert sorted(reader.calls) == ["https://a.ee/1", "https://a.ee/2"]
    results = bodies[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "149 990 €" in results[0]["content"]


async def test_tools_effort_and_caching_are_sent():
    handler, bodies = scripted(message([text("## Ок")]))
    await loop(handler, FakeReader(), max_searches=4).research("x", "y", "text")
    body = bodies[0]
    by_name = {t["name"]: t for t in body["tools"]}
    assert set(by_name) == {"web_search", "site_search", "read"}
    assert by_name["web_search"]["type"] == "web_search_20250305"
    assert by_name["web_search"]["max_uses"] == 4
    assert "kv.ee" in by_name["site_search"]["input_schema"]["properties"]["site"]["enum"]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "medium"}
    assert "tool_choice" not in body


async def test_haiku_gets_no_effort_setting():
    handler, bodies = scripted(message([text("## Ок")]))
    await loop(handler, FakeReader(), model="claude-haiku-4-5").research("x", "y", "text")
    assert "output_config" not in bodies[0]


async def test_site_search_reads_the_sites_own_search_page():
    handler, _ = scripted(
        message([tool_use("t1", "site_search", site="kv.ee", query="Kalevipoja põik 3")],
                "tool_use"),
        message([text("## Ок")]))
    reader = FakeReader()
    await loop(handler, reader).research("x", "y", "text")
    assert reader.calls == [
        "https://www.kv.ee/search?deal_type=1&keyword=Kalevipoja%20p%C3%B5ik%203"]


async def test_spent_reads_force_an_answer_with_tools_off():
    handler, bodies = scripted(
        message([tool_use(f"t{i}", "read", url=f"https://a.ee/{i}") for i in range(3)],
                "tool_use"),
        message([text("## Итог")]))
    reader = FakeReader()
    assert await loop(handler, reader, max_reads=2).research("x", "y", "text") == "## Итог"
    assert len(reader.calls) == 2  # the third was refused, not read
    results = bodies[1]["messages"][-1]["content"]
    assert results[2]["is_error"] is True
    assert results[-1] == {"type": "text", "text": FORCE_ANSWER}
    assert bodies[1]["tool_choice"] == {"type": "none"}


async def test_the_soft_deadline_forces_an_answer():
    now = [0.0]

    def later():
        now[0] = 100.0

    handler, bodies = scripted(
        message([tool_use("t1", "read", url="https://a.ee/1")], "tool_use"),
        message([text("## Итог")]))
    r = loop(handler, FakeReader(on_read=later), soft_deadline_s=90, clock=lambda: now[0])
    await r.research("x", "y", "text")
    assert bodies[1]["tool_choice"] == {"type": "none"}


async def test_a_url_read_twice_is_not_charged_again():
    handler, bodies = scripted(
        message([tool_use("t1", "read", url="https://a.ee/1")], "tool_use"),
        message([tool_use("t2", "read", url="https://a.ee/1")], "tool_use"),
        message([text("## Итог")]))
    reader = FakeReader()
    await loop(handler, reader, max_reads=2).research("x", "y", "text")
    assert reader.calls == ["https://a.ee/1"]
    assert "tool_choice" not in bodies[2]  # one read left: nothing forced


async def test_bad_tool_input_costs_nothing_and_says_why():
    handler, bodies = scripted(
        message([tool_use("t1", "site_search", site="bauhof.ee", query="liimpuit"),
                 tool_use("t2", "site_search", site="city24.ee", query="Lasnamäe", deal="rent"),
                 tool_use("t3", "read", url="kv.ee/1")], "tool_use"),
        message([text("## Итог")]))
    reader = FakeReader()
    await loop(handler, reader, max_reads=8).research("x", "y", "text")
    results = bodies[1]["messages"][-1]["content"]
    assert all(r["is_error"] for r in results)
    assert "unknown site" in results[0]["content"]
    assert "no rent search" in results[1]["content"]
    assert "http" in results[2]["content"]
    assert "8" in results[2]["content"]  # reads left: none were charged
    assert reader.calls == []


async def test_an_unreadable_page_tells_the_model_to_move_on():
    handler, bodies = scripted(
        message([tool_use("t1", "read", url="https://kv.ee/1")], "tool_use"),
        message([text("## Итог")]))
    await loop(handler, FakeReader(fail={"https://kv.ee/1"})).research("x", "y", "text")
    result = bodies[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "protected" in result["content"]


async def test_the_hard_deadline_still_raises_research_timeout():
    handler, _ = scripted(message([tool_use("t1", "read", url="https://a.ee/1")], "tool_use"))
    with pytest.raises(ResearchTimeout):
        await loop(handler, FakeReader(delay=1.0), deadline_s=0.05).research("x", "y", "text")


async def test_a_question_line_becomes_a_research_question():
    handler, _ = scripted(message([text("ВОПРОС: какой город?")]))
    with pytest.raises(ResearchQuestion, match="какой город"):
        await loop(handler, FakeReader()).research("x", "y", "text")


async def test_a_cut_off_answer_is_kept_and_an_empty_one_is_an_error():
    handler, _ = scripted(message([text("## Итог\n- обрыв")], "max_tokens"))
    assert await loop(handler, FakeReader()).research("x", "y", "text") == "## Итог\n- обрыв"
    handler, _ = scripted(message([], "max_tokens"))
    with pytest.raises(ResearchError, match="no answer"):
        await loop(handler, FakeReader()).research("x", "y", "text")


async def test_one_log_line_per_lookup_and_no_urls_at_info(caplog):
    caplog.set_level(logging.INFO)
    handler, _ = scripted(
        message([tool_use("t1", "site_search", site="kv.ee", query="Lasnamäe")], "tool_use"),
        message([text("## Итог")]))
    await loop(handler, FakeReader()).research("x", "y", "text")
    # The app's own lines only: the HTTP client logs every request URL at INFO by itself.
    info = [r.getMessage() for r in caplog.records
            if r.levelno >= logging.INFO and r.name.startswith("app.")]
    assert len([m for m in info if m.startswith("research:")]) == 1
    assert "kv.ee" in next(m for m in info if m.startswith("research:"))
    assert not any("http" in m for m in info)
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_research_loop.py -q`
Expected: FAIL — `ImportError: cannot import name 'FORCE_ANSWER' from 'app.llm.prompts'`.

- [ ] **Step 3: Add the loop's text to `app/llm/prompts.py`** — directly after the `RESEARCH_PROMPT` definition. Edit with the editor tool; the `«»` quotes and the `{}` placeholders are literal.

```python
def research_loop_prompt(searches: int, reads: int) -> str:
    """RESEARCH_PROMPT for the loop the bot drives: the three tools and the budget, stated."""
    return f"""Ты — исследовательский модуль личного ассистента. Пользователь попросил найти \
что-то в интернете; твой ответ целиком запишется в его Notion или Obsidian. Куда и как \
записать (создать страницу, дописать, в какой раздел) — решает и делает бот, не ты: такие \
слова в сообщении пропускай, твоё дело — только содержание.

Инструменты:
- web_search — поиск в интернете;
- site_search — поиск прямо на сайте магазина или недвижимости из списка; свежие объявления \
и товары с ценами вернее искать так, чем общим поиском;
- read — прочитать страницу по адресу из результатов поиска или со страницы, которую ты уже \
прочитал; страница приходит сокращённой до нужного.

Бюджет на весь поиск: до {searches} вызовов web_search и до {reads} вызовов site_search и read \
вместе. После каждого вызова видно, сколько осталось. Когда бюджет кончится или поиск \
затянется, инструменты отключатся — тогда сразу пиши ответ из того, что нашёл. Никогда не жди \
и не повторяй тот же запрос.

Верни только результат в Markdown — без вступлений, вопросов и рассказа о том, как ты искал; \
первая строка ответа — сразу заголовок ##:
- по делу и компактно: заголовки ##, списки -, таблица, если сравниваешь; ссылки [текст](url);
- цены, площади, адреса — как на сайте, с валютой и единицами;
- в конце раздел «## Источники» со ссылками на страницы, которые ты использовал;
- ничего не выдумывай: только то, что нашёл; не извиняйся и не пиши о том, чего не нашёл;
- пиши по-русски, даже если источники на другом языке, — переводи.
Картинки не ищи: их ищет отдельный модуль.

Только если сама тема поиска противоречива или непонятна настолько, что искать нечего, ответь \
ровно одной строкой: {RESEARCH_QUESTION} <короткий вопрос пользователю> — и больше ничего. Во \
всех остальных случаях ищи и пиши результат; в самом результате вопросов не задавай никогда."""


SITE_KINDS = {
    "real_estate": "недвижимость",
    "groceries": "продукты",
    "building": "стройматериалы и товары для дома",
    "goods": "товары и техника",
    "electronics": "техника",
    "prices": "сравнение цен в разных магазинах",
}


def site_search_tool(sites) -> str:
    listing = "; ".join(
        f"{s.name} — {SITE_KINDS.get(s.kind, s.kind)}"
        + (" (продажа; аренда — deal=rent)" if s.rent_template else "") for s in sites)
    return ("Поиск прямо на сайте из списка: " + listing + ". Запрос пиши на языке сайта — "
            "по-эстонски, как ищут на нём («piim», а не «молоко»; улицы — как пишутся: "
            "«Kalevipoja põik 3»). Возвращает страницу результатов с ценами и ссылками; нужное "
            "объявление или товар открой через read.")


READ_TOOL = ("Прочитать страницу по адресу (http или https). look_for — что ищешь на ней "
             "(«цена, площадь, этаж»): страница придёт сокращённой до этого.")
READS_LEFT = "\n\n(осталось вызовов site_search и read: {n})"
READ_REFUSED = "Бюджет чтений исчерпан — страница не прочитана. Пиши ответ из того, что нашёл."
READ_FAILED = ("Страницу прочитать не удалось ({reason}). Не повторяй — попробуй другую "
               "или пиши ответ.")
ALREADY_READ = "Эта страница уже читается в этом же ходе — её текст в соседнем результате."
FORCE_ANSWER = ("Инструменты больше недоступны: бюджет или время поиска исчерпаны. Напиши "
                "ответ сейчас из того, что уже нашёл.")
TOOL_INPUT_ERROR = "Неверный вызов: {error}"
```

`RESEARCH_QUESTION` is defined further down in `prompts.py`; the f-string reads it when the function is called, so the order is fine.

- [ ] **Step 4: Run the tests again** — still failing, now on the loop itself.

Run: `uv run pytest tests/test_research_loop.py -q`
Expected: FAIL — `TypeError: WebResearcher.__init__() got an unexpected keyword argument 'reader'`.

- [ ] **Step 5: Implement the loop in `app/llm/research.py`.** No Cyrillic in this file.

5a. Imports — add to the existing ones:

```python
import time
from dataclasses import dataclass, field

from app.llm.prompts import (
    ALREADY_READ,
    FORCE_ANSWER,
    READ_FAILED,
    READ_REFUSED,
    READ_TOOL,
    READS_LEFT,
    TOOL_INPUT_ERROR,
    research_loop_prompt,
    site_search_tool,
)
from app.web.reader import PageUnreadable, Reader
from app.web.sites import RENT, SALE, SITES, search_url
from app.web.trim import relevant
```

(merge the new prompt names into the existing `from app.llm.prompts import (...)` block, keeping it sorted.)

5b. Constants — after `_DYNAMIC_PREFIXES`:

```python
MAX_TURNS = 12  # model calls in one lookup, pause_turn resumes included; the budget ends it sooner
MAX_TOKENS = 8192  # thinking and the answer together; the answer is capped at MAX_RESULT_CHARS
_EFFORT_PREFIXES = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-mythos-5",
                    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")
# Dollars per million tokens (input, output), for the log line's estimate; a search is $0.01.
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-5": (2.0, 10.0),
          "claude-sonnet-4-6": (3.0, 15.0), "claude-opus-5": (5.0, 25.0)}
SEARCH_PRICE = 0.01
```

5c. `final_text` — text before a tool call is narration too, so the boundary also counts `tool_use` and `server_tool_use`:

```python
def final_text(blocks: list[Any]) -> str:
    """The answer proper: the text after the last tool call or result. Text before it is the
    model narrating its search ("let me look that up"), which does not belong in the note."""
    last_tool = max((i for i, b in enumerate(blocks)
                     if b.type.endswith("_tool_result")
                     or b.type in ("tool_use", "server_tool_use")), default=-1)
    text = "".join(b.text for b in blocks[last_tool + 1:] if b.type == "text").strip()
    return _drop_preamble(text)
```

5d. New module-level helpers, after `merge`:

```python
def estimate_cost(model: str, *, input_tokens: int, output_tokens: int, cache_read: int = 0,
                  cache_write: int = 0, searches: int = 0) -> float:
    """Dollars, roughly, for the log and the benchmark. Unknown models are priced as Sonnet 5."""
    pin, pout = PRICES.get(model, PRICES["claude-sonnet-5"])
    tokens = (input_tokens * pin + cache_write * pin * 1.25 + cache_read * pin * 0.1
              + output_tokens * pout)
    return tokens / 1_000_000 + searches * SEARCH_PRICE


def loop_tools(max_searches: int) -> list[dict]:
    """web_search is Anthropic's, the basic version: it runs on their side and has no code
    sandbox — the newer version's sandbox is where a model once slept and retried for minutes.
    site_search and read run here, so this code decides how many there are."""
    return [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches},
        {"name": "site_search", "description": site_search_tool(SITES),
         "input_schema": {"type": "object", "additionalProperties": False,
                          "required": ["site", "query"],
                          "properties": {"site": {"enum": [s.name for s in SITES]},
                                         "query": {"type": "string"},
                                         "deal": {"enum": [SALE, RENT]}}}},
        {"name": "read", "description": READ_TOOL,
         "input_schema": {"type": "object", "additionalProperties": False,
                          "required": ["url"],
                          "properties": {"url": {"type": "string"},
                                         "look_for": {"type": "string"}}}},
    ]


@dataclass
class Lookup:
    """One lookup's budget, the pages it read, and what it cost."""

    reads_left: int
    pages: dict[str, str] = field(default_factory=dict)
    sites: list[str] = field(default_factory=list)
    site_reads: int = 0
    page_reads: int = 0
    fallbacks: int = 0
    unreadable: int = 0
    chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    searches: int = 0

    @property
    def spent(self) -> bool:
        return self.reads_left <= 0

    def count(self, usage: Any) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        tools = getattr(usage, "server_tool_use", None)
        self.searches += getattr(tools, "web_search_requests", 0) or 0
```

5e. `WebResearcher.__init__` — new parameters (keep every existing one) and their storage:

```python
    def __init__(
        self, api_key: str, model: str, *, max_searches: int = 4, timeout_s: float = 600.0,
        client: anthropic.AsyncAnthropic | None = None,
        is_image: Callable[[str], Awaitable[bool]] | None = None,
        search: ImageSearch | None = None,
        extra: Callable[[], str] | None = None, deadline_s: float = DEADLINE_S,
        health: Health | None = None, reader: Reader | None = None, max_reads: int = 8,
        soft_deadline_s: float = 90.0, clock: Callable[[], float] = time.monotonic,
        engine: str = "server",
    ) -> None:
```

and in the body, next to `self._tools = research_tools(model, max_searches)`:

```python
        self._max_searches = max_searches
        self._max_reads = max_reads
        self._soft_deadline = soft_deadline_s
        self._clock = clock
        self._engine = engine
        self._loop_tools = loop_tools(max_searches)
        self._reader = reader if reader is not None else Reader()
        self.last: Lookup | None = None
```

`aclose` also closes the reader:

```python
    async def aclose(self) -> None:
        await self._client.close()
        await self._reader.aclose()
```

5f. In `_research`, pick the text half by engine — replace the `text_call = (...)` assignment with:

```python
        text_call = (self._text(request, query) if want_text
                     else asyncio.sleep(0, result=""))
```

and add the method:

```python
    def _text(self, request: str, query: str) -> Awaitable[str]:
        if self._engine == "loop":
            prompt = research_loop_prompt(self._max_searches, self._max_reads)
            return self._run_loop(_with_extra(prompt, self._extra()), request, query)
        return self._run(_with_extra(RESEARCH_PROMPT, self._extra()), request, query)
```

5g. The loop and its tool handling — new methods on `WebResearcher`:

```python
    async def _run_loop(self, system: str, request: str, query: str) -> str:
        """Claude asks for lookups, this runs them, until it answers or the budget ends it. When
        the reads are spent or the soft deadline passes, the next request has tools switched
        off: the model cannot call one again, so it cannot spin — it answers."""
        lookup = Lookup(reads_left=self._max_reads)
        self.last = lookup
        start = self._clock()
        history: list[dict] = [{"role": "user", "content": research_message(request, query)}]
        turn: list[Any] = []  # the assistant turn being written, across pause_turn resumes
        force = False
        resp = None
        for _ in range(MAX_TURNS):
            messages = [*history, {"role": "assistant", "content": turn}] if turn else history
            resp = await self._create(system, messages, force)
            lookup.count(resp.usage)
            turn = [*turn, *resp.content]
            if resp.stop_reason == "pause_turn":
                continue
            calls = [b for b in resp.content if b.type == "tool_use"]
            if force or resp.stop_reason != "tool_use" or not calls:
                break
            results = await self._tools(lookup, calls, request)
            force = lookup.spent or self._clock() - start >= self._soft_deadline
            if force:
                results.append({"type": "text", "text": FORCE_ANSWER})
            history += [{"role": "assistant", "content": turn},
                        {"role": "user", "content": results}]
            turn = []
        self._log(lookup, start)
        text = final_text(turn)
        question = _question(text)
        if question:
            raise ResearchQuestion(question)
        if not text:
            stop = resp.stop_reason if resp is not None else "none"
            raise ResearchError(f"no answer (stop_reason={stop})")
        return text[:MAX_RESULT_CHARS]

    async def _create(self, system: str, messages: list[dict], force: bool) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": MAX_TOKENS, "system": system,
            "messages": messages, "tools": self._loop_tools,
            # Every turn re-sends the conversation so far; cached, the repeat costs a tenth.
            "cache_control": {"type": "ephemeral"},
        }
        if force:
            kwargs["tool_choice"] = {"type": "none"}
        if self.model.startswith(_EFFORT_PREFIXES):
            kwargs["output_config"] = {"effort": "medium"}
        try:
            resp = await self._client.messages.create(**kwargs)
        except anthropic.APIError as e:
            raise ResearchError(describe(e), self._health.record(e)) from None
        self._health.ok()
        return resp

    async def _tools(self, lookup: Lookup, calls: list[Any], request: str) -> list[dict]:
        """Every client tool call of one turn, answered in one message. The budget is handed out
        in the order the calls came, before any runs; the reads then run side by side."""
        seen: set[str] = set()
        plans = [self._allocate(lookup, call, seen) for call in calls]
        answers = await asyncio.gather(*(self._answer(lookup, plan, request) for plan in plans))
        note = READS_LEFT.format(n=lookup.reads_left)
        return [{"type": "tool_result", "tool_use_id": call.id, "content": body + note,
                 "is_error": failed}
                for call, (body, failed) in zip(calls, answers, strict=True)]

    @staticmethod
    def _allocate(lookup: Lookup, call: Any, seen: set[str]) -> tuple:
        """What to do with one call, decided before anything runs: ("error", why),
        ("refused",), ("cached", url), ("dup",) or ("read", url, look_for, site)."""
        args = call.input if isinstance(call.input, dict) else {}
        try:
            if call.name == "site_search":
                site = str(args.get("site", ""))
                look_for = str(args.get("query", ""))
                url = search_url(site, look_for, str(args.get("deal") or SALE))
            elif call.name == "read":
                site = ""
                look_for = str(args.get("look_for", ""))
                url = str(args.get("url", "")).strip()
                if not url.startswith(("http://", "https://")):
                    raise ValueError("read needs a full http or https address")
            else:
                raise ValueError(f"unknown tool {call.name!r}")
        except ValueError as e:  # SiteError is a ValueError too
            return ("error", TOOL_INPUT_ERROR.format(error=e))
        if url in lookup.pages:
            return ("cached", url)
        if url in seen:
            return ("dup",)
        if lookup.spent:
            return ("refused",)
        lookup.reads_left -= 1
        seen.add(url)
        return ("read", url, look_for, site)

    async def _answer(self, lookup: Lookup, plan: tuple, request: str) -> tuple[str, bool]:
        kind = plan[0]
        if kind == "error":
            return plan[1], True
        if kind == "refused":
            return READ_REFUSED, True
        if kind == "dup":
            return ALREADY_READ, False
        if kind == "cached":
            return lookup.pages[plan[1]], False
        _, url, look_for, site = plan
        if site:
            lookup.sites.append(site)
            lookup.site_reads += 1
        else:
            lookup.page_reads += 1
        try:
            page = await self._reader.read(url)
        except PageUnreadable as e:
            lookup.unreadable += 1
            return READ_FAILED.format(reason=e), True
        lookup.fallbacks += page.via != "jina"
        log.debug("research read %s via %s", url, page.via)
        body = f"{page.title}\n{page.url}\n\n{relevant(page.text, f'{look_for} {request}')}"
        lookup.pages[url] = body
        lookup.chars += len(body)
        return body, False

    def _log(self, lookup: Lookup, start: float) -> None:
        cost = estimate_cost(self.model, input_tokens=lookup.input_tokens,
                             output_tokens=lookup.output_tokens, cache_read=lookup.cache_read,
                             cache_write=lookup.cache_write, searches=lookup.searches)
        sites = f" ({', '.join(dict.fromkeys(lookup.sites))})" if lookup.sites else ""
        log.info("research: %d web search(es), %d site search(es)%s, %d page read(s) "
                 "(%d via fallback, %d unreadable), %.1fk chars, %.0f s, %dk+%dk tok, ~$%.3f",
                 lookup.searches, lookup.site_reads, sites, lookup.page_reads,
                 lookup.fallbacks, lookup.unreadable, lookup.chars / 1000,
                 self._clock() - start,
                 (lookup.input_tokens + lookup.cache_read + lookup.cache_write) // 1000,
                 lookup.output_tokens // 1000, cost)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_research_loop.py tests/test_research.py tests/test_reply.py -q`
Expected: all pass. `tests/test_research.py` (the old engine) is untouched and still green.

Then the whole suite: `uv run pytest -q && uv run ruff check .` — all pass, ruff clean.

If `messages.create` rejects the top-level `cache_control` keyword (`TypeError`), the installed SDK predates it: pass it as `extra_body={"cache_control": {"type": "ephemeral"}}` instead, and keep the test's assertion on the request body.

- [ ] **Step 7: Commit**

```bash
git add app/llm/prompts.py app/llm/research.py tests/test_research_loop.py
git commit -m "feat: a research loop the bot drives, with a budget enforced in code" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: A failed web step is not searched again by the next step

**Files:**
- Modify: `app/conversation/plan.py` (`PlanState`)
- Modify: `app/conversation/orchestrator.py` (`_Turn`, `_step`, `_research`)
- Test: `tests/test_orchestrator.py` (append to the plan tests)

**Interfaces:**
- Produces: `PlanState.skip_web_next: bool = False`; `_Turn.skip_web: bool = False`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_orchestrator.py`, after the existing plan tests (it uses `FakeResearcher`, `FakePlanner`, `plan_interp`, `collect`, `make_interp`, `cand` already defined there):

```python
async def test_after_a_failed_web_step_only_the_next_step_skips_searching(make):
    """The checker answers a failed search with the same search reworded, which doubled a
    10-minute wait for a site that answered the same way. The step after a failure writes what
    it has; a later step searches again, because a plan may look up one item per step."""
    researcher = FakeResearcher(fail=True)
    planner = FakePlanner(["найди квартиру на kv.ee и добавь в идеи"],
                          then=["найди квартиру на kv.ee ещё раз и добавь в идеи",
                                "найди рецепт борща и добавь в идеи"])
    bot = make(researcher=researcher, planner=planner)
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="kv.ee квартира")))
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="kv.ee квартира",
                                             content="таблица: адрес, цена")))
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="рецепт борща")))
    await bot.orch.handle_text(CHAT, USER, "сделай таблицу и найди квартиру на kv.ee",
                               progress=collect([]))

    assert [query for _, query in researcher.asked] == ["kv.ee квартира", "рецепт борща"]
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_orchestrator.py -q -k only_the_next_step_skips`
Expected: FAIL — `researcher.asked` has three queries: the step after the failure searched again.

- [ ] **Step 3: Implement**

`app/conversation/plan.py` — add to `PlanState`, after `field_answers`:

```python
    # Set when a step's web search failed: the next step then writes what it has without
    # searching. The checker's answer to a failed search is the same search reworded, which
    # doubled a 10-minute wait for a site that answered the same way.
    skip_web_next: bool = False
```

`app/conversation/orchestrator.py`, in `_Turn`, after the `outcome: str = "failed"` line:

```python
    # This plan step comes right after one whose web search failed: it does not search.
    skip_web: bool = False
```

In `_step`, directly after `turn.outcome, turn.last_undo = "failed", None`:

```python
        turn.skip_web, turn.plan.skip_web_next = turn.plan.skip_web_next, False
```

In `_research`, directly after the early return for `candidate is None or not candidate.web_query or result.intent not in ("create", "append")`:

```python
        if turn.skip_web:
            log.info("plan: the step before could not search the web; this one writes without")
            return replace(decision, candidate=replace(candidate, web_query=None)), None
```

In `_research`, in both the `except ResearchTimeout` and the `except ResearchError` branches, before their `return`:

```python
            self._web_failed(turn)
```

and the helper on `Orchestrator`, next to `_research`:

```python
    @staticmethod
    def _web_failed(turn: _Turn) -> None:
        if turn.plan is not None:
            turn.plan.skip_web_next = True
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_orchestrator.py tests/test_planner.py tests/test_steps.py -q && uv run pytest -q && uv run ruff check .`
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add app/conversation/plan.py app/conversation/orchestrator.py tests/test_orchestrator.py
git commit -m "fix: after a plan step's web search fails, the next step writes without searching" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `tools/check_sites.py` — the site table, checked live

**Files:**
- Create: `tools/check_sites.py`
- Test: `tests/test_check_sites.py`

**Interfaces:**
- Consumes: `Reader`, `PageUnreadable` (Task 3); `SITES`, `SALE`, `RENT`, `search_url` (Task 1).
- Produces: `works(text: str, probe: str) -> tuple[bool, int, int]`; a CLI that exits 1 when any search fails.

- [ ] **Step 1: Write the failing test** — `tests/test_check_sites.py`:

```python
from tools.check_sites import works


def test_a_results_page_naming_the_query_with_prices_works():
    page = "Piim 1 l 1,39 €\nPiim 2 l 2,49 €\nPiimapulber 3,10 €"
    assert works(page, "piim") == (True, 3, 3)


def test_a_page_without_prices_or_without_the_query_fails():
    assert not works("piim piim piim", "piim")[0]
    assert not works("1,00 € 2,00 € 3,00 €", "piim")[0]


def test_the_probe_matches_in_other_forms():
    assert works("Lasnamäel 1 €\nLasnamäe 2 €\nLasnamäe linnaosa 3 €", "Lasnamäe")[0]
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_check_sites.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.check_sites'`.

- [ ] **Step 3: Implement** — `tools/check_sites.py`:

```python
"""Check every site search in app/web/sites.py live, through the reader the bot uses: the page
must come back naming what was searched, with prices on it.

    uv run python -m tools.check_sites

Exits 1 when any search fails: a shop that redesigned its search shows up here, not as a lookup
that quietly found nothing. Paced to stay under Jina Reader's keyless limit (~20 pages a minute).
"""

from __future__ import annotations

import asyncio
import re
import time

from app.web.reader import PageUnreadable, Reader
from app.web.sites import RENT, SALE, SITES, search_url

PRICE = re.compile(r"\d[\d \u00a0]*[.,]?\d*\s?€|€\s?\d")
MIN_HITS = 3
PACE_S = 3.5


def works(text: str, probe: str) -> tuple[bool, int, int]:
    """(passes, probe hits, prices): the page names what was searched and lists prices."""
    stem = probe.casefold()[:6]
    hits = text.casefold().count(stem)
    prices = len(PRICE.findall(text))
    return hits >= MIN_HITS and prices >= MIN_HITS, hits, prices


async def main() -> int:
    reader = Reader()
    failed = 0
    try:
        for site in SITES:
            for deal in ([SALE, RENT] if site.rent_template else [SALE]):
                url = search_url(site.name, site.probe, deal)
                start = time.monotonic()
                try:
                    page = await reader.read(url)
                    ok, hits, prices = works(page.text, site.probe)
                    via = page.via
                except PageUnreadable as e:
                    ok, hits, prices, via = False, 0, 0, str(e)
                failed += not ok
                print(f"{'WORKS' if ok else 'FAILS':5}  {site.name:16} {deal:4}  "
                      f"{time.monotonic() - start:4.1f}s  hits={hits:<4} prices={prices:<4} "
                      f"via {via}", flush=True)
                await asyncio.sleep(PACE_S)
    finally:
        await reader.aclose()
    print(f"\n{len(SITES)} sites, {failed} search(es) failing")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4: Run the tests, then the live check**

Run: `uv run pytest tests/test_check_sites.py -q && uv run ruff check tools/check_sites.py`
Expected: 3 passed; ruff clean.

Live (network, free, ~1 minute): `PYTHONIOENCODING=utf-8 uv run python -m tools.check_sites`
Expected: every line `WORKS`, exit code 0. If a site prints `FAILS`, do not change the table to make it pass silently: note which one in the task report, and remove it from `SITES` only if a second run fails the same way.

Not in this plan: finding working search addresses for Prisma, Coop, Bauhof, Espak, kinnisvara24 and city24 rentals. The user will ask for more sites later (2026-10-01); this tool is how they will be checked.

- [ ] **Step 5: Commit**

```bash
git add tools/check_sites.py tests/test_check_sites.py
git commit -m "feat: tools/check_sites.py checks every site search live" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `tools/benchmark_research.py` — old and new on the same questions

**Files:**
- Create: `tools/benchmark_research.py`
- Test: `tests/test_benchmark_research.py`

**Interfaces:**
- Consumes: `WebResearcher(..., engine=...)`, `estimate_cost`, `ResearchError` (Task 4); `Reader` (Task 3).
- Produces: `Meter` (wraps an `AsyncAnthropic` client and adds up usage over every call); `QUESTIONS`; a CLI.

- [ ] **Step 1: Write the failing test** — `tests/test_benchmark_research.py`:

```python
from types import SimpleNamespace

from tools.benchmark_research import QUESTIONS, Meter


class FakeMessages:
    async def create(self, **kw):
        return SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, output_tokens=10, cache_read_input_tokens=50,
            cache_creation_input_tokens=0,
            server_tool_use=SimpleNamespace(web_search_requests=1)))


async def test_the_meter_adds_up_every_call_and_resets():
    meter = Meter(SimpleNamespace(messages=FakeMessages()))
    await meter.messages.create(model="m")
    await meter.messages.create(model="m")
    assert (meter.calls, meter.input, meter.output, meter.cache_read, meter.searches) == (
        2, 200, 20, 100, 2)
    meter.reset()
    assert meter.calls == 0 and meter.input == 0


def test_the_questions_cover_real_estate_shops_and_general_research():
    keys = [key for key, _, _ in QUESTIONS]
    assert len(keys) == len(set(keys)) == 8
    assert {"kv", "flats", "panels", "milk", "iphone", "borscht", "trip", "pelevin"} == set(keys)
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/test_benchmark_research.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.benchmark_research'`.

- [ ] **Step 3: Implement** — `tools/benchmark_research.py`:

```python
"""The research loop against the old server-side search, on the same questions.

    uv run python -m tools.benchmark_research [--model claude-sonnet-5] [--only kv,milk]

Needs ANTHROPIC_API_KEY, read from the production .env and never printed. Costs real money:
about $1-3 for the full set, mostly the old path's. The table goes to stdout; the answers go to
data/eval/web/benchmark-<date>.md (git-ignored: listings carry sellers' names and numbers), to
be read for correctness — a fast answer that is wrong is not a win.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.research import ResearchError, WebResearcher, estimate_cost
from app.web.reader import Reader

QUESTIONS = [
    ("kv", "найди на kv.ee квартиру Kalevipoja põik 3-120 и детали объявления",
     "Kalevipoja põik 3-120 kv.ee"),
    ("flats", "найди квартиры в Ласнамяэ до 150 000 евро", "квартиры Lasnamäe продажа до 150000 €"),
    ("panels", "где дешевле клееный щит из сосны — K-Rauta или Ehituse ABC",
     "liimpuit mänd hind K-Rauta Ehituse ABC"),
    ("milk", "сравни цену молока в Rimi и Selver", "piim hind Rimi Selver"),
    ("iphone", "где дешевле всего iPhone 16 в Эстонии", "iPhone 16 hind Eesti"),
    ("borscht", "найди рецепт борща из копчёной курицы", "рецепт борща из копченой курицы"),
    ("trip", "план поездки с ребёнком Хельсинки–Стокгольм на выходные в октябре",
     "Хельсинки Стокгольм с ребёнком выходные октябрь"),
    ("pelevin", "список всех романов Виктора Пелевина", "все романы Виктора Пелевина список"),
]
OUT_DIR = Path("data/eval/web")


class _Keys(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    anthropic_api_key: SecretStr = SecretStr("")


class Meter:
    """Wraps a client's messages.create and adds up usage over every call a lookup makes —
    the old path's server-side loop included, which reports nothing of its own."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages = SimpleNamespace(create=self._create)
        self.reset()

    def reset(self) -> None:
        self.calls = self.input = self.output = self.cache_read = self.cache_write = 0
        self.searches = 0

    async def _create(self, **kwargs: Any) -> Any:
        resp = await self._client.messages.create(**kwargs)
        u = resp.usage
        self.calls += 1
        self.input += getattr(u, "input_tokens", 0) or 0
        self.output += getattr(u, "output_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        tools = getattr(u, "server_tool_use", None)
        self.searches += getattr(tools, "web_search_requests", 0) or 0
        return resp

    async def close(self) -> None:
        await self._client.close()


def build(engine: str, key: str, model: str, meter: Meter) -> WebResearcher:
    if engine == "server":  # what production runs before the switch
        return WebResearcher(key, model, client=meter, max_searches=3, deadline_s=600.0,
                             engine="server")
    return WebResearcher(key, model, client=meter, max_searches=4, max_reads=8,
                         soft_deadline_s=90.0, deadline_s=180.0, reader=Reader(),
                         engine="loop")


async def run(args: argparse.Namespace) -> int:
    key = _Keys(_env_file=args.env).anthropic_api_key.get_secret_value()
    if not key:
        raise SystemExit(f"ANTHROPIC_API_KEY is not set ({args.env})")
    only = set(args.only.split(",")) if args.only else None
    rows, answers = [], []
    for engine in ("server", "loop"):
        meter = Meter(anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=600))
        researcher = build(engine, key, args.model, meter)
        try:
            for name, request, query in QUESTIONS:
                if only and name not in only:
                    continue
                meter.reset()
                start = time.monotonic()
                try:
                    answer, ok = await researcher.research(request, query, "text"), True
                except ResearchError as e:
                    answer, ok = f"[{type(e).__name__}: {e}]", False
                seconds = time.monotonic() - start
                cost = estimate_cost(args.model, input_tokens=meter.input,
                                     output_tokens=meter.output, cache_read=meter.cache_read,
                                     cache_write=meter.cache_write, searches=meter.searches)
                rows.append((name, engine, ok, seconds, meter.input + meter.cache_read
                             + meter.cache_write, meter.output, cost))
                answers.append(f"## {name} — {engine} ({seconds:.0f} s, ~${cost:.3f})\n\n{answer}")
                print(f"{name:8} {engine:6} {'ok' if ok else 'FAIL':4} {seconds:5.0f} s  "
                      f"~${cost:.3f}", flush=True)
        finally:
            await researcher.aclose()
    print("\n| question | engine | answered | s | tokens in | out | ~$ |\n|---|---|---|---|---|---|---|")
    for name, engine, ok, s, tin, tout, cost in rows:
        print(f"| {name} | {engine} | {'yes' if ok else 'no'} | {s:.0f} | {tin:,} | {tout:,} "
              f"| {cost:.3f} |")
    for engine in ("server", "loop"):
        mine = [r for r in rows if r[1] == engine]
        print(f"{engine}: {sum(r[2] for r in mine)}/{len(mine)} answered, "
              f"{sum(r[3] for r in mine):.0f} s, ~${sum(r[6] for r in mine):.2f}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"benchmark-{date.today().isoformat()}.md"
    out.write_text("\n\n".join(answers), encoding="utf-8")
    print(f"answers: {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--only", default="", help="comma-separated question keys")
    p.add_argument("--env", default="C:/apps/ai_assistant/.env")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_benchmark_research.py -q && uv run ruff check tools/benchmark_research.py`
Expected: 2 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add tools/benchmark_research.py tests/test_benchmark_research.py
git commit -m "feat: tools/benchmark_research.py compares the old and the new search" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: The benchmark decides; switch, delete the old path, wire it, document it

**Files:**
- Modify: `app/llm/research.py`, `app/llm/prompts.py`, `app/config.py`, `app/main.py`, `.env.example`, `documentation/ARCHITECTURE.md`
- Modify: `tests/test_research.py`, `tests/test_research_loop.py`, `tests/test_config.py`, `tools/benchmark_research.py`
- Create: `documentation/RESEARCH_BENCHMARK.md`

**Interfaces:**
- Consumes: everything above.
- Produces: `WebResearcher` without `engine` — the loop is the only text half; settings `research_max_searches` (4), `research_max_reads` (8), `research_soft_deadline_s` (90.0), `research_deadline_s` (180.0), `jina_api_key`.

- [ ] **Step 1: Run the benchmark — the gate**

Run: `PYTHONIOENCODING=utf-8 uv run python -m tools.benchmark_research`
(costs about $1–3; takes up to ~25 minutes, mostly the old path)

Then read every answer in `data/eval/web/benchmark-<date>.md`, old beside new, and judge each question: did it answer what was asked, with real data from the sources it cites?

**Switch only if both hold:**
1. The loop answers at least as well as the old path on every question type: real estate (`kv`, `flats`), shops (`panels`, `milk`, `iphone`), general (`borscht`, `trip`, `pelevin`).
2. The loop is cheaper or faster in total.

If either fails, **stop here**. Report the table and what went wrong to the user, and do not do the rest of this task.

- [ ] **Step 2: Record the result** — create `documentation/RESEARCH_BENCHMARK.md` with: the date and model; the table the tool printed; the per-engine totals; one or two sentences per question type on answer quality. No answer text and no listing details: those stay in the git-ignored `data/eval/web/`.

- [ ] **Step 3: Switch the tests first** — in `tests/test_research_loop.py`, drop `engine="loop"` from the `loop()` helper:

```python
    return WebResearcher(KEY, model, client=sdk, reader=reader, **kw)
```

In `tests/test_research.py`: remove `MAX_FETCH_TOKENS` and `research_tools` from the imports; delete `test_tool_versions_follow_the_model`; in `test_research_sends_tools_and_resumes_a_paused_turn` change the expected tool names:

```python
    assert {t["name"] for t in first["tools"]} == {"web_search", "site_search", "read"}
```

Append to `tests/test_config.py`:

```python
def test_research_budget_defaults(env):
    s = Settings(_env_file=None)
    assert (s.research_max_searches, s.research_max_reads) == (4, 8)
    assert (s.research_soft_deadline_s, s.research_deadline_s) == (90.0, 180.0)
    assert s.jina_api_key.get_secret_value() == ""
```

Run: `uv run pytest tests/test_research.py tests/test_research_loop.py tests/test_config.py -q`
Expected: FAIL — `engine` still defaults to `"server"` and the settings do not exist yet.

- [ ] **Step 4: Switch the code**

`app/llm/research.py`:
- Delete `research_tools`, `MAX_FETCH_TOKENS`, `MAX_CONTINUATIONS`, `_DYNAMIC_PREFIXES`, the old `_run` and `self._tools`.
- Rename `_run_loop` to `_run`.
- Remove the `engine` parameter and `self._engine`; `_text` becomes:

```python
    def _text(self, request: str, query: str) -> Awaitable[str]:
        prompt = research_loop_prompt(self._max_searches, self._max_reads)
        return self._run(_with_extra(prompt, self._extra()), request, query)
```

- Set `DEADLINE_S = 180.0` and rewrite its comment: "The backstop behind the soft deadline: the loop answers by itself once its budget or RESEARCH_SOFT_DEADLINE_S is spent, so this only fires when a single call hangs."
- Rewrite the module docstring: "Web research: Claude with three tools — Anthropic's web search, a known shop's or real-estate site's own search (app/web/sites.py), and reading a page (app/web/reader.py, trimmed by app/web/trim.py) — in a loop this module drives, with a budget it enforces. The interpreter only decides *that* something is to be looked up (Candidate.web_query); this turns the query into Markdown with sources, which then goes through the normal write path as the candidate's content."

`app/llm/prompts.py`: delete the old `RESEARCH_PROMPT` and remove its import from `research.py`.

`app/config.py` — replace the research block's two settings with:

```python
    research_max_searches: int = Field(4, ge=1, le=20)
    # Site searches and page reads together, per lookup; past it the model is told to answer.
    research_max_reads: int = Field(8, ge=1, le=40)
    # After this the tools are switched off and the model answers with what it found.
    research_soft_deadline_s: float = Field(90.0, ge=15.0, le=900.0)
    # The backstop: only fires when one call hangs. The user is told it was cut short.
    research_deadline_s: float = Field(180.0, ge=30.0, le=1800.0)
    # Optional: Jina Reader works without a key at ~20 pages a minute; a key raises that.
    jina_api_key: SecretStr = SecretStr("")
```

`app/main.py` — `from app.web.reader import Reader`, and the researcher:

```python
    researcher = (
        WebResearcher(settings.anthropic_api_key.get_secret_value(), settings.research_model,
                      max_searches=settings.research_max_searches,
                      max_reads=settings.research_max_reads,
                      soft_deadline_s=settings.research_soft_deadline_s,
                      reader=Reader(jina_key=settings.jina_api_key.get_secret_value()),
                      is_image=images.is_image, search=ImageSearch(),
                      extra=lambda: tuning.research_note,
                      deadline_s=settings.research_deadline_s, health=health)
        if uses_cloud(settings) else None
    )
```

`tools/benchmark_research.py` — `build()` keeps only the loop branch (no `engine` argument), the `for engine in ("server", "loop")` loops become a single run labelled `"loop"`, and the module docstring says it benchmarks the research loop.

`.env.example` — replace `RESEARCH_MAX_SEARCHES=3` with:

```
RESEARCH_MAX_SEARCHES=4
# Site searches and page reads per lookup, and the seconds after which the tools are switched
# off and the model answers with what it found. RESEARCH_DEADLINE_S is only a backstop.
RESEARCH_MAX_READS=8
RESEARCH_SOFT_DEADLINE_S=90
RESEARCH_DEADLINE_S=180
# Optional: pages are read through Jina Reader (r.jina.ai), free without a key at ~20 pages a
# minute. It sees which public pages are read, not your messages.
JINA_API_KEY=
```

`documentation/ARCHITECTURE.md` — a new paragraph directly after the one that mentions `app/conversation/shared_research.py`:

```markdown
**Web research** (`app/llm/research.py`, `app/web/`). A message that asks to find something is
looked up by a loop the bot drives. Claude gets three tools: `web_search` (Anthropic's, the
basic version without a code sandbox), `site_search` (a shop's or real-estate site's own search
page, from the verified table in `app/web/sites.py`) and `read` (a page through Jina Reader,
which gets past the Cloudflare challenge in front of kv.ee and K-Rauta, with a plain fetch as
fallback — public addresses only, every redirect checked). Pages are cut to what matters before
the model sees them (`app/web/trim.py`). The budget is the code's, not the model's:
`RESEARCH_MAX_SEARCHES` searches and `RESEARCH_MAX_READS` site searches and reads, and after
`RESEARCH_SOFT_DEADLINE_S` the tools are switched off and the model answers with what it has;
`RESEARCH_DEADLINE_S` is a backstop. It replaced Claude's server-side tool loop, which once
spent 20 minutes on a kv.ee listing it had already read in the first 40 seconds
(`docs/superpowers/specs/2026-10-01-web-search-design.md`, `documentation/RESEARCH_BENCHMARK.md`).
`tools/check_sites.py` checks the site table live; `tools/benchmark_research.py` measures a
change.
```

- [ ] **Step 5: Run everything**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass; ruff clean. Then `grep -rn "research_tools\|MAX_FETCH_TOKENS\|_DYNAMIC_PREFIXES\|RESEARCH_PROMPT\b\|engine=" app tools tests` — expected: no hits.

- [ ] **Step 6: Commit**

```bash
git add -A app tools tests documentation .env.example
git commit -m "feat: web research runs on the bounded loop; the server-side loop is gone" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Release

- [ ] **Step 1: Build**

Run: `uv run python release.py --auto --dry-run`, then `uv run python release.py --auto`
Expected: a patch release (no `uv.lock` change); it runs pytest and ruff itself, commits `release: vX.Y.Z`, tags it, and writes `dist/ai_assistant-X.Y.Z.zip`.

- [ ] **Step 2: Hand over** — report the version, the benchmark totals, and that prod still needs `update.cmd` (the bot must be stopped first, or use the `ai_assistant start` scheduled task for a remote restart after updating). Push (`git push && git push --tags`) and update prod only when the user says so.
