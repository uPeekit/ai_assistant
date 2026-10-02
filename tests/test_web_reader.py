import httpx
import pytest

from app.web import reader as reader_mod
from app.web.reader import PageUnreadable, Reader

JINA_OK = ("Title: Korter\nURL Source: https://kv.ee/1\n\n"
           "Markdown Content:\n## Korter\n149 990 €")
SHOP = ("<html><head><title>Pood</title><script>x = 1</script></head><body>"
        "<div><a href='/p/1'>Piim</a></div><div>1,39 €</div></body></html>")


async def public(url: str) -> bool:
    return not any(h in url for h in ("127.0.0.1", "localhost", "192.168."))


def transport(jina=None, site=None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            return jina(request) if jina else httpx.Response(500)
        return site(request) if site else httpx.Response(500)
    return httpx.MockTransport(handler)


def reader(**kw) -> Reader:
    return Reader(transport=transport(**kw), is_public=public)


async def test_jina_reads_the_page_and_carries_the_key():
    seen = []

    def jina(req):
        seen.append(req)
        return httpx.Response(200, text=JINA_OK)

    r = Reader(jina_key="k", transport=transport(jina=jina), is_public=public)
    page = await r.read("https://kv.ee/1")
    assert (page.title, page.via) == ("Korter", "jina")
    assert "149 990 €" in page.text
    assert str(seen[0].url) == "https://r.jina.ai/https://kv.ee/1"
    assert seen[0].headers["authorization"] == "Bearer k"
    # Images stay: a shop named only by its logo keeps its name (trim turns it into alt text).
    assert "x-retain-images" not in seen[0].headers


async def test_a_jina_failure_falls_back_to_a_direct_fetch():
    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    refused = ("Title: kv.ee\nWarning: Target URL returned error 403: Forbidden\n\n"
               "Markdown Content:\nJust a moment...")
    for jina in (lambda r: httpx.Response(429), slow,
                 lambda r: httpx.Response(200, text=refused)):
        page = await reader(jina=jina, site=lambda r: httpx.Response(200, html=SHOP)).read(
            "https://shop.ee/")
        assert (page.via, page.title) == ("direct", "Pood")
        assert "[Piim](https://shop.ee/p/1)" in page.text
        assert "x = 1" not in page.text  # scripts dropped


async def test_a_challenge_on_both_paths_is_unreadable():
    challenge = "<html><head><title>Just a moment...</title></head><body>wait</body></html>"
    r = reader(jina=lambda r: httpx.Response(
        200, text="Title: Just a moment...\n\nMarkdown Content:\nchecking"),
        site=lambda r: httpx.Response(200, html=challenge))
    with pytest.raises(PageUnreadable, match="protected"):
        await r.read("https://kv.ee/1")


async def test_an_error_status_on_the_direct_fetch_is_unreadable():
    r = reader(jina=lambda r: httpx.Response(503), site=lambda r: httpx.Response(403))
    with pytest.raises(PageUnreadable, match="403"):
        await r.read("https://kv.ee/1")


async def test_a_private_address_is_refused_without_any_request():
    def boom(req):
        raise AssertionError("no request may be made")

    with pytest.raises(PageUnreadable, match="not a public"):
        await reader(jina=boom, site=boom).read("http://127.0.0.1:8787/api/targets")


async def test_a_redirect_to_a_private_address_is_refused():
    def site(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8787/api/targets"})

    with pytest.raises(PageUnreadable, match="not public"):
        await reader(jina=lambda r: httpx.Response(503), site=site).read("https://evil.ee/go")


async def test_a_huge_page_is_cut_at_the_byte_cap(monkeypatch):
    monkeypatch.setattr(reader_mod, "MAX_BYTES", 10_000)
    big = "<html><body>" + "<p>rida 1,00 €</p>" * 20_000 + "</body></html>"
    page = await reader(jina=lambda r: httpx.Response(503),
                        site=lambda r: httpx.Response(200, html=big)).read("https://big.ee/")
    assert len(page.text) < 10_000
