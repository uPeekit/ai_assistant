from tools.check_sites import works


def test_a_results_page_naming_the_query_with_prices_works():
    page = "Piim 1 l 1,39 €\nPiim 2 l 2,49 €\nPiimapulber 3,10 €"
    assert works(page, "piim") == (True, 3, 3)


def test_a_page_without_prices_or_without_the_query_fails():
    assert not works("piim piim piim", "piim")[0]
    assert not works("1,00 € 2,00 € 3,00 €", "piim")[0]


def test_the_probe_matches_in_other_forms():
    assert works("Lasnamäel 1 €\nLasnamäe 2 €\nLasnamäe linnaosa 3 €", "Lasnamäe")[0]
