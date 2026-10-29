"""An Instagram post as files Telegram can show: a reel's video, a carousel's photos and videos.

Instagram links in a chat show no preview and no player, so a group with the "media" right
gets the post itself in reply. yt-dlp asks Instagram first: it is the only source that tells
a photo from a video and has the post's caption. Instagram refuses anonymous visitors now and
then, so the mirrors (INSTAGRAM_MIRRORS) come next: sites that fetch the post on their own
servers and hand back the file. Mirrors come and go, so they are a setting, not code.

A mirror is a URL template with "{id}" for the post's code. With "{n}" as well it serves the
post's items one by one from 0; past the last one some keep serving the last item again, so a
file seen before ends the post. Without "{n}" it serves the first item only.

Nothing found is None: the caller stays quiet, a chat never sees an error for someone's link."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# A post, a reel or a video, with or without the author's name before it in the path.
LINK = re.compile(r"(?<![\w.])(?:https?://)?(?:www\.|m\.)?instagram\.com/(?:[\w.]+/)?"
                  r"(?:p|reels?|tv)/([\w-]{5,})", re.I)
# A link with the rest of its address: the share parameters after the post's code.
_WHOLE = re.compile(LINK.pattern + r"\S*", re.I)
MAX_LINKS = 3
# What one Telegram album holds.
MAX_ITEMS = 10
# What a bot may upload: 10 MB a photo, 50 MB anything else.
MAX_BYTES = {"photo": 10 * 1024 * 1024, "video": 50 * 1024 * 1024}
TIMEOUT_S = 60.0
# The mirrors answer link-preview robots with the file; a browser gets their web page.
MIRROR_AGENT = "TelegramBot (like TwitterBot)"
# A progressive file (picture and sound in one): no ffmpeg on the machine to join the two.
FORMAT = "best[vcodec!=?none][acodec!=?none]/best"

Kind = Literal["photo", "video"]


@dataclass(frozen=True)
class Item:
    kind: Kind
    data: bytes


@dataclass(frozen=True)
class Post:
    items: list[Item]
    caption: str = ""
    # Where it came from, for the log: "instagram" or the mirror's host.
    source: str = ""


def codes(text: str) -> list[str]:
    """The posts linked in `text`, each once, in order; at most MAX_LINKS."""
    out: list[str] = []
    for m in LINK.finditer(text or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out[:MAX_LINKS]


def bare(text: str) -> bool:
    """Nothing but links to posts: no words around them that would make it a request."""
    return bool(codes(text)) and not _WHOLE.sub("", text).strip()


def mirrors(setting: str) -> list[str]:
    """The templates of INSTAGRAM_MIRRORS (comma-separated); one without "{id}" is no mirror."""
    return [t.strip() for t in setting.split(",") if "{id}" in t]


def _extract(code: str) -> dict | None:
    """What yt-dlp knows of the post, files not downloaded. Blocking: run in a thread."""
    import yt_dlp  # a slow import, needed only when a link turns up

    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "ignore_no_formats_error": True, "format": FORMAT, "socket_timeout": TIMEOUT_S}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f"https://www.instagram.com/p/{code}/", download=False)


def _sources(info: dict) -> list[tuple[Kind, str, dict]]:
    """(kind, url, headers) of each item yt-dlp found: a video has a chosen file; a photo has
    no formats, only its pictures (the largest is the last)."""
    out: list[tuple[Kind, str, dict]] = []
    for entry in info.get("entries") or [info]:
        if not entry:
            continue
        headers = entry.get("http_headers") or {}
        if entry.get("url"):
            out.append(("video", entry["url"], headers))
        elif entry.get("thumbnails"):
            out.append(("photo", entry["thumbnails"][-1]["url"], headers))
    return out[:MAX_ITEMS]


class Instagram:
    def __init__(self, mirror_templates: list[str], *,
                 extract: Callable[[str], dict | None] = _extract,
                 client: httpx.AsyncClient | None = None) -> None:
        self._mirrors = mirror_templates
        self._extract = extract
        self._client = client

    async def fetch(self, code: str) -> Post | None:
        """The post's items, from Instagram or else the first mirror that has them."""
        async with self._http() as http:
            post = await self._direct(code, http)
            if post is not None:
                return post
            for template in self._mirrors:
                post = await self._mirror(code, template, http)
                if post is not None:
                    return post
        return None

    def _http(self):
        if self._client is not None:
            return _Borrowed(self._client)
        return httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True)

    async def _direct(self, code: str, http: httpx.AsyncClient) -> Post | None:
        try:
            info = await asyncio.to_thread(self._extract, code)
        except Exception as e:  # yt-dlp raises its own errors, and anything else on a bad day
            log.info("instagram: yt-dlp found nothing (%s)", type(e).__name__)
            return None
        if not info:
            return None
        items: list[Item] = []
        for kind, url, headers in _sources(info):
            got = await _download(http, url, headers)
            if got is None:
                return None  # half a carousel is not the post: let a mirror try it whole
            items.append(Item(kind, got[1]))
        if not items:
            return None
        return Post(items, caption=(info.get("description") or "").strip(), source="instagram")

    async def _mirror(self, code: str, template: str, http: httpx.AsyncClient) -> Post | None:
        host = urlsplit(template).hostname or template
        numbered = "{n}" in template
        items: list[Item] = []
        seen: set[str] = set()
        headers = {"User-Agent": MIRROR_AGENT}
        for n in range(MAX_ITEMS if numbered else 1):
            url = template.format(id=code, n=n)
            got = await _download(http, url, headers)
            if got is None and n > 0:
                # A mirror fetches each item from Instagram as it is asked, and falters now
                # and then (seen: two photos of nine, then nothing): once more before the end.
                got = await _download(http, url, headers)
            if got is None:
                break
            digest = hashlib.sha256(got[1]).hexdigest()
            if digest in seen:
                break  # past the last item: the last one again
            seen.add(digest)
            items.append(Item(*got))
        if not items:
            log.info("instagram: mirror %s had nothing", host)
            return None
        return Post(items, source=host)


class _Borrowed:
    """A client the caller owns, used as `async with` without closing it."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *exc) -> None:
        return None


def _kind(content_type: str) -> Kind | None:
    kind = content_type.split(";")[0].strip().casefold()
    if kind.startswith("image/"):
        return "photo"
    if kind.startswith("video/"):
        return "video"
    return None


async def _download(http: httpx.AsyncClient, url: str,
                    headers: dict) -> tuple[Kind, bytes] | None:
    """The file at `url` with its kind (from its type), or None: not there, not a picture or a
    video (a mirror's error page), or larger than Telegram takes from a bot."""
    try:
        async with http.stream("GET", url, headers=headers) as r:
            kind = _kind(r.headers.get("content-type", ""))
            if r.status_code != 200 or kind is None:
                return None
            limit = MAX_BYTES[kind]
            if int(r.headers.get("content-length") or 0) > limit:
                return None
            data = bytearray()
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > limit:
                    return None
            return kind, bytes(data)
    except httpx.HTTPError as e:
        log.info("instagram: a download failed (%s)", type(e).__name__)
        return None

