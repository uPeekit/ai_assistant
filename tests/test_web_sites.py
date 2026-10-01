import pytest

from app.web.sites import BY_NAME, RENT, SITES, SiteError, search_url


def test_every_site_has_a_template_a_probe_and_a_unique_name():
    assert len(BY_NAME) == len(SITES)
    for s in SITES:
        assert s.template.startswith("https://") and "{q}" in s.template
        assert s.probe
        if s.rent_template:
            assert "{q}" in s.rent_template


def test_the_query_is_quoted_with_estonian_letters_and_spaces():
    assert search_url("kv.ee", "Kalevipoja  põik 3") == (
        "https://www.kv.ee/search?deal_type=1&keyword=Kalevipoja%20p%C3%B5ik%203")


def test_rent_uses_the_rent_search():
    assert search_url("kv.ee", "Lasnamäe", RENT) == (
        "https://www.kv.ee/search?deal_type=2&keyword=Lasnam%C3%A4e")


def test_errors_say_what_is_wrong_in_words_the_model_can_act_on():
    with pytest.raises(SiteError, match="unknown site"):
        search_url("bauhof.ee", "liimpuit")
    with pytest.raises(SiteError, match="no rent search"):
        search_url("rimi.ee", "piim", RENT)
    with pytest.raises(SiteError, match="empty"):
        search_url("rimi.ee", "   ")
