"""An Instagram post as files (app/media/instagram.py): Instagram first, then the mirrors in
order; nothing found is None. No network: yt-dlp and the web are stand-ins."""

from __future__ import annotations

import httpx
import pytest

from app.media import instagram as ig
from app.media.instagram import Instagram, codes, mirrors

VX = "https://vx.example/offload/{id}/{n}"
KK = "https://kk.example/p/{id}/"
JPEG, MP4 = "image/jpeg", "video/mp4"


def test_links_are_found_in_any_shape_each_once():
    text = ("глянь https://www.instagram.com/p/DeXghYBkRlW/?mdxt=aGhvN294dWRwcmsz и "
            "instagram.com/reel/CrUJOHFNEQC/ не ссылка, а это "
            "https://instagram.com/reels/AbCdE12345 https://m.instagram.com/nasa/p/Xy-z_9876/ "
            "https://www.instagram.com/p/DeXghYBkRlW/")
    assert codes(text) == ["DeXghYBkRlW", "CrUJOHFNEQC", "AbCdE12345"]
    assert codes("https://m.instagram.com/nasa/p/Xy-z_9876/") == ["Xy-z_9876"]
    assert codes("https://www.instagram.com/nasa/") == []
    assert codes("https://notinstagram.com/p/DeXghYBkRlW/") == []
    assert codes("") == []


def test_at_most_a_few_posts_per_message():
    text = " ".join(f"https://www.instagram.com/p/Code{i}xyz/" for i in range(6))
    assert len(codes(text)) == ig.MAX_LINKS


def test_a_template_without_the_post_is_no_mirror():
    assert mirrors(f" {VX} , https://dead.example/, {KK},") == [VX, KK]
    assert mirrors("") == []


class Web:
    """The web: a URL → (status, content type, body); anything else is 404."""

    def __init__(self, pages: dict[str, tuple[int, str, bytes]]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.asked.append(url)
        status, kind, body = self.pages.get(url, (404, "text/html", b"not here"))
        return httpx.Response(status, headers={"content-type": kind}, content=body)


def client(web: Web) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(web))


def refuses(code):
    raise RuntimeError("login required")


async def test_a_reel_comes_from_instagram_with_its_caption():
    web = Web({"https://cdn.example/reel.mp4": (200, MP4, b"video")})
    info = {"id": "R", "url": "https://cdn.example/reel.mp4", "description": " Earth at night ",
            "http_headers": {"User-Agent": "x"}}
    post = await Instagram([VX], extract=lambda code: info, client=client(web)).fetch("R")
    assert [(i.kind, i.data) for i in post.items] == [("video", b"video")]
    assert post.caption == "Earth at night" and post.source == "instagram"
    assert web.asked == ["https://cdn.example/reel.mp4"]  # no mirror asked


async def test_a_carousel_from_instagram_is_its_photos_and_videos_in_order():
    web = Web({"https://cdn.example/1.jpg": (200, JPEG, b"one"),
               "https://cdn.example/2.mp4": (200, MP4, b"two")})
    info = {"_type": "playlist", "description": "Talk about complicated",
            "entries": [{"thumbnails": [{"url": "https://cdn.example/small.jpg"},
                                        {"url": "https://cdn.example/1.jpg"}]},
                        {"url": "https://cdn.example/2.mp4"}]}
    post = await Instagram([], extract=lambda code: info, client=client(web)).fetch("C")
    assert [(i.kind, i.data) for i in post.items] == [("photo", b"one"), ("video", b"two")]
    assert post.caption == "Talk about complicated"


async def test_when_instagram_refuses_the_numbered_mirror_serves_every_item():
    # Past the last item this mirror serves the last one again.
    web = Web({"https://vx.example/offload/C/0": (200, JPEG, b"one"),
               "https://vx.example/offload/C/1": (200, MP4, b"two"),
               "https://vx.example/offload/C/2": (200, MP4, b"two")})
    post = await Instagram([VX, KK], extract=refuses, client=client(web)).fetch("C")
    assert [(i.kind, i.data) for i in post.items] == [("photo", b"one"), ("video", b"two")]
    assert post.source == "vx.example" and post.caption == ""
    assert not any("kk.example" in u for u in web.asked)


async def test_an_item_the_mirror_faltered_on_is_asked_for_once_more():
    web = Web({"https://vx.example/offload/C/0": (200, JPEG, b"one"),
               "https://vx.example/offload/C/1": (200, JPEG, b"two")})
    flaky = {"https://vx.example/offload/C/1"}

    def once(request):
        if str(request.url) in flaky:
            flaky.clear()
            return httpx.Response(502, content=b"upstream")
        return web(request)
    instagram = Instagram([VX], extract=refuses,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(once)))
    post = await instagram.fetch("C")
    assert [i.data for i in post.items] == [b"one", b"two"]


async def test_a_dead_mirror_is_passed_for_the_next_one():
    web = Web({"https://vx.example/offload/R/0": (504, "text/html", b"gateway"),
               "https://kk.example/p/R/": (200, MP4, b"video")})
    post = await Instagram([VX, KK], extract=lambda code: None, client=client(web)).fetch("R")
    assert [i.data for i in post.items] == [b"video"] and post.source == "kk.example"


async def test_a_mirrors_web_page_is_not_a_post():
    web = Web({"https://kk.example/p/R/": (200, "text/html", b"<html>ad</html>")})
    assert await Instagram([KK], extract=refuses, client=client(web)).fetch("R") is None


async def test_half_a_carousel_from_instagram_lets_a_mirror_bring_it_whole():
    web = Web({"https://cdn.example/1.jpg": (200, JPEG, b"one"),
               "https://kk.example/p/C/": (200, JPEG, b"first")})
    info = {"entries": [{"thumbnails": [{"url": "https://cdn.example/1.jpg"}]},
                        {"url": "https://cdn.example/gone.mp4"}]}
    post = await Instagram([KK], extract=lambda code: info, client=client(web)).fetch("C")
    assert post.source == "kk.example"


async def test_too_big_for_telegram_is_not_downloaded_whole(monkeypatch):
    monkeypatch.setitem(ig.MAX_BYTES, "video", 10)
    web = Web({"https://kk.example/p/R/": (200, MP4, b"x" * 11)})
    assert await Instagram([KK], extract=refuses, client=client(web)).fetch("R") is None


async def test_nothing_anywhere_is_none():
    assert await Instagram([VX, KK], extract=refuses, client=client(Web({}))).fetch("R") is None


@pytest.mark.parametrize("error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_a_network_failure_is_nothing_found(error):
    def broken(request):
        raise error
    instagram = Instagram([KK], extract=refuses,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    assert await instagram.fetch("R") is None
