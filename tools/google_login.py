"""Sign the bot in to Google (read-only), once, or again when the sign-in stopped working.

    python -m tools.google_login

Prints a Google link. Open it, sign in, allow the read-only access. On the bot's own computer
the browser comes back to this tool by itself; from another machine the last page fails to
load — copy its whole address (http://localhost:8790/?state=…&code=…) and paste it here. The
sign-in is stored next to the database as data/google_token.json, beside the client key
data/google_client.json (an OAuth client of type "Desktop app")."""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import secrets
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

PORT = 8790
REDIRECT = f"http://localhost:{PORT}/"
SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/forms.body.readonly",
    "https://www.googleapis.com/auth/forms.responses.readonly",
)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.decode().rstrip("=")


def sign_in_link(client: dict, state: str, challenge: str) -> str:
    return client.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") + "?" + urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
        "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})


def code_from(address: str, state: str) -> str:
    """The code in the address Google sent the browser to; refuses another sign-in's."""
    query = parse_qs(urlparse(address.strip()).query)
    if query.get("state", [""])[0] != state:
        raise ValueError("this address belongs to another sign-in attempt")
    if "error" in query:
        raise ValueError(f"Google said: {query['error'][0]}")
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("no code in this address")
    return code


def exchange(client: dict, code: str, verifier: str, http: httpx.Client) -> dict:
    """The tokens for the code, as the file the bot reads (app/google/auth.py)."""
    token_uri = client.get("token_uri", "https://oauth2.googleapis.com/token")
    resp = http.post(token_uri, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "code_verifier": verifier})
    if resp.status_code >= 300:
        try:
            said = resp.json().get("error_description") or resp.json().get("error")
        except ValueError:
            said = resp.text[:200]
        raise ValueError(f"Google refused the code ({resp.status_code}): {said}")
    body = resp.json()
    if not body.get("refresh_token"):
        raise ValueError("Google gave no refresh token; remove the app's access in your "
                         "Google account settings and sign in again")
    expiry = datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 3600)))
    return {"token": body["access_token"], "refresh_token": body["refresh_token"],
            "token_uri": token_uri, "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""), "scopes": list(SCOPES),
            "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "account": ""}


def _local_server(found: queue.Queue) -> HTTPServer | None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            found.put(f"http://localhost:{PORT}{self.path}")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("localhost", PORT), Handler)
    except OSError:
        return None  # the port is taken: pasting the address still works
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    from app.config import load_settings

    sys.stdout.reconfigure(encoding="utf-8")
    data = load_settings().db_path.parent
    client_file, token_file = data / "google_client.json", data / "google_token.json"
    try:
        raw = json.loads(client_file.read_text(encoding="utf-8"))
        client = raw.get("installed") or raw.get("web") or raw
    except (OSError, ValueError):
        print(f"No client key at {client_file}: create an OAuth client of type 'Desktop app' "
              "in Google Cloud, download its JSON and save it there.")
        return 2
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(16)
    found: queue.Queue = queue.Queue()
    server = _local_server(found)
    print("Open this link and sign in:\n\n" + sign_in_link(client, state, challenge) + "\n")
    print("On another machine: paste here the whole address the browser ends up on "
          f"(it starts with {REDIRECT}?state=) and press Enter.")
    threading.Thread(target=lambda: found.put(sys.stdin.readline()), daemon=True).start()
    try:
        while True:
            address = found.get()
            try:
                code = code_from(address, state)
                break
            except ValueError as e:
                print(f"Not that one: {e}. Try again.")
                threading.Thread(target=lambda: found.put(sys.stdin.readline()),
                                 daemon=True).start()
        with httpx.Client(timeout=30.0) as http:
            try:
                token = exchange(client, code, verifier, http)
            except ValueError as e:
                print(e)
                return 1
    finally:
        if server is not None:
            server.shutdown()
    tmp = Path(token_file).with_suffix(".tmp")
    tmp.write_text(json.dumps(token), encoding="utf-8")
    tmp.replace(token_file)
    print(f"Signed in. Stored in {token_file}. Restart the bot to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
