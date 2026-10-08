"""Reading the mailbox over IMAP, without changing anything in it.

Gmail with an app password: no OAuth, no consent screen, no token that expires in a week. Every
fetch uses BODY.PEEK, so reading a message here never marks it read in Gmail — the whole point
of the read-only phase.

Nothing in this module writes to the mailbox. There is no delete, no send, no flag change: the
operations simply do not exist, which is a stronger guarantee than a scope."""

from __future__ import annotations

import email
import imaplib
import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message as RawMessage
from email.utils import parsedate_to_datetime

from app import texts

log = logging.getLogger(__name__)

HOST = "imap.gmail.com"
PORT = 993
FOLDER = "INBOX"
MAX_BODY = 1200
MAX_FETCH = 60
TIMEOUT_S = 30
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\xa0]+")
_MARKERS = "|".join(texts.MAIL_QUOTE_MARKERS)
_QUOTED = re.compile(rf"^\s*(>|On .*wrote:|\d{{1,2}}\.\d{{1,2}}\.\d{{4}}.*({_MARKERS}))", re.M)


@dataclass(frozen=True)
class Message:
    """One message, flattened to what a classifier needs and nothing more."""

    uid: str
    sender: str
    subject: str
    received: datetime | None
    body: str
    bulk: bool  # carries List-Unsubscribe: a newsletter, a receipt, a notification

    @property
    def short_sender(self) -> str:
        """"Elektrilevi" out of "Elektrilevi <no-reply@elektrilevi.ee>"."""
        name = self.sender.split("<")[0].strip().strip('"')
        return name or self.sender.split("<")[-1].strip(">").strip()


def _text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


def _body(raw: RawMessage) -> str:
    """The plain-text part, or the HTML one with its tags removed. Quoted history is cut: it is
    the same text again, and it crowds out what the message itself says."""
    chosen, html = "", ""
    for part in raw.walk() if raw.is_multipart() else [raw]:
        if part.get_content_maintype() != "text":
            continue
        try:
            payload = part.get_payload(decode=True)
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, ValueError, AttributeError):
            continue
        if part.get_content_subtype() == "plain" and not chosen.strip():
            chosen = text
        elif part.get_content_subtype() == "html" and not html.strip():
            html = text
    # A plain part holding only a newline is what many senders put next to the real HTML body,
    # so "is there a plain part" is not the question — "does it say anything" is.
    text = chosen if chosen.strip() else _TAGS.sub(" ", html)
    cut = _QUOTED.search(text)
    if cut:
        text = text[:cut.start()]
    text = _SPACES.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:MAX_BODY]


def parse(uid: str, raw_bytes: bytes) -> Message:
    raw = email.message_from_bytes(raw_bytes)
    try:
        received = parsedate_to_datetime(raw.get("Date", ""))
    except (TypeError, ValueError):
        received = None
    return Message(
        uid=uid,
        sender=_text(raw.get("From")),
        subject=_text(raw.get("Subject")),
        received=received,
        body=_body(raw),
        bulk=bool(raw.get("List-Unsubscribe") or raw.get("List-Id")
                  or (raw.get("Precedence", "").lower() in ("bulk", "list"))),
    )


class MailboxError(Exception):
    pass


class GmailIMAP:
    """Read-only Gmail over IMAP. One connection per run: a long-lived IMAP session drops
    silently, and a run is a handful of seconds."""

    def __init__(self, address: str, app_password: str, *, host: str = HOST,
                 port: int = PORT) -> None:
        self._address = address
        self._password = app_password
        self._host = host
        self._port = port

    def fetch_since(self, uid_after: str | None, hours: int = 12,
                    limit: int = MAX_FETCH) -> tuple[list[Message], str | None, str]:
        """Messages newer than `uid_after` (or the last `hours` on a first run), the newest uid
        seen, and the mailbox's UIDVALIDITY — which invalidates stored uids when it changes."""
        try:
            with self._open() as box:
                validity = self._validity(box)
                if uid_after:
                    criteria = ["UID", f"{int(uid_after) + 1}:*"]
                else:
                    since = (datetime.now(UTC) - timedelta(hours=hours))
                    criteria = ["SINCE", since.strftime("%d-%b-%Y")]
                uids = self._search(box, criteria)
                # A "UID n:*" search always returns at least the last message, even when it is
                # older than n — the server clamps the range rather than answering nothing.
                if uid_after:
                    uids = [u for u in uids if int(u) > int(uid_after)]
                newest = uids[-1] if uids else uid_after
                # Only what is still unread goes in the digest: mail the user has already opened
                # on the phone is mail they have seen. `newest` still moves past it, so it is not
                # picked up again once it is older than the next run.
                unread = set(self._search(box, [*criteria, "UNSEEN"])) if uids else set()
                uids = [u for u in uids if u in unread]
                if len(uids) > limit:
                    # More than one run holds: the oldest go now and the bookmark stops at the
                    # last of them, so the rest are the next run's. Taking the newest and
                    # moving the bookmark past the whole lot left the others out of every
                    # digest, with nothing to say so.
                    log.info("mail: %d unread, %d taken now, the rest next run",
                             len(uids), limit)
                    uids, newest = uids[:limit], uids[limit - 1]
                messages = [self._one(box, uid) for uid in uids]
                return [m for m in messages if m is not None], newest, validity
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailboxError(f"{type(e).__name__}: {e}") from None

    # ---- plumbing --------------------------------------------------------------------

    def set_seen(self, uids: list[str], validity: str, seen: bool = True) -> int:
        """Mark these letters read (or unread again); returns how many were asked for.

        The one write this module makes, and only when the user presses the digest's
        button. Refuses when the mailbox was renumbered since the digest (UIDVALIDITY
        changed): the same numbers would now name other letters."""
        if not uids:
            return 0
        try:
            with self._open(readonly=False) as box:
                now = self._validity(box)
                if validity and now and now != validity:
                    raise MailboxError("the mailbox was renumbered since that digest")
                flag = "+FLAGS" if seen else "-FLAGS"
                ok, _ = box.uid("STORE", ",".join(uids), flag, "(\\Seen)")
                if ok != "OK":
                    raise MailboxError(f"store failed: {ok}")
                return len(uids)
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailboxError(f"{type(e).__name__}: {e}") from None

    def _open(self, readonly: bool = True) -> imaplib.IMAP4_SSL:
        box = imaplib.IMAP4_SSL(self._host, self._port, timeout=TIMEOUT_S)
        try:
            box.login(self._address, self._password)
            # Read-only unless a flag is being set on the user's say-so (set_seen): a
            # read-only select is what keeps reading the mail from ever changing it.
            box.select(FOLDER, readonly=readonly)
        except Exception:
            with suppress(Exception):
                box.logout()
            raise
        return box

    @staticmethod
    def _search(box: imaplib.IMAP4_SSL, criteria: list[str]) -> list[str]:
        ok, data = box.uid("SEARCH", None, *criteria)
        if ok != "OK":
            raise MailboxError(f"search failed: {ok}")
        return [u.decode() for u in (data[0] or b"").split()]

    @staticmethod
    def _validity(box: imaplib.IMAP4_SSL) -> str:
        ok, data = box.status(FOLDER, "(UIDVALIDITY)")
        if ok != "OK" or not data:
            return ""
        found = re.search(rb"UIDVALIDITY (\d+)", data[0] or b"")
        return found.group(1).decode() if found else ""

    @staticmethod
    def _one(box: imaplib.IMAP4_SSL, uid: str) -> Message | None:
        # PEEK, so fetching does not mark the message as read.
        ok, data = box.uid("FETCH", uid, "(BODY.PEEK[])")
        if ok != "OK" or not data or not isinstance(data[0], tuple):
            log.warning("could not fetch one message; it is left out of this digest")
            return None
        return parse(uid, data[0][1])
