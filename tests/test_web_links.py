import asyncio

from app.web.links import LINKS_CHARS, MAX_LINKS, LinkReader, find_links, is_private
from app.web.reader import Page, PageUnreadable


def test_links_are_found_in_order_once_and_without_trailing_punctuation():
    text = ("глянь https://www.homes.example/flat-3859999, и ещё «https://grocer.example/p/1». "
            "(https://a.example/x) https://www.homes.example/flat-3859999!")
    assert find_links(text) == ["https://www.homes.example/flat-3859999", "https://grocer.example/p/1",
                                "https://a.example/x"]


def test_a_never_open_domain_covers_its_subdomains_and_nothing_else():
    never = ["google.com", "mybank.example"]
    assert is_private("https://docs.google.com/d/1", never)
    assert is_private("https://mybank.example/login", never)
    assert not is_private("https://notgoogle.com/x", never)
    assert not is_private("https://www.homes.example/1", never)


class FakeReader:
    def __init__(self, slow=(), broken=()):
        self.calls: list[str] = []
        self.slow, self.broken = set(slow), set(broken)

    async def read(self, url):
        self.calls.append(url)
        if url in self.slow:
            await asyncio.sleep(5)
        if url in self.broken:
            raise PageUnreadable("protected")
        return Page(url, "Title", "## Flat\n" + "Price 174 900 €\n" * 2000, "jina")


def reader(fake, never=(), deadline_s=12.0) -> LinkReader:
    return LinkReader(fake, lambda: list(never), deadline_s=deadline_s)


async def test_each_link_is_read_trimmed_and_titled():
    pages = await reader(FakeReader()).read("добавь https://www.homes.example/1 в таблицу")
    [page] = pages
    assert (page.url, page.title, page.error) == ("https://www.homes.example/1", "Title", "")
    assert "174 900 €" in page.text and LINKS_CHARS // 2 < len(page.text) <= LINKS_CHARS


async def test_several_links_share_one_budget():
    pages = await reader(FakeReader()).read("https://a.example/1 https://b.example/2 https://c.example/3")
    assert len(pages) == 3
    assert all(0 < len(page.text) <= LINKS_CHARS // 3 for page in pages)


async def test_a_private_link_is_never_read():
    fake = FakeReader()
    [page] = await reader(fake, never=["google.com"]).read("см. https://docs.google.com/d/1")
    assert page.error == "private" and fake.calls == []


async def test_an_unreadable_link_is_reported_not_raised():
    [page] = await reader(FakeReader(broken={"https://homes.example/1"})).read("https://homes.example/1")
    assert page.error == "protected" and page.text == ""


async def test_a_slow_link_gives_up_at_the_deadline_and_the_rest_still_come_back():
    fake = FakeReader(slow={"https://slow.example/"})
    pages = await reader(fake, deadline_s=0.2).read("https://slow.example/ и https://fast.example/")
    by_url = {p.url: p for p in pages}
    assert by_url["https://slow.example/"].error == "timeout"
    assert by_url["https://fast.example/"].error == "" and by_url["https://fast.example/"].text


async def test_at_most_a_few_links_are_read():
    fake = FakeReader()
    text = " ".join(f"https://a{i}.example/" for i in range(MAX_LINKS + 3))
    pages = await reader(fake).read(text)
    assert len(pages) == MAX_LINKS and len(fake.calls) == MAX_LINKS


async def test_a_message_without_links_reads_nothing():
    fake = FakeReader()
    assert await reader(fake).read("купи молоко") == []
    assert fake.calls == []
