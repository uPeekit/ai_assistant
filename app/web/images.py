"""Is an image link from the web real? Web research keeps only the ones that are.

The URLs come from a web search, i.e. from arbitrary pages, so only plain http(s) to a public
address is asked, every redirect included: without that, a page could point the bot at
http://127.0.0.1:8787 (the admin page) or at the router on the local network."""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import httpx

from app.web import reader
from app.web.reader import USER_AGENT

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml", "image/bmp",
    "image/tiff", "image/heic",
})


class NotAnImage(Exception):
    pass


class ImageCheck:
    def __init__(self, *, timeout_s: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout_s, transport=transport, follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def is_image(self, url: str) -> bool:
        """Whether `url` serves an image — status and headers only, the body is never read.
        Lets web research drop dead or blocked links before anything is written."""
        try:
            await self._head(url)
        except NotAnImage as e:
            log.info("image link dropped (%s)", e)
            return False
        return True

    async def _head(self, url: str) -> None:
        for _ in range(MAX_REDIRECTS + 1):
            if not await reader.is_public_url(url):
                raise NotAnImage(f"not a public http(s) address: {url}")
            try:
                async with self._http.stream("GET", url) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code != 200:
                        raise NotAnImage(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in IMAGE_TYPES:
                        raise NotAnImage(f"content type {ctype or '?'}")
                    length = resp.headers.get("content-length", "")
                    if length.isdigit() and int(length) > MAX_IMAGE_BYTES:
                        raise NotAnImage("larger than 5 MB")
                    return
            except httpx.HTTPError as e:
                raise NotAnImage(type(e).__name__) from None
        raise NotAnImage("too many redirects")
