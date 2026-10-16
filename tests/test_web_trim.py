from app.web.trim import GAP, page_facts, relevant

PAGE = "\n".join([
    "We use cookies to make the site work",            # cookie banner: dropped
    "[Home](https://shop.example/)",                   # menu link: dropped
    "[Contact](https://shop.example/contact)",
    "![logo](https://shop.example/logo.png)",          # image: dropped
    *[f"Introduction {n}" for n in range(30)],         # the start of the page
    "## Search results",                               # heading: kept
    "[Milk 2.5% Dairyland 1 l](https://shop.example/p/1)",  # the term, with its link: kept
    "1,39 €",                                          # price: kept
    "Add to basket",                                   # context around the price
    *[f"Other text {n}" for n in range(300)],          # filler
    "[Milk 3.5% Farmhouse 1 l](https://shop.example/p/2)",
    "1,49 €",
])


def test_prices_terms_and_headings_are_kept_with_their_links():
    out = relevant(PAGE, "milk", limit=600)
    assert "[Milk 2.5% Dairyland 1 l](https://shop.example/p/1)" in out
    assert "1,39 €" in out and "1,49 €" in out
    assert "## Search results" in out
    assert "Add to basket" in out  # a line of context


def test_cookie_menu_and_image_lines_are_dropped():
    out = relevant(PAGE, "milk", limit=6000)
    assert "cookies" not in out
    assert "Home" not in out and "Contact" not in out
    assert "logo.png" not in out


def test_the_limit_is_held_and_skipped_lines_are_marked():
    out = relevant(PAGE, "milk", limit=300)
    assert len(out) <= 300
    assert GAP in out


def test_words_match_in_other_forms():
    page = "\n".join(["вступление", *["шум"] * 200, "Купить молока в магазине", *["шум"] * 200])
    assert "Купить молока" in relevant(page, "молоко", limit=200)


def test_a_number_in_the_request_finds_its_line():
    page = "\n".join(["start", *["other"] * 300, "Flat no 120, Elm Street 3", *["other"] * 300])
    assert "Flat no 120" in relevant(page, "Elm Street 3-120", limit=200)


def test_a_page_with_nothing_matching_is_filled_from_its_start():
    assert relevant("first line\nsecond\nthird", "zzzz", limit=100) == "first line\nsecond\nthird"


def test_form_checkboxes_are_noise():
    """Listing pages carry their whole search form; its ticked boxes say "floor" and
    "residential" and crowded the listing's own price out of the page."""
    page = "\n".join(["- [x] Not the top floor - [x] Top floor",
                      "- [ ] commercial land - [x] residential land (apartment block)",
                      "# Elm Street 3, Riverside", "Price 159 000 €"])
    out = relevant(page, "floor price", limit=6000)
    assert "[x]" not in out and "[ ]" not in out
    assert "159 000 €" in out


def test_an_image_becomes_its_alt_text_so_a_shop_named_only_by_its_logo_keeps_its_name():
    """A price comparison site names each shop only by a logo: drop the images and the price
    list loses who sells at that price."""
    page = ("[![Image 16: GadgetStore logo](https://img.prices.example/f/gadgetstore.jpg)]"
            "(https://www.prices.example/dealer/336-gadgetstore)\n743,00 €")
    out = relevant(page, "iphone", limit=6000)
    assert "GadgetStore logo" in out and "743,00 €" in out
    assert "gadgetstore.jpg" not in out


def test_a_shop_page_about_biscuits_keeps_its_products():
    """"cookie" is a biscuit as well as a cookie banner's word: as a banner word it emptied a
    shop's results page for cookies down to bare prices."""
    page = "\n".join(["We use cookies to make the site work",
                      "[Chocolate Cookies 180 g](https://shop.example/p/1)", "1,29 €",
                      "[Oatmeal Cookies 300 g](https://shop.example/p/2)", "2,15 €"])
    out = relevant(page, "cookies", limit=6000)
    assert "Chocolate Cookies 180 g" in out and "Oatmeal Cookies 300 g" in out
    assert "We use" not in out  # the banner itself still goes


LISTING = "\n".join([
    *[f"[Menu {n}](https://www.homes.example/m{n})" for n in range(80)],     # navigation
    "Price", "–", "Year built",                                              # a search form
    "# Elm Street 3, Riverside, Springfield",
    "Home loan offered by Example Bank. Loan amount 133 000 €; contract fee 190 €; "
    + "conditions " * 90,                                                    # a loan disclaimer
    *[f"[Harbour Road {n} _168 900€_ Flat for sale, 4 rooms](https://www.homes.example/o{n})"
      for n in range(30)],                                                   # other listings
    " 174 900€  2 695 €/m² ",
    "| Flat for sale |", "| --- |",
    "| Rooms | 4 |", "| Total area | 64.9 m² |", "| Floor/Floors | 3/9 |",
    "| Year built | 1979 |", "| [Energy class](https://energy.example/class) | D |",
    "The flat is on the third storey of a nine-storey building and its windows face both "
    "sides of the house.",
    *[f"Other listing {n}" for n in range(200)],
])


def test_a_linked_page_keeps_its_facts_table_price_and_description():
    """The page a link points to is the subject: its facts come first, wherever they sit, and
    menus and other listings go. The question is in Russian and shares no word with the page."""
    out = page_facts(LISTING, "сколько стоит и на каком этаже?", limit=3000)
    assert len(out) <= 3000
    for fact in ("174 900€", "| Rooms | 4 |", "| Floor/Floors | 3/9 |", "| Year built | 1979 |",
                 "Energy class", "third storey", "# Elm Street 3"):
        assert fact in out, fact
    assert "Menu 5" not in out and "Harbour Road 3 " not in out and "| --- |" not in out


def test_a_linked_page_is_still_in_page_order():
    out = page_facts(LISTING, "", limit=3000)
    assert out.index("Elm Street") < out.index("174 900€") < out.index("Rooms")


def test_a_short_linked_page_comes_back_whole():
    page = "# Title\nThe first paragraph, about the matter.\nThe second paragraph."
    assert page_facts(page, "что тут", limit=3000) == page
