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

PRICE = re.compile(r"\d[\d  ]*[.,]?\d*\s?€|€\s?\d")
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
