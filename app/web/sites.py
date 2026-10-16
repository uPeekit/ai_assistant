"""Shops and listing sites the research loop can search directly, from `data/sites.json`.

Search engines are slow to index a new listing or a shop's stock; the site's own search page has
them today. The table is the owner's: which sites are worth searching depends on where they
live, so none are built in. A site with a search template gets a direct search (site_search);
one without is only named to the model, which finds it with web_search and reads it with read.
tools/check_sites.py checks each template live.

    [{"name": "shop.example", "kind": "groceries", "template": "https://shop.example/s?q={q}",
      "rent_template": "", "probe": "milk"}]

`kind` is one of app.llm.prompts.SITE_KINDS; "{q}" becomes the quoted query.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

SALE, RENT = "sale", "rent"


@dataclass(frozen=True)
class Site:
    name: str
    kind: str  # what it is for; app.llm.prompts says it to the model in words
    template: str = ""  # "{q}" becomes the quoted query; empty: no direct search
    rent_template: str = ""
    probe: str = ""  # a query tools/check_sites.py expects results and prices for


def load(path: Path) -> tuple[Site, ...]:
    """The sites in the file; none when it is missing or unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as e:
        log.warning("could not read %s (%s); no site searches", path, e)
        return ()
    sites = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
            continue
        template = str(entry.get("template", ""))
        rent = str(entry.get("rent_template", ""))
        if any(t and "{q}" not in t for t in (template, rent)):
            log.warning("site %s: a template without {q} is ignored", entry["name"])
            continue
        sites.append(Site(str(entry["name"]).strip(), str(entry.get("kind", "")), template,
                          rent, str(entry.get("probe", ""))))
    return tuple(sites)


def searchable(sites: Sequence[Site]) -> tuple[Site, ...]:
    return tuple(s for s in sites if s.template)


class SiteError(ValueError):
    """A search the table cannot make. The message goes to the model as the tool's answer."""


def search_url(sites: Sequence[Site], name: str, query: str, deal: str = SALE) -> str:
    by_name = {s.name: s for s in searchable(sites)}
    site = by_name.get(name)
    if site is None:
        raise SiteError(f"unknown site {name!r}; use one of: {', '.join(by_name)}")
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
