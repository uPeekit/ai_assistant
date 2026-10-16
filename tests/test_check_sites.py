from tools.check_sites import works


def test_a_results_page_naming_the_query_with_prices_works():
    page = "Milk 1 l 1,39 €\nMilk 2 l 2,49 €\nMilkshake 3,10 €"
    assert works(page, "milk") == (True, 3, 3)


def test_a_page_without_prices_or_without_the_query_fails():
    assert not works("milk milk milk", "milk")[0]
    assert not works("1,00 € 2,00 € 3,00 €", "milk")[0]


def test_the_probe_matches_in_other_forms():
    assert works("Gardening 1 €\nGardens 2 €\nGarden tools 3 €", "Garden")[0]
