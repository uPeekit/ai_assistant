# Web search: a bounded research loop — design

2026-10-01. Approved in conversation section by section; this is the written spec.

## Goal

The bot can look things up on the web — general research, and Estonian shops and real estate
("details of one thing", "find and compare") — reliably and cheaply. A lookup always ends in an
answer within about 90 seconds and a fraction of a dollar, and can never spiral.

Out of scope: watching over time (new listings, price drops), editing the site table from the
admin page, picture search (Wikimedia + cited pages stays as it is). More sites will be added
later at the user's request.

## Why — what was measured

- **The failure was the loop, not access.** Event 118 ("find Kalevipoja põik 3-120 on kv.ee")
  ran 20.7 minutes and wrote nothing: two research calls each hit the 600 s deadline. Replayed,
  Anthropic's `web_fetch` read the kv.ee listing at 40 s; the model then spent minutes on
  refused searches past `max_uses` and ran `sleep 15/30/60` in the dynamic-filtering sandbox,
  reading 645k input tokens (~$1.40). The cap was never stated to the model.
- **Usage** (2026-09-18 → 10-01): ~20 research calls (16 ok, 2 timed out, 2 failed), median
  37k input tokens, max 279k. About 1.5 lookups a day: reliability and the tail matter more than
  the price of a typical call.
- **Access.** A plain fetch from the user's PC reads 12 of 14 likely sites; kv.ee and K-Rauta
  answer with a Cloudflare challenge (403). A headless real browser (Edge via Playwright) on the
  PC is challenged too. **Jina Reader** (`r.jina.ai`, keyless) read both, and shop and listing
  search pages with prices; 4–7 s a page uncached, under 1 s cached.

## Design

`WebResearcher.research(request, query, media) -> str` keeps its signature, its exceptions
(`ResearchQuestion`, `ResearchTimeout`, `ResearchError`), its answer format (Markdown, first line
a `##` heading, `## Источники` at the end, the `ВОПРОС:` line, `MAX_RESULT_CHARS`) and its
picture half. Only the text half changes: the server-side tool loop is replaced by a loop the
bot drives. `SharedResearch`, the orchestrator, the vault side and plans are unchanged except for
the plan rule below.

### Units

- `app/web/reader.py` — `Reader.read(url) -> Page(url, title, text, via)`. Jina Reader first
  (`https://r.jina.ai/<url>`, `X-Retain-Images: none`, 20 s timeout, optional `JINA_API_KEY`
  header); on an HTTP error, timeout, rate limit or a challenge page, a plain fetch with a
  browser user agent, HTML reduced to text. Both failing → `PageUnreadable("protected")`.
  Challenge pages are recognised by their title/text ("Just a moment", "security verification").
  Only http(s) URLs whose host resolves to public addresses are read — `is_public_url` from
  `app/notion/images.py`, reused — because the fallback runs on the user's PC and a page could
  otherwise steer the model at the admin page (`127.0.0.1:8787`) or the home network.
- `app/web/trim.py` — `relevant(text, terms, limit=6000) -> str`, pure. Keeps the title and
  headings; lines with prices (`€`, `EUR`), sizes and rooms (`m²`, `m2`, `tuba`, `toa`,
  `korrus`, year built); lines with any term (the read's `look_for` and the request), matched on
  stems the way `app/vault/index.py` matches Russian; one line of context either side; links on
  kept lines. Drops cookie/consent lines, image lines and link-only menu lines. Under the limit,
  it tops up from the start of the page.
- `app/web/sites.py` — the site table: name, what it is for, search URL template, for real
  estate a sale/rent variant. Plain data plus `search_url(site, query, deal)`. Initial table,
  each verified on 2026-10-01 to return the query and prices through Jina:

  | site | for | template |
  |---|---|---|
  | kv.ee | real estate, sale | `https://www.kv.ee/search?deal_type=1&keyword={q}` |
  | kv.ee | real estate, rent | `https://www.kv.ee/search?deal_type=2&keyword={q}` |
  | city24.ee | real estate, sale | `https://www.city24.ee/real-estate-search/apartments-for-sale?search={q}` |
  | rimi.ee | groceries | `https://www.rimi.ee/epood/ee/otsing?query={q}` |
  | selver.ee | groceries | `https://www.selver.ee/search?q={q}` |
  | k-rauta.ee | building | `https://www.k-rauta.ee/otsing?q={q}` |
  | ehituseabc.ee | building | `https://www.ehituseabc.ee/search?q={q}` |
  | kaup24.ee | general goods, electronics | `https://kaup24.ee/et/search?q={q}` |
  | euronics.ee | electronics | `https://www.euronics.ee/search?q={q}` |
  | hinnavaatlus.ee | price comparison across shops | `https://www.hinnavaatlus.ee/search/?Type=products&Query={q}` |

  2026-10-02, during the build: city24.ee and ehituseabc.ee failed `tools/check_sites.py` twice
  — their results render in the browser after Jina captures the page, so only menus came back —
  and were taken out of the table. k-rauta.ee failed once and passed once; it stays, flaky.

  Not working yet (pages without results or prices): Prisma, Coop (ecoop.ee), Bauhof, Espak,
  kinnisvara24, city24 rentals. They are reachable through search + read. Finding working
  addresses for them is deferred: the user will ask for more sites later, and each will be added
  only once it passes `tools/check_sites.py`.
- The loop — in `app/llm/research.py`, replacing `_run`.

### The loop

Tools given to the model:

- `search` — Anthropic's web search, **basic version** (`web_search_20250305`, no code
  sandbox), `max_uses` = the search budget. A server tool: the API runs it inside the request.
- `site_search(site, query, deal?)` — a client tool; `site` is an enum of the table's names.
  The description says what each site is for, and that the query must be in the site's language
  (Estonian product words and street names: "piim", not "молоко").
- `read(url, look_for)` — a client tool; returns the trimmed page.

Each turn: send the conversation; on `tool_use`, run the client calls concurrently, return all
results in one user message, repeat; `pause_turn` is resumed as today. Every tool result ends
with what is left ("reads left: 3").

Budget, enforced in code:

| | default | setting |
|---|---|---|
| web searches | 4 | `RESEARCH_MAX_SEARCHES` (enforced by the API as `max_uses`) |
| site searches + reads together | 8 | `RESEARCH_MAX_READS` |
| time before the forced answer | 90 s | `RESEARCH_SOFT_DEADLINE_S` |
| hard stop | 180 s | `RESEARCH_DEADLINE_S` (was 600) |

A call past the read budget gets "budget spent — not read" instead of a page. When the reads are
spent or the soft deadline passes, the next request carries `tool_choice: {"type": "none"}` and
a line telling the model to answer with what it has: it cannot call a tool again. The hard stop
remains `ResearchTimeout` and is a backstop only. A URL read twice in one lookup is served from
memory and not charged. Top-level prompt caching is on, so the history re-sent each turn is billed
at the cache rate. The model is `RESEARCH_MODEL` (Sonnet 5 today). Where the model accepts it
(Sonnet 5, the Opus and Fable models), the request sets `output_config.effort: "medium"`;
Haiku 4.5 rejects `effort`, so it is left out there. `RESEARCH_PROMPT` is rewritten for the three tools, states the
budget, and says to prefer `site_search` for the listed shops and real estate.

Expected cost: ~$0.04–0.08 a typical lookup, ~$0.20 worst case; confirmed by the benchmark.

### Plans

After a plan step's web research fails (`ResearchError` or `ResearchTimeout`), the orchestrator
drops the `web_query` of the **next** step the checker plans, in code; that step still writes
what it has. Only the next one: a plan that looks up one item per step ("each of Pelevin's
novels") must keep searching for the later items. This replaces relying on the checker prompt's
"retry differently, once", which produced the same kv.ee lookup again and doubled the wait.

### Failures

| what | result |
|---|---|
| Jina fails / rate-limited / challenge | plain fetch |
| plain fetch fails or is challenged | the model is told "protected, could not read" and goes on |
| not a public http(s) address | refused, the model is told so |
| budget or soft deadline reached | forced answer from what was found — not an error |
| Anthropic unavailable | `ResearchError` with the `health` reason, as today |
| empty answer | `ResearchError("no answer")`, as today |
| hard stop | `ResearchTimeout`, as today |

### Logging

One INFO line per lookup — counts, site names, how many reads fell back, characters read,
seconds, tokens, estimated cost:
`research: 1 search, 2 site searches (kv.ee), 3 reads (1 via fallback), 9.8k chars, 31 s,
24k+1k tok, ~$0.05`. URLs only at DEBUG: they can carry addresses, and message text already
stays out of INFO (`tests/test_security.py`).

## Testing

Unit, test-first, no network:
- `trim` on hand-made pages: prices and terms kept with context, cookie and menu lines dropped,
  links kept, the limit held, top-up from the start.
- `sites`: templates, quoting of Estonian letters and spaces, sale/rent.
- `reader` with `httpx.MockTransport`: Jina ok; Jina 429 and timeout → fallback; challenge page
  → fallback → `PageUnreadable`; private and loopback addresses refused without a request.
- the loop with a scripted fake Anthropic client: tool calls run and answered in one message;
  parallel calls; the read budget turning into `tool_choice: none`; the soft deadline doing the
  same; a repeated URL not charged; the hard stop raising `ResearchTimeout`; `ВОПРОС:` →
  `ResearchQuestion`; the answer format unchanged.
- plans: a failed web step strips the next step's `web_query`.

Live, before the switch — `tools/benchmark_research.py`, the old and the new path on the same
questions, recording time, tokens, estimated cost and whether an answer came back; the answers are
read for correctness. Questions: the kv.ee flat at Kalevipoja põik 3-120; flats in Lasnamäe under
150 000 €; glued pine panels at K-Rauta vs Ehituse ABC; milk at Rimi vs Selver; the cheapest
iPhone 16; borscht with smoked chicken; a weekend Helsinki–Stockholm trip with a child; Pelevin's
novels. Pages captured for it stay in git-ignored `data/eval/web/` (listings carry sellers'
names and phone numbers); unit tests never use them.

`tools/check_sites.py` — the live check of every template (query word and prices come back).

## Rollout

One release. It ships only if the new path answers at least as well as the old on the
benchmark, and is cheaper or faster. The server-side loop and its constants are deleted in the
same release. Docs: the research section of `documentation/ARCHITECTURE.md`; `.env.example`
gains `JINA_API_KEY` (optional) and the new `RESEARCH_*` settings.
