"""tools/google_login.py: the link, the pasted address, and the stored sign-in."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tools.google_login import REDIRECT, SCOPES, code_from, exchange, pkce, sign_in_link

CLIENT = {"client_id": "cid", "client_secret": "sec", "token_uri": "https://t.example/token"}


def test_the_link_asks_for_read_only_access_with_pkce():
    verifier, challenge = pkce()
    query = parse_qs(urlparse(sign_in_link(CLIENT, "st", challenge)).query)
    assert query["redirect_uri"] == [REDIRECT] and query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"] and query["scope"] == [" ".join(SCOPES)]
    assert all(s.endswith("readonly") for s in SCOPES) and verifier != challenge


def test_the_pasted_address_gives_its_code_and_only_for_this_attempt():
    good = f"{REDIRECT}?state=st&iss=https://accounts.google.com&code=4/abc&scope=x"
    assert code_from(good, "st") == "4/abc"
    with pytest.raises(ValueError):
        code_from(good, "other")
    with pytest.raises(ValueError):
        code_from(f"{REDIRECT}?state=st&error=access_denied", "st")


def test_the_code_becomes_the_file_the_bot_reads():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r",
                                         "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        token = exchange(CLIENT, "4/abc", "verifier", http)
    assert seen["code_verifier"] == ["verifier"] and seen["code"] == ["4/abc"]
    assert token["refresh_token"] == "r" and token["client_id"] == "cid"
    assert token["token_uri"] == "https://t.example/token"
