"""Reminder rules from the admin page: remind N days before dated things in the vault.

    {"id": "a1b2", "kind": "notes", "folder": "Club", "tag": "", "date_prop": "date",
     "days_before": 1, "time": "09:00", "chat_id": -100123, "created_at": "..."}

`notes` watches notes (in `folder`, and/or carrying `tag`) with a date property; `tasks`
watches open tasks carrying `#tag` with a due date. The items are the morning agenda's
(app/vault/agenda.py), so a date is read the same way everywhere. Each item fires once per due
moment (`reminder_fired`), so a restart never repeats one and a moved meeting fires again at
its new time. A rule fires only for moments after it was created, and never for an item whose
date has passed. A rule for a group reads the vault as that group's scope sees it."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import texts
from app.reminders.scheduler import LATE, late_by
from app.reminders.store import ReminderStore
from app.vault import agenda
from app.vault.agenda import Item
from app.vault.index import VaultIndex

log = logging.getLogger(__name__)

KINDS = ("notes", "tasks")
MAX_DAYS = 60
DEFAULT_AT = time(9, 0)


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    chat_id: int
    created_at: datetime
    folder: str = ""
    tag: str = ""
    date_prop: str = ""
    days_before: int = 1
    at: time = DEFAULT_AT


def _at(value: object) -> time:
    try:
        hour, minute = (int(p) for p in str(value).strip().split(":", 1))
        return time(hour, minute)
    except (ValueError, TypeError):
        return DEFAULT_AT


def _rule(raw: dict, now: datetime) -> Rule:
    kind = str(raw.get("kind", "")).strip()
    if kind not in KINDS:
        raise ValueError(f"rule kind must be one of {KINDS}")
    try:
        chat_id = int(raw["chat_id"])
        days = int(raw.get("days_before", 1))
    except (KeyError, TypeError, ValueError):
        raise ValueError("a rule needs a numeric chat_id and days_before") from None
    if not 0 <= days <= MAX_DAYS:
        raise ValueError(f"days_before must be 0..{MAX_DAYS}")
    folder = str(raw.get("folder", "")).strip().strip("/")
    tag = str(raw.get("tag", "")).strip().lstrip("#")
    if kind == "tasks" and not tag:
        raise ValueError("a task rule needs a tag")
    if kind == "notes" and not (folder or tag):
        raise ValueError("a note rule needs a folder or a tag")
    try:
        created = datetime.fromisoformat(str(raw.get("created_at", "")))
        if created.tzinfo is None:
            raise ValueError
    except ValueError:
        created = now
    return Rule(id=str(raw.get("id") or secrets.token_hex(4)), kind=kind, chat_id=chat_id,
                created_at=created, folder=folder, tag=tag,
                date_prop=str(raw.get("date_prop", "")).strip(), days_before=days,
                at=_at(raw.get("time", "09:00")))


def _dump(rule: Rule) -> dict:
    return {"id": rule.id, "kind": rule.kind, "chat_id": rule.chat_id,
            "folder": rule.folder, "tag": rule.tag, "date_prop": rule.date_prop,
            "days_before": rule.days_before, "time": rule.at.strftime("%H:%M"),
            "created_at": rule.created_at.isoformat()}


class ReminderRules:
    """data/reminder_rules.json, re-read by mtime. Unreadable: no rules, said once."""

    def __init__(self, path: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self._path = path
        self._now = now or (lambda: datetime.now(UTC))
        self._rules: list[Rule] = []
        self._mtime: float | None = None
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            try:
                stat = self._path.stat()
            except OSError:
                self._rules, self._mtime = [], None
                return
            if self._mtime == stat.st_mtime:
                return
            self._mtime = stat.st_mtime
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError("not a list")
                self._rules = [_rule(r, self._now()) for r in raw if isinstance(r, dict)]
            except (OSError, ValueError) as e:
                log.warning("could not read %s (%s); no reminder rules run", self._path, e)
                self._rules = []

    def all(self) -> list[Rule]:
        self.reload()
        return list(self._rules)

    def dump(self) -> list[dict]:
        return [_dump(r) for r in self.all()]

    def save(self, payload: object) -> int:
        """The rules the page posted; a rule keeps its id and creation time, a new one gets
        both now. Returns how many changed; ValueError for a malformed list."""
        if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
            raise ValueError("'reminder_rules' must be a list of objects")
        now = self._now()
        old = {r.id: r for r in self.all()}
        wanted: list[Rule] = []
        for raw in payload:
            known = old.get(str(raw.get("id", "")))
            rule = _rule({k: v for k, v in raw.items() if k != "created_at"}, now)
            if known is not None and _same_watch(known, rule):
                # Only the chat changed, or nothing: it keeps its start. Anything else is a
                # new rule from now — widening one must not send a burst of past reminders.
                rule = Rule(**{**rule.__dict__, "created_at": known.created_at})
            wanted.append(rule)
        changed = sum(1 for r in wanted if old.get(r.id) != r) + len(
            set(old) - {r.id for r in wanted})
        if changed:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_text(json.dumps([_dump(r) for r in wanted], ensure_ascii=False,
                                          indent=1), encoding="utf-8")
                tmp.replace(self._path)
                self._rules, self._mtime = wanted, self._path.stat().st_mtime
            log.info("reminder rules: %d (%d changed)", len(wanted), changed)
        return changed


def _same_watch(one: Rule, two: Rule) -> bool:
    return (one.kind, one.folder, one.tag, one.date_prop, one.days_before, one.at) == (
        two.kind, two.folder, two.tag, two.date_prop, two.days_before, two.at)


def _items(rule: Rule, index: VaultIndex, props: tuple[str, ...]) -> list[Item]:
    if rule.kind == "tasks":
        tag = rule.tag.casefold()
        return [i for i in agenda.open_tasks(index)
                if i.due and tag in {t.casefold() for t in i.tags}]
    out = []
    for item in agenda.dated_notes(index, (rule.date_prop,) if rule.date_prop else props):
        note = index.get(item.path)
        if note is None:
            continue
        if rule.folder and not item.path.startswith(f"{rule.folder}/"):
            continue
        if rule.tag and rule.tag.casefold() not in {t.casefold() for t in note.tags}:
            continue
        out.append(item)
    return out


def _key(rule: Rule, item: Item) -> str:
    # A task's words, not its line: a task moved to another day is the same task.
    return item.path if rule.kind == "notes" else f"{item.path}:{item.text}"


def fire_time(rule: Rule, item: Item, zone: ZoneInfo) -> datetime:
    day: date = item.due - timedelta(days=rule.days_before)
    at = rule.at
    if rule.kind == "tasks" and rule.days_before == 0 and item.time:
        at = _at(item.time)  # the task's own time, on its own day
    return datetime.combine(day, at, tzinfo=zone)


def message(item: Item, today: date) -> str:
    """How far the item really is from today — a late send says "today", not "tomorrow"."""
    days = (item.due - today).days
    if days <= 0:
        return texts.REMINDER_RULE_TODAY.format(label=item.label)
    if days == 1:
        return texts.REMINDER_RULE_TOMORROW.format(label=item.label)
    return texts.REMINDER_RULE_DAYS.format(n=days, label=item.label)


async def run_rules(now: datetime, rules: list[Rule], store: ReminderStore,
                    index_for: Callable[[int], VaultIndex | None],
                    props: tuple[str, ...], send: Callable[[int, str], Awaitable[object]],
                    tz: str) -> int:
    """One pass: send every rule's item whose moment has come and that was not sent yet."""
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    sent = 0
    for rule in rules:
        index = index_for(rule.chat_id)
        if index is None:
            continue
        for item in _items(rule, index, props):
            when = fire_time(rule, item, zone)
            if when > now or when < rule.created_at or item.due < today:
                continue
            key = _key(rule, item)
            if store.fired(rule.id, key, when):
                continue
            text = message(item, today)
            delay = now - when
            line = (texts.REMINDER_LATE.format(delay=late_by(delay), text=text)
                    if delay > LATE else texts.REMINDER.format(text=text))
            try:
                await send(rule.chat_id, line)
            except Exception as e:  # tried again on the next pass
                log.warning("rule %s not sent (%s)", rule.id, type(e).__name__)
                continue
            store.mark_fired(rule.id, key, when, now)
            sent += 1
    return sent
