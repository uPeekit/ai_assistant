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
