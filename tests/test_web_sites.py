import json

import pytest

from app.web.sites import RENT, Site, SiteError, load, search_url, searchable

SITES = (
    Site("shop.example", "groceries", "https://shop.example/search?q={q}", probe="milk"),
    Site("homes.example", "real_estate", "https://homes.example/find?deal=sale&kw={q}",
         rent_template="https://homes.example/find?deal=rent&kw={q}", probe="Oak Lane"),
    Site("prices.example", "prices", "https://prices.example/q/{q}", probe="phone"),
    Site("market.example", "goods"),
)


def test_only_sites_with_a_template_are_searchable():
    assert [s.name for s in searchable(SITES)] == [
        "shop.example", "homes.example", "prices.example"]


def test_the_query_is_quoted_with_foreign_letters_and_spaces():
    assert search_url(SITES, "homes.example", "Élm  Straße 3") == (
        "https://homes.example/find?deal=sale&kw=%C3%89lm%20Stra%C3%9Fe%203")


def test_rent_uses_the_rent_search():
    assert search_url(SITES, "homes.example", "Oak Lane", RENT) == (
        "https://homes.example/find?deal=rent&kw=Oak%20Lane")


def test_errors_say_what_is_wrong_in_words_the_model_can_act_on():
    with pytest.raises(SiteError, match="unknown site"):
        search_url(SITES, "tools.example", "plank")
    with pytest.raises(SiteError, match="no rent search"):
        search_url(SITES, "shop.example", "milk", RENT)
    with pytest.raises(SiteError, match="empty"):
        search_url(SITES, "shop.example", "   ")


def test_a_known_only_site_has_no_direct_search():
    with pytest.raises(SiteError, match="unknown site"):
        search_url(SITES, "market.example", "bike")


def test_no_sites_means_no_searches():
    with pytest.raises(SiteError, match="unknown site"):
        search_url((), "shop.example", "milk")


def test_load_reads_the_owners_table(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text(json.dumps([
        {"name": "shop.example", "kind": "groceries",
         "template": "https://shop.example/search?q={q}", "probe": "milk"},
        {"name": " homes.example ", "kind": "real_estate",
         "template": "https://homes.example/find?deal=sale&kw={q}",
         "rent_template": "https://homes.example/find?deal=rent&kw={q}"},
        {"name": "market.example", "kind": "goods"},
    ]), encoding="utf-8")
    sites = load(path)
    assert [s.name for s in sites] == ["shop.example", "homes.example", "market.example"]
    assert sites[0] == Site("shop.example", "groceries",
                            "https://shop.example/search?q={q}", "", "milk")
    assert sites[1].rent_template == "https://homes.example/find?deal=rent&kw={q}"
    assert sites[2].template == ""


def test_load_without_a_file_is_no_sites(tmp_path):
    assert load(tmp_path / "missing.json") == ()


def test_load_with_bad_json_is_no_sites(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text("[{not json", encoding="utf-8")
    assert load(path) == ()
    path.write_text('{"name": "shop.example"}', encoding="utf-8")
    assert load(path) == ()


def test_load_skips_entries_without_a_name_or_with_a_template_without_q(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text(json.dumps([
        {"kind": "groceries", "template": "https://nameless.example/?q={q}"},
        {"name": "  ", "template": "https://blank.example/?q={q}"},
        {"name": "broken.example", "template": "https://broken.example/search"},
        {"name": "half.example", "template": "https://half.example/?q={q}",
         "rent_template": "https://half.example/rent"},
        "not a dict",
        {"name": "shop.example", "template": "https://shop.example/search?q={q}"},
    ]), encoding="utf-8")
    assert [s.name for s in load(path)] == ["shop.example"]
