"""An image link from web research is kept only when it really serves an image, from a public
address (app/web/images.py)."""

import httpx
import pytest

from app.web import reader
from app.web.images import MAX_IMAGE_BYTES, ImageCheck

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


@pytest.fixture
def public(monkeypatch):
    """Every host counts as public except `internal.lan`: DNS is not something tests touch."""
    async def _public(url: str) -> bool:
        return httpx.URL(url).host != "internal.lan"

    monkeypatch.setattr(reader, "is_public_url", _public)


def check(handler) -> ImageCheck:
    return ImageCheck(transport=httpx.MockTransport(handler))


async def test_private_and_non_http_addresses_are_not_public():
    for url in ("http://127.0.0.1:8787/", "http://10.0.0.1/x.png", "http://192.168.1.1/a.jpg",
                "http://[::1]/a.png", "ftp://example.com/a.png", "file:///etc/passwd"):
        assert await reader.is_public_url(url) is False, url


async def test_an_image_reads_headers_only(public):
    read = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            read.append(True)
            yield PNG

    images = check(lambda req: httpx.Response(200, stream=Body(),
                                              headers={"content-type": "image/png"}))
    assert await images.is_image("https://example.com/a.png") is True
    assert read == []


async def test_a_declared_oversize_a_page_and_a_missing_file_are_not_images(public):
    images = check(lambda req: httpx.Response(
        200, content=b"x", headers={"content-type": "image/jpeg",
                                    "content-length": str(MAX_IMAGE_BYTES + 1)}))
    assert await images.is_image("https://example.com/big.jpg") is False
    images = check(lambda req: httpx.Response(200, text="<html>",
                                              headers={"content-type": "text/html"}))
    assert await images.is_image("https://example.com/page") is False
    assert await check(lambda req: httpx.Response(404)).is_image("https://example.com/x.jpg") \
        is False


async def test_a_redirect_into_the_local_network_is_refused(public):
    def handler(req):
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal.lan/secret.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    assert await check(handler).is_image("https://example.com/redirect.png") is False


async def test_a_redirect_to_a_public_image_is_followed(public):
    def handler(req):
        if req.url.path == "/old.png":
            return httpx.Response(301, headers={"location": "/new.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    assert await check(handler).is_image("https://example.com/old.png") is True
