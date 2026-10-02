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


async def test_a_page_the_site_answered_404_is_not_read_as_content():
    """Jina answers 200 and passes the site's error page on: kv.ee's 404 for a delisted flat is
    its top 10 listings, which read like the flat the user linked."""
    gone = ("Title: KV.EE\nURL Source: https://www.kv.ee/1\n"
            "Warning: Target URL returned error 404: Not Found\n\nMarkdown Content:\n"
            "## 404 SEE LEHT EI ELA ENAM SIIN\n### TOP 10 KUULUTUSED\nVabaduse väljak 6, 189 000 €")
    r = reader(jina=lambda req: httpx.Response(200, text=gone),
               site=lambda req: httpx.Response(404, html="<p>404</p>"))
    with pytest.raises(PageUnreadable, match="404"):
        await r.read("https://www.kv.ee/1")


async def test_a_never_open_site_is_refused_without_any_request():
    """The research loop reads through this too: a Docs link in the message must not reach
    Jina because the model chose to read it."""
    def boom(req):
        raise AssertionError("no request may be made")

    r = Reader(transport=transport(jina=boom, site=boom), is_public=public,
               never_open=lambda: ["google.com"])
    with pytest.raises(PageUnreadable, match="private"):
        await r.read("https://docs.google.com/document/d/abc")


async def test_a_redirect_to_a_never_open_site_is_refused():
    def site(req):
        return httpx.Response(302, headers={"location": "https://www.swedbank.ee/private"})

    r = Reader(transport=transport(jina=lambda r: httpx.Response(503), site=site),
               is_public=public, never_open=lambda: ["swedbank.ee"])
    with pytest.raises(PageUnreadable, match="private"):
        await r.read("https://bit.ly/x")


def test_a_malformed_link_or_entry_is_simply_not_private():
    from app.web.reader import is_private

    assert not is_private("http://[bad/x", ["google.com"])
    assert not is_private("https://kv.ee/1", ["http://[bad"])


async def test_a_huge_page_is_cut_at_the_byte_cap(monkeypatch):
    monkeypatch.setattr(reader_mod, "MAX_BYTES", 10_000)
    big = "<html><body>" + "<p>rida 1,00 €</p>" * 20_000 + "</body></html>"
    page = await reader(jina=lambda r: httpx.Response(503),
                        site=lambda r: httpx.Response(200, html=big)).read("https://big.ee/")
    assert len(page.text) < 10_000


async def test_a_malformed_url_is_unreadable_not_a_crash():
    """httpx.InvalidURL is not an httpx.HTTPError: uncaught, one bad address the model made up
    ended the whole lookup with the generic error instead of the usual fallback."""
    r = reader(jina=lambda r: httpx.Response(200, text=JINA_OK),
               site=lambda r: httpx.Response(200, html=SHOP))
    with pytest.raises(PageUnreadable):
        await r.read("https://shop.ee/a\nb")
