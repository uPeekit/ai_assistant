from app.web.trim import GAP, relevant

PAGE = "\n".join([
    "Kasutame küpsiseid, et leht töötaks",          # cookie banner: dropped
    "[Avaleht](https://rimi.ee/)",                  # menu link: dropped
    "[Kontakt](https://rimi.ee/kontakt)",
    "![logo](https://rimi.ee/logo.png)",            # image: dropped
    *[f"Sissejuhatus {n}" for n in range(30)],     # the start of the page
    "## Otsingu tulemused",                         # heading: kept
    "[Piim 2,5% Alma 1 l](https://rimi.ee/p/1)",    # the term, with its link: kept
    "1,39 €",                                       # price: kept
    "Lisa korvi",                                   # context around the price
    *[f"Muu tekst {n}" for n in range(300)],        # filler
    "[Piim 3,5% Farmi 1 l](https://rimi.ee/p/2)",
    "1,49 €",
])


def test_prices_terms_and_headings_are_kept_with_their_links():
    out = relevant(PAGE, "piim", limit=600)
    assert "[Piim 2,5% Alma 1 l](https://rimi.ee/p/1)" in out
    assert "1,39 €" in out and "1,49 €" in out
    assert "## Otsingu tulemused" in out
    assert "Lisa korvi" in out  # a line of context


def test_cookie_menu_and_image_lines_are_dropped():
    out = relevant(PAGE, "piim", limit=6000)
    assert "küpsis" not in out
    assert "Avaleht" not in out and "Kontakt" not in out
    assert "logo.png" not in out


def test_the_limit_is_held_and_skipped_lines_are_marked():
    out = relevant(PAGE, "piim", limit=300)
    assert len(out) <= 300
    assert GAP in out


def test_words_match_in_other_forms():
    page = "\n".join(["вступление", *["шум"] * 200, "Купить молока в магазине", *["шум"] * 200])
    assert "Купить молока" in relevant(page, "молоко", limit=200)


def test_a_number_in_the_request_finds_its_line():
    page = "\n".join(["algus", *["muu"] * 300, "Korter nr 120, Kalevipoja põik 3", *["muu"] * 300])
    assert "Korter nr 120" in relevant(page, "Kalevipoja põik 3-120", limit=200)


def test_a_page_with_nothing_matching_is_filled_from_its_start():
    assert relevant("first line\nsecond\nthird", "zzzz", limit=100) == "first line\nsecond\nthird"


def test_form_checkboxes_are_noise():
    """kv.ee pages carry their whole search form; its ticked boxes say "korrus" and "elamumaa"
    and crowded the listing's own price out of the page."""
    page = "\n".join(["- [x] Mitte viimane - [x] Viimane korrus",
                      "- [ ] ärimaa - [x] elamumaa (korterelamu)",
                      "# Kalevipoja põik 3, Lasnamäe", "Hind 159 000 €"])
    out = relevant(page, "korrus hind", limit=6000)
    assert "[x]" not in out and "[ ]" not in out
    assert "159 000 €" in out
