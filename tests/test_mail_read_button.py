"""«Отметить прочитанными» under a mail digest: it marks exactly the mail that digest covered,
and the button turns into one that marks it unread again."""

from __future__ import annotations

import json

import pytest

from app import texts
from app.mail.classify import Sorted
from app.mail.imap import GmailIMAP, MailboxError
from app.mail.marks import DigestMarks
from app.mail.service import MailRun, MailService, MailState
from tests.test_mail import BUCKETS, message
from tests.test_mail_local import local

# ---- the mailbox -------------------------------------------------------------------------------


class WritableBox:
    """imaplib enough for set_seen: records how the folder was opened and what was stored."""

    def __init__(self, validity: str = "7", fail: bool = False) -> None:
        self.validity, self.fail = validity, fail
        self.selected: list[bool] = []
        self.stored: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def status(self, folder, what):
        return "OK", [f"INBOX (UIDVALIDITY {self.validity})".encode()]

    def uid(self, command, *args):
        assert command == "STORE"
        self.stored.append(args)
        return ("NO", [b"denied"]) if self.fail else ("OK", [b""])


def imap(monkeypatch, box: WritableBox) -> GmailIMAP:
    client = GmailIMAP("me@x.ee", "pw")

    def opened(readonly: bool = True):
        box.selected.append(readonly)
        return box

    monkeypatch.setattr(client, "_open", opened)
    return client


def test_marking_read_opens_the_mailbox_writable_and_sets_the_seen_flag(monkeypatch):
    box = WritableBox()
    assert imap(monkeypatch, box).set_seen(["10", "12"], "7", seen=True) == 2
    assert box.selected == [False]  # the one place the mailbox is opened read-write
    assert box.stored == [("10,12", "+FLAGS", "(\\Seen)")]

    imap(monkeypatch, box).set_seen(["10"], "7", seen=False)
    assert box.stored[-1] == ("10", "-FLAGS", "(\\Seen)")


def test_a_renumbered_mailbox_is_never_written_to(monkeypatch):
    """Gmail renumbered everything since the digest: those uids now name other letters."""
    box = WritableBox(validity="8")
    with pytest.raises(MailboxError):
        imap(monkeypatch, box).set_seen(["10"], "7")
    assert box.stored == []


def test_a_refused_store_is_an_error(monkeypatch):
    with pytest.raises(MailboxError):
        imap(monkeypatch, WritableBox(fail=True)).set_seen(["10"], "7")


# ---- remembering what each digest covered ------------------------------------------------------

def test_a_digest_is_remembered_by_a_short_id_and_old_ones_are_dropped(tmp_path):
    clock = [1_000_000.0]
    marks = DigestMarks(tmp_path / "mail_digests.json", clock=lambda: clock[0])
    first = marks.add(["10", "11"], "7")
    assert marks.get(first) == (["10", "11"], "7")
    assert len(first) <= 12 and ":" not in first  # fits Telegram's 64-byte callback data

    clock[0] += 15 * 24 * 3600
    second = marks.add(["12"], "7")
    assert marks.get(first) is None and marks.get(second) == (["12"], "7")
    assert json.loads((tmp_path / "mail_digests.json").read_text(encoding="utf-8"))


# ---- the service -------------------------------------------------------------------------------

class Box:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail

    def set_seen(self, uids, validity, seen=True):
        self.calls.append((uids, validity, seen))
        if self.fail:
            raise self.fail
        return len(uids)


def svc(tmp_path, box) -> MailService:
    return MailService(box, local({"messages": []}), MailState(tmp_path / "state.json"),
                       buckets=BUCKETS, marks=DigestMarks(tmp_path / "marks.json"))


def sorted_run(*uids: str) -> MailRun:
    return MailRun(sorted=[Sorted(message(u), "bills", "x") for u in uids], validity="7")


async def test_the_button_marks_every_letter_of_its_own_digest_and_nothing_else(tmp_path):
    box = Box()
    service = svc(tmp_path, box)
    noon = service.remember(sorted_run("10", "11", "12"))
    evening = service.remember(sorted_run("20"))

    ok, said = await service.mark(noon, seen=True)

    assert ok and said == texts.MAIL_MARKED.format(n=3)
    assert box.calls == [(["10", "11", "12"], "7", True)]  # not the evening's letter

    ok, said = await service.mark(evening, seen=False)
    assert ok and said == texts.MAIL_UNMARKED.format(n=1)
    assert box.calls[-1] == (["20"], "7", False)


async def test_nothing_to_remember_and_a_forgotten_digest_are_said_plainly(tmp_path):
    box = Box()
    service = svc(tmp_path, box)
    assert service.remember(MailRun()) is None  # no mail, no button

    ok, said = await service.mark("deadbeef", seen=True)
    assert not ok and said == texts.MAIL_MARK_GONE and box.calls == []


async def test_a_mailbox_that_refuses_says_why(tmp_path):
    service = svc(tmp_path, Box(fail=MailboxError("store failed: NO")))
    digest_id = service.remember(sorted_run("10"))

    ok, said = await service.mark(digest_id, seen=True)

    assert not ok and said == texts.MAIL_MARK_FAILED.format(error="store failed: NO")


# ---- the message and the button ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_real_digest_carries_the_button_and_the_comparisons_do_not(
        env, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app import main
    from app.config import Settings
    from app.llm.health import Health
    from app.mail.buckets import Buckets
    from tests.test_mail import FakeMailbox

    env.setenv("GMAIL_ADDRESS", "me@example.com")
    env.setenv("GMAIL_APP_PASSWORD", "pw")
    env.setenv("ANTHROPIC_API_KEY", "")
    env.setenv("MAIL_MODEL", "qwen3:8b")
    env.setenv("MAIL_SHADOW_MODEL", "other")
    sent: list[tuple[str, object]] = []

    class FakeLocal:
        def __init__(self, base_url, model, buckets, **kw) -> None:
            self.model, self.buckets, self.meanings = model, buckets, {}

        async def sort(self, messages):
            return [Sorted(m, "bills", "счёт") for m in messages], 0, 0

        async def aclose(self) -> None:
            pass

    async def send_message(chat_id, body, reply_markup=None, **kw):
        sent.append((body, reply_markup))

    monkeypatch.setattr(main, "GmailIMAP", lambda *a: FakeMailbox([([message("10")], "1")]))
    monkeypatch.setattr(main, "LocalClassifier", FakeLocal)
    settings = Settings(_env_file=None, db_path=tmp_path / "bot.sqlite")
    service, daily = main._mail_digest(
        settings, SimpleNamespace(get=lambda name: True),
        Buckets(tmp_path / "b.txt", "bills: pay\nother: rest"),
        SimpleNamespace(mail_at="12:00"), Health(),
        lambda: SimpleNamespace(bot=SimpleNamespace(send_message=send_message)))
    await daily._send()

    real = [markup for body, markup in sent if body.startswith("📬")]
    others = [markup for body, markup in sent if not body.startswith("📬")]
    assert real and all(m is not None for m in real) and all(m is None for m in others)
    button = real[0].inline_keyboard[0][0]
    assert button.text == texts.BTN_MAIL_READ and button.callback_data.startswith("mr:")
    assert service.marks.get(button.callback_data[3:]) == (["10"], "1")


# ---- pressing it -------------------------------------------------------------------------------

async def test_pressing_marks_the_digest_says_so_and_offers_the_way_back(tmp_path):
    from tests.test_handlers import ALLOWED_USER, build, callback_update, dispatch

    hs = build()
    box = Box()
    service = svc(tmp_path, box)
    digest_id = service.remember(sorted_run("10", "11"))
    hs.app.bot_data["mail"] = service

    assert await dispatch(hs.app, callback_update(ALLOWED_USER, f"mr:{digest_id}", hs.bot),
                          hs.context)

    assert box.calls == [(["10", "11"], "7", True)]
    assert hs.orch.calls == []  # mail buttons never reach the conversation
    assert hs.bot.answered == ["cbq-1"]
    back = hs.bot.cleared[-1][2].inline_keyboard[0][0]
    assert back.text == texts.BTN_MAIL_UNREAD and back.callback_data == f"mu:{digest_id}"


async def test_a_stranger_pressing_the_button_changes_nothing(tmp_path):
    from tests.test_handlers import DENIED_USER, build, callback_update, dispatch

    hs = build()
    box = Box()
    service = svc(tmp_path, box)
    hs.app.bot_data["mail"] = service
    digest_id = service.remember(sorted_run("10"))

    await dispatch(hs.app, callback_update(DENIED_USER, f"mr:{digest_id}", hs.bot), hs.context)

    assert box.calls == [] and hs.bot.answered == []
