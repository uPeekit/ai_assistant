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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

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
SITE_ERROR = re.compile(r"target url returned error ([45]\d\d)", re.I)


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str
    via: str  # "jina" or "direct"


class PageUnreadable(Exception):
    """The page could not be read. The message goes to the model: "protected", "not public"."""


def is_private(url: str, never_open: Iterable[str]) -> bool:
    """Whether `url` is on a site the user never wants read: a domain covers its subdomains."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:  # a malformed address is not a site on the list; reading it fails anyway
        return False
    for domain in never_open:
        domain = domain.strip().lower().lstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


class Reader:
    def __init__(self, *, jina_key: str = "", transport: httpx.AsyncBaseTransport | None = None,
                 is_public: Callable[[str], Awaitable[bool]] = is_public_url,
                 never_open: Callable[[], Iterable[str]] = tuple) -> None:
        self._key = jina_key
        self._is_public = is_public
        # The admin page's never-open list, checked on every read: the research loop reads
        # whatever link the model picks, including one the user pasted.
        self._never_open = never_open
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read(self, url: str) -> Page:
        if is_private(url, self._never_open()):
            raise PageUnreadable("private: on the never-open list")
        if not await self._is_public(url):
            raise PageUnreadable("not a public web address")
        # httpx.InvalidURL is not an httpx.HTTPError: an address the model made up (a newline in
        # it, say) would otherwise escape both paths and end the whole lookup.
        try:
            return await self._jina(url)
        except (httpx.HTTPError, httpx.InvalidURL, PageUnreadable) as e:
            log.info("jina could not read a page (%s); fetching it directly", type(e).__name__)
        try:
            return await self._direct(url)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise PageUnreadable(f"could not be fetched ({type(e).__name__})") from None

    async def _jina(self, url: str) -> Page:
        # Images stay in: a shop that is named only by its logo (hinnavaatlus.ee's price list)
        # would lose its name. Trimming turns each image into its alt text.
        headers: dict[str, str] = {}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        resp = await self._client.get(JINA + url, headers=headers, timeout=JINA_TIMEOUT_S,
                                      follow_redirects=True)
        resp.raise_for_status()
        head, title, body = _jina_parts(resp.text)
        if not body or any(CHALLENGE.search(part) for part in (head, title, body[:2000])):
            raise PageUnreadable("protected")
        # Jina answers 200 and passes the site's error page on: kv.ee's 404 for a delisted flat
        # is its top 10 listings, which read like the flat that was asked about.
        failed = SITE_ERROR.search(head)
        if failed:
            raise PageUnreadable(f"the site answered {failed.group(1)}")
        return Page(url, title, body, "jina")

    async def _direct(self, url: str) -> Page:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            async with self._client.stream("GET", current, headers={"User-Agent": BROWSER_UA},
                                           timeout=FETCH_TIMEOUT_S) as resp:
                if resp.is_redirect:
                    target = urljoin(current, resp.headers.get("location", ""))
                    if is_private(target, self._never_open()):
                        raise PageUnreadable("private: redirected to the never-open list")
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
