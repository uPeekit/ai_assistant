"""The owner's Google sign-in, kept fresh: an access token from the stored refresh token.

`data/google_token.json` is the "authorized user" file the sign-in writes (tools/google_login.py):
client id and secret, the refresh token, and the last access token with its expiry. An access
token lasts an hour; a fresh one is asked for a minute before it runs out. A refresh token
Google no longer accepts (revoked, or the app was put back into testing) raises SignInNeeded:
the owner has to sign in again, and nothing else can fix that."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.google.errors import GoogleError, SignInNeeded

log = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"
EARLY = timedelta(minutes=1)
__all__ = ["GoogleAuth", "SignInNeeded", "GoogleError"]


class GoogleAuth:
    def __init__(self, path: Path, *, http: httpx.AsyncClient | None = None,
                 now=lambda: datetime.now(UTC)) -> None:
        self._path = path
        self._http = http
        self._now = now
        self._token: str = ""
        self._expiry: datetime | None = None
        self._lock = asyncio.Lock()
        # When Google last gave a token: a "sign in again" warning older than this is over.
        self.last_ok: datetime | None = None
        self._restore()

    def _restore(self) -> None:
        """The access token the file holds, while it lasts: a restart need not ask again."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            expiry = datetime.strptime(data["expiry"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=UTC)
            if data.get("token"):
                self._token, self._expiry = data["token"], expiry
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _stored(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SignInNeeded("no Google sign-in stored") from None
        if not data.get("refresh_token") or not data.get("client_id"):
            raise SignInNeeded("the stored Google sign-in has no refresh token")
        return data

    @property
    def signed_in(self) -> bool:
        try:
            self._stored()
        except SignInNeeded:
            return False
        return True

    async def token(self, *, refused: str = "") -> str:
        """A valid access token. `refused` is a token Google just answered 401 to: a new one
        is asked for unless another request already got one meanwhile."""
        async with self._lock:
            usable = self._token and self._expiry and self._now() < self._expiry - EARLY
            if usable and self._token != refused:
                return self._token
            data = self._stored()
            http = self._http or httpx.AsyncClient(timeout=15.0)
            try:
                resp = await http.post(data.get("token_uri") or TOKEN_URI, data={
                    "grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                    "client_id": data["client_id"],
                    "client_secret": data.get("client_secret", "")})
            except httpx.HTTPError as e:  # no network yet (a bot started before it): later
                raise GoogleError(type(e).__name__) from None
            finally:
                if self._http is None:
                    await http.aclose()
            if resp.status_code in (400, 401):
                # invalid_grant: revoked, expired (an app left in testing), or a changed secret.
                raise SignInNeeded(f"Google refused the sign-in ({resp.status_code})")
            if resp.status_code >= 300:
                raise GoogleError(f"Google token service answered {resp.status_code}",
                                  resp.status_code)
            body = resp.json()
            self.last_ok = self._now()
            self._token = body["access_token"]
            self._expiry = self._now() + timedelta(seconds=int(body.get("expires_in", 3600)))
            self._save(data)
            return self._token

    def _save(self, data: dict) -> None:
        """The new access token kept with the rest, so a restart does not ask for one at once."""
        try:
            data = {**data, "token": self._token,
                    "expiry": self._expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.info("could not store the Google access token (%s)", type(e).__name__)
