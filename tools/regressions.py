"""Regression cases: real messages the bot once got wrong, run through the real pipeline.

    # once, and again whenever the vault or the Google sources should be taken afresh
    uv run python -m tools.regressions snapshot

    # every case, with the models .env names (or others), three times each
    uv run python -m tools.regressions run --runs 3 [--reader claude-haiku-4-5] \
        [--answerer claude-sonnet-5] [--only meetings] [--write data/eval/regressions/run.md]

Unlike tools/benchmark_filer.py, which scores the reader's first action on a frozen context,
a case here is a whole turn: the reader, the planner, the group's scope, the asker's names,
the Google sources and the answerer, on a copy of the vault — so a fix anywhere along the way
is held to. The vault and the raw Google answers are taken once (`snapshot`) and replayed:
the sources are cut into sections by the code under test, never by the code of the day the
snapshot was taken.

Everything lives under data/eval/regressions/ (git-ignored: it is the owner's notes and
documents). `cases.yaml` there holds the cases:

    - id: club-next-meetings
      text: when are the next club meetings?
      now: 2026-10-20T12:00            # the bot's clock for the turn
      chat: owner | <group chat id>    # the owner's private chat, or a group as configured
      user: 7                          # a group member's Telegram id (groups only)
      name: Olga                       # their Telegram name
      known: [olya]                    # the names they go by in the records, as told
      expect:
        contains: [first meeting, "9 November|09.11"]   # "a|b": either
        absent: [obsidian://]
        writes: [task, task]           # the kinds written, in any order
        reminders: 2
        lines_at_least: 10

    - id: report-still-running         # a watched folder's file: reported now, or waiting?
      kind: folder
      files: [2026-10-21_1300.md]      # under data/eval/regressions/files/
      expect: {reported: false}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.audit.store import AuditStore
from app.chats import ChatRef, Chats
from app.google.auth import GoogleAuth
from app.google.client import GoogleClient
from app.google.errors import GoogleError
from app.google.sources import GoogleSources, Sources
from app.reminders.store import ReminderStore
from app.vault.answer import VaultAnswerer
from app.vault.askers import Askers
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.planner import VaultPlanner
from app.vault.staged import StagedFiler, claude_ask
from app.vault.writer import VaultWriter
from app.watch.folders import Folders, FolderWatch

HOME = Path("data/eval/regressions")
OWNER_ID = 1  # the owner's private chat and user, as far as a case is concerned
# Not worth a copy: the vault's app settings, Syncthing's old versions, the trash, pictures.
SKIP_DIRS = {".obsidian", ".stversions", ".trash", ".git"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".mp4", ".mov", ".heic"}
METHODS = ("file", "children", "search", "document", "sheet_values", "form")


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore")
    anthropic_api_key: SecretStr = SecretStr("")
    filer_model: str = "claude-haiku-4-5"
    vault_answer_model: str = "claude-haiku-4-5"
    escalate_model: str = "claude-sonnet-5"
    timezone: str = "UTC"
    obsidian_vault: str = ""


# ---- Google, recorded once and replayed -----------------------------------------------------

def _key(method: str, args: tuple) -> str:
    return f"{method}:{json.dumps(args, ensure_ascii=False)}"


class _Auth:
    last_ok = None


class RecordingClient:
    """The real client, with every answer kept for the replay."""

    def __init__(self, client: GoogleClient) -> None:
        self._client = client
        self.auth = client.auth
        self.calls: dict[str, object] = {}

    def __getattr__(self, method: str):
        if method not in METHODS:
            raise AttributeError(method)

        async def call(*args):
            out = await getattr(self._client, method)(*args)
            self.calls[_key(method, args)] = out
            return out
        return call


class ReplayClient:
    """Google as it answered when the snapshot was taken. A question it was never asked
    fails as Google failing would: the copies there are stand, nothing is made up."""

    def __init__(self, calls: dict[str, object]) -> None:
        self._calls = calls
        self.auth = _Auth()

    def __getattr__(self, method: str):
        if method not in METHODS:
            raise AttributeError(method)

        async def call(*args):
            key = _key(method, args)
            if key not in self._calls:
                raise GoogleError("not in the snapshot", 503)
            return self._calls[key]
        return call


# ---- the checks -----------------------------------------------------------------------------

def check(expect: dict, reply: str, writes: list[str], reminders: int,
          reported: bool | None = None) -> list[str]:
    """What the turn got wrong against `expect`: nothing when it passed."""
    said = reply.casefold()
    misses = []
    for item in expect.get("contains") or []:
        if not any(alt.strip().casefold() in said for alt in str(item).split("|")):
            misses.append(f"lacks «{item}»")
    for item in expect.get("absent") or []:
        if str(item).casefold() in said:
            misses.append(f"has «{item}»")
    if "writes" in expect and sorted(expect["writes"] or []) != sorted(writes):
        misses.append(f"wrote {sorted(writes)}, not {sorted(expect['writes'] or [])}")
    if "reminders" in expect and expect["reminders"] != reminders:
        misses.append(f"set {reminders} reminder(s), not {expect['reminders']}")
    lines = [ln for ln in reply.splitlines() if ln.strip()]
    if len(lines) < expect.get("lines_at_least", 0):
        misses.append(f"{len(lines)} line(s), not {expect['lines_at_least']}+")
    if "reported" in expect and reported != expect["reported"]:
        misses.append("reported" if reported else "held back")
    return misses


# ---- a run ----------------------------------------------------------------------------------

@dataclass
class Outcome:
    case: str
    misses: list[str] = field(default_factory=list)
    reply: str = ""
    seconds: float = 0.0


@dataclass
class Models:
    reader: str
    answerer: str
    strong: str
    key: str
    zone: str


def _copy_vault(source: Path, target: Path) -> None:
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        here = Path(root)
        for name in files:
            if Path(name).suffix.lower() in SKIP_SUFFIXES:
                continue
            to = target / here.relative_to(source) / name
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(here / name, to)


def _when(case: dict) -> datetime:
    now = case.get("now")
    return datetime.fromisoformat(str(now)) if now else datetime.now()


async def _turn(case: dict, models: Models, work: Path, copies: Path,
                calls: dict[str, object]) -> tuple[str, list[str], int]:
    """One case through the pipeline, on a fresh copy of the vault."""
    vault = work / "vault"
    shutil.copytree(HOME / "vault", vault)
    db = work / "bot.sqlite"
    audit = AuditStore(db)
    audit.migrate()
    audit.close()
    now = _when(case)
    index = VaultIndex(vault)
    index.refresh()
    askers = Askers(work / "askers.json")
    group = case.get("chat", "owner") != "owner"
    user = int(case.get("user", 7)) if group else OWNER_ID
    for name in case.get("known") or []:
        askers._remember(user, str(name))
    google = GoogleSources(ReplayClient(calls), Sources(HOME / "sources.json"), copies)
    store = ReminderStore(db)
    base = pipe = VaultPipeline(
        index, VaultWriter(index, now=lambda: now),
        StagedFiler.claude(models.key, models.reader, lookup_model=models.strong),
        now=lambda: now,
        answerer=VaultAnswerer.claude(models.key, models.answerer,
                                      second_model=models.strong),
        reminders=store, zone=models.zone, google=google, askers=askers,
        planner=VaultPlanner.claude(models.key, models.reader, models.strong))
    if group:
        scope = Chats(HOME / "chats.json").get(int(case["chat"]))
        if scope is None:
            raise SystemExit(f"{case['id']}: chat {case['chat']} is not in chats.json")
        pipe = pipe.scoped(scope)
    chat = ChatRef(int(case["chat"]) if group else OWNER_ID, user, owner=not group,
                   name=str(case.get("name", "")))
    try:
        turn = await pipe.handle(str(case["text"]), chat=chat)
    finally:
        await base.aclose()
        store.close()  # Windows keeps an open database from being deleted
    return turn.reply_line(), [w.kind for w in turn.writes], len(turn.reminders)


async def _folder(case: dict, models: Models, work: Path) -> tuple[str, bool]:
    """A watched folder's new files, as the watcher reads them at a digest time."""
    folder = work / "folder"
    folder.mkdir()
    instruction = ""
    folders_json = HOME / "folders.json"
    if folders_json.exists():
        listed = json.loads(folders_json.read_text("utf-8"))
        instruction = listed[0].get("instruction", "") if listed else ""
    registry = Folders(work / "folders.json")
    registry.save([{"path": str(folder), "title": "T", "instruction": instruction}])
    sdk = anthropic.AsyncAnthropic(api_key=models.key, timeout=90.0, max_retries=1)
    watch = FolderWatch(registry, work / "state.json",
                        claude_ask(sdk, models.answerer, think=False),
                        confirm=claude_ask(sdk, models.strong, think=False))
    await watch.run()  # the baseline: an empty folder
    an_hour_ago = time.time() - 3600
    for name in case.get("files") or []:
        to = folder / name
        shutil.copy2(HOME / "files" / name, to)
        os.utime(to, (an_hour_ago, an_hour_ago))
    message = await watch.run()
    await sdk.close()
    return message, bool(message)


async def run_case(case: dict, models: Models, copies: Path,
                   calls: dict[str, object]) -> Outcome:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="regression-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        try:
            if case.get("kind") == "folder":
                reply, reported = await _folder(case, models, work)
                misses = check(case.get("expect") or {}, reply, [], 0, reported)
            else:
                reply, writes, reminders = await _turn(case, models, work, copies, calls)
                misses = check(case.get("expect") or {}, reply, writes, reminders)
        except Exception as e:  # noqa: BLE001 — a case that breaks is a failed case
            reply, misses = "", [f"raised {type(e).__name__}: {e}"]
    return Outcome(case["id"], misses, reply, time.monotonic() - started)


def render(models: Models, outcomes: dict[str, list[Outcome]]) -> str:
    runs = max((len(v) for v in outcomes.values()), default=0)
    passed = sum(1 for v in outcomes.values() for o in v if not o.misses)
    total = sum(len(v) for v in outcomes.values())
    out = [f"# Regressions — {datetime.now():%Y-%m-%d %H:%M}",
           f"reader {models.reader}, answerer {models.answerer}, strong {models.strong}; "
           f"{runs} run(s) each: **{passed}/{total} passed**", "",
           "| case | passed | median s | what went wrong |", "|---|---|---|---|"]
    for case, done in outcomes.items():
        ok = sum(1 for o in done if not o.misses)
        seconds = sorted(o.seconds for o in done)[len(done) // 2]
        wrong = "; ".join(dict.fromkeys(m for o in done for m in o.misses))
        out.append(f"| {case} | {ok}/{len(done)} | {seconds:.1f} | {wrong} |")
    failed = [(c, o) for c, done in outcomes.items() for o in done if o.misses]
    if failed:
        out += ["", "## Replies that failed", ""]
        for case, o in failed:
            out += [f"### {case}", "", "```", o.reply.strip()[:4000] or "(no reply)", "```", ""]
    return "\n".join(out)


async def run(a: argparse.Namespace) -> int:
    settings = _Settings(_env_file=a.env)
    models = Models(reader=a.reader or settings.filer_model,
                    answerer=a.answerer or settings.vault_answer_model,
                    strong=a.strong or settings.escalate_model,
                    key=settings.anthropic_api_key.get_secret_value(), zone=settings.timezone)
    if not models.key:
        print("no ANTHROPIC_API_KEY in", a.env, file=sys.stderr)
        return 2
    if not (HOME / "vault").is_dir():
        print("no snapshot: run `python -m tools.regressions snapshot` first", file=sys.stderr)
        return 2
    cases = yaml.safe_load((HOME / "cases.yaml").read_text("utf-8")) or []
    cases = [c for c in cases if not a.only or a.only in c["id"]]
    calls = json.loads((HOME / "google.json").read_text("utf-8")) \
        if (HOME / "google.json").exists() else {}
    outcomes: dict[str, list[Outcome]] = {c["id"]: [] for c in cases}
    with tempfile.TemporaryDirectory(prefix="regression-google-") as copies:
        for n in range(a.runs):
            for case in cases:
                outcome = await run_case(case, models, Path(copies), calls)
                outcomes[case["id"]].append(outcome)
                print(f"run {n + 1}: {case['id']}: "
                      f"{'ok' if not outcome.misses else '; '.join(outcome.misses)}",
                      flush=True)
    report = render(models, outcomes)
    print("\n" + report.split("\n## Replies that failed")[0])
    if a.write:
        a.write.parent.mkdir(parents=True, exist_ok=True)
        a.write.write_text(report, encoding="utf-8")
        print(f"\nwritten: {a.write}")
    return 0


# ---- the snapshot ---------------------------------------------------------------------------

READS = {"form": "form", "doc": "document", "sheet": "sheet_values"}


async def _retry_missing(recording: RecordingClient, tries: int = 3) -> None:
    """A folder's file Google timed out on is read again: one missing form was the very quiz
    a case asked about, and its replay found no quiz at all."""
    for _ in range(tries):
        wanted = [(READS[c["kind"]], c["id"], c.get("name", ""))
                  for key, listed in list(recording.calls.items()) if key.startswith("children:")
                  for c in listed if c.get("kind") in READS]
        missing = [w for w in wanted if _key(w[0], (w[1],)) not in recording.calls]
        if not missing:
            return
        for method, file_id, name in missing:
            try:
                await getattr(recording, method)(file_id)
            except GoogleError as e:
                print(f"still unread: {name or file_id} ({e})", file=sys.stderr)
    left = [w for w in wanted if _key(w[0], (w[1],)) not in recording.calls]
    if left:
        print(f"{len(left)} file(s) missing from the snapshot: "
              + ", ".join(n or i for _, i, n in left), file=sys.stderr)


async def snapshot(a: argparse.Namespace) -> int:
    """The vault, the chats and sources as configured, and Google's raw answers for every
    source — taken from the running install, never written to."""
    settings = _Settings(_env_file=a.env)
    data = a.data
    vault = Path(a.vault or settings.obsidian_vault)
    if not vault.is_dir():
        print(f"no vault at {vault!s}", file=sys.stderr)
        return 2
    if (HOME / "vault").exists():
        shutil.rmtree(HOME / "vault")
    _copy_vault(vault, HOME / "vault")
    for name in ("chats.json", "sources.json", "folders.json"):
        if (data / name).exists():
            shutil.copy2(data / name, HOME / name)
    calls: dict[str, object] = {}
    if (data / "google_token.json").exists() and (data / "sources.json").exists():
        with tempfile.TemporaryDirectory(prefix="regression-snapshot-") as tmp:
            token = Path(tmp) / "google_token.json"
            shutil.copy2(data / "google_token.json", token)  # a refresh writes the copy
            recording = RecordingClient(GoogleClient(GoogleAuth(token)))
            sources = Sources(HOME / "sources.json")
            google = GoogleSources(recording, sources, Path(tmp) / "copies")
            results = await asyncio.gather(*(google._refresh(s) for s in sources.all()),
                                           return_exceptions=True)
            for source, result in zip(sources.all(), results, strict=True):
                if isinstance(result, BaseException):
                    print(f"source {source.title or source.id}: {result}", file=sys.stderr)
            await _retry_missing(recording)
            calls = recording.calls
    (HOME / "google.json").write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")
    print(f"snapshot in {HOME}: vault copied, {len(calls)} Google answers recorded")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.regressions")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot", help="take the vault and the Google sources afresh")
    s.add_argument("--env", default="C:/apps/ai_assistant/.env")
    s.add_argument("--data", type=Path, default=Path("C:/apps/ai_assistant/data"))
    s.add_argument("--vault", default="", help="default: OBSIDIAN_VAULT from --env")
    r = sub.add_parser("run", help="run the cases")
    r.add_argument("--env", default="C:/apps/ai_assistant/.env")
    r.add_argument("--reader", default="", help="default: FILER_MODEL")
    r.add_argument("--answerer", default="", help="default: VAULT_ANSWER_MODEL")
    r.add_argument("--strong", default="", help="default: ESCALATE_MODEL")
    r.add_argument("--runs", type=int, default=1)
    r.add_argument("--only", default="", help="cases whose id contains this")
    r.add_argument("--write", type=Path)
    a = p.parse_args(argv)
    return asyncio.run(snapshot(a) if a.command == "snapshot" else run(a))


if __name__ == "__main__":
    raise SystemExit(main())
