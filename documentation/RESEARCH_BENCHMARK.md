# Research benchmark — 2026-10-02

`tools/benchmark_research.py`, model `claude-sonnet-5`, the same eight questions through the old
server-side search (Claude's own web search and fetch, `max_uses` 3, 600 s deadline) and the new
bounded loop (`web_search` + `site_search` + `read` through Jina Reader, 4 searches, 8 reads, 90 s
soft deadline). Answers were read side by side for correctness; they stay in the git-ignored
`data/eval/web/` (listings carry sellers' details).

## Time and cost

| question | old: s | old: ~$ | loop: s | loop: ~$ |
|---|---|---|---|---|
| kv — a flat by address on kv.ee | 325 | 1.144 | 22 → 29* | 0.051 → 0.086* |
| flats — Lasnamäe under 150 000 € | 155 | 0.889 | 19 | 0.035 |
| panels — glued pine panels, K-Rauta vs Ehituse ABC | 60 | 0.354 | 41 | 0.091 |
| milk — Rimi vs Selver | 260 | 0.567 | 73 | 0.162 |
| iphone — cheapest iPhone 16 | 112 | 1.130 | 36 → 16–30* | 0.103 → 0.037–0.093* |
| borscht — a recipe | 68 | 0.288 | 23 | 0.062 |
| trip — Helsinki–Stockholm with a child | 206 | 0.441 | 37 | 0.125 |
| pelevin — all the novels | 110 | 1.012 | 19 | 0.081 |
| **total** | **1 296** | **5.83** | **270** | **0.71** |

\* re-run after the prompt fixes below.

## Answer quality

- **Real estate.** First run: the loop used only kv.ee's own search, which shows active listings,
  and found nothing in the building the old path found four listings in. After the prompt told it
  to also web-search a specific listing, it found them (and the building's details). On "flats
  under 150 000 €" the loop was better from the start: a table of nine matching flats against
  three 12–16 m² rooms.
- **Shops.** Panels: the loop read Ehituse ABC's prices per size and found K-Rauta's panels are
  spruce, not pine; the old path gave up ("rate limit"). Milk: a tie. iPhone: first the loop's
  cheapest was 768.90 € against the old 743 €, and once it used hinnavaatlus.ee the shop names
  were missing — the shops there are logos, and images were stripped. With images turned into
  their alt text and "where is it cheapest" pointed at hinnavaatlus.ee in the tool's own
  description, two runs gave 747 € and 737.08 €, shops named.
- **General.** Borscht: a tie. Trip: the loop's plan is detailed with twelve sources, the old one
  had none. Pelevin: the loop listed every novel to 2024 with sources; the old answer stopped
  after four.

Run-to-run variance is real: the same question can take a different route. A hint stated only in
the system prompt did not hold for the iPhone question; stated in the tool's description, it did.

## Decision

Switched: the loop answers at least as well on every question and costs about an eighth.
