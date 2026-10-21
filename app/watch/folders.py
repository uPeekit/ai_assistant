"""Folders the owner asked the bot to watch, each read with the owner's own instruction.

A folder on this machine where something else writes files — an agent's run reports, a log of
exports — and an instruction in the owner's words about what to tell them ("is anything asked of
me, did anything go wrong"). At the digest times the folder's listing is compared with the files
already reported; a new file that has stopped changing is read and handed, with the instruction,
to the model, which says whether anything needs the owner. What a folder holds, which parts of a
file matter and what counts as wrong is the instruction's to say — none of it is decided here.

    no new files               -> nothing
    new files, nothing needed  -> one line
    something needs the owner  -> the model's words, with the file each point came from

A file is reported once (a later change to it is not read again), and only after the model has
answered: a failed call leaves it for the next time. Fifteen quiet minutes do not make a file
done — an agent writes its report at the start of a run and adds to it for hours — so the model
also names the files that read as still being written: those are not reported, and are read
again when they change, or as they are once they have not changed for STALE.
`data/folders.json` holds the folders, edited on the admin page; the files already reported
are kept next to it."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from app import texts
from app.llm import prompts as P
from app.vault.filer import FilerError

log = logging.getLogger(__name__)

LATER = object()  # a file that could not be opened now: read it next time
QUIET = timedelta(minutes=15)  # changed more recently than this: still being written
STALE = timedelta(hours=6)  # unchanged this long, a file said to be unfinished is sent as it is
WAITING = "unfinished"  # the mark on a file the model read as still being written
MAX_CHARS = 40_000  # of all a folder's new files together, per run
MAX_TOKENS = 1_500
CONFIRM_READS = 2  # readings of a file said to be unfinished; either one holds it
STILL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["still"],
                "properties": {"still": {"type": "boolean"}}}
FOLDER_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["attention", "text", "unfinished"],
                 "properties": {"attention": {"type": "boolean"}, "text": {"type": "string"},
                                "unfinished": {"type": "array", "items": {"type": "string"}}}}


@dataclass(frozen=True)
class Folder:
    id: str
    path: str
    title: str = ""
    pattern: str = "*"
    instruction: str = ""
    enabled: bool = True
    # A file that is in the folder while its writer works ("IN-PROGRESS.md"): the folder is
    # not read then. Empty: none.
    busy: str = ""

    @property
    def name(self) -> str:
        return self.title or Path(self.path).name or self.path


def _path_id(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:10]


def _folder(raw: dict) -> Folder:
    path = str(raw.get("path", "")).strip()
    if not path:
        raise ValueError("a watched folder needs a path")
    busy = str(raw.get("busy", "")).strip()[:200]
    if busy and (busy in (".", "..") or any(c in busy for c in "/\\:")):
        raise ValueError("the busy file is a file name in the folder, not a path")
    return Folder(id=str(raw.get("id") or _path_id(path)), busy=busy,
                  path=path, title=str(raw.get("title", "")).strip()[:200],
                  pattern=str(raw.get("pattern", "")).strip() or "*",
                  instruction=str(raw.get("instruction", ""))[:2000],
                  enabled=raw.get("enabled", True) is not False)


class Folders:
    """data/folders.json, re-read by mtime. Unreadable: no folders, said once."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._folders: list[Folder] = []
        self._mtime: float | None = None
        self._lock = threading.Lock()

    def all(self) -> list[Folder]:
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime
            except OSError:
                self._folders, self._mtime = [], None
                return []
            if mtime != self._mtime:
                self._mtime = mtime
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if not isinstance(raw, list):
                        raise ValueError("not a list")
                    self._folders = [_folder(r) for r in raw if isinstance(r, dict)]
                except (OSError, ValueError) as e:
                    log.warning("could not read %s (%s); no watched folders", self._path, e)
                    self._folders = []
            return list(self._folders)

    def dump(self) -> list[dict]:
        return [asdict(f) for f in self.all()]

    def save(self, payload: object) -> int:
        if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
            raise ValueError("'folders' must be a list of objects")
        old = {f.id: f for f in self.all()}
        # A folder given another path is another folder: a new id, so it takes a new baseline
        # instead of reporting every file already in the new place.
        wanted = [replace(f, id=_path_id(f.path)) if f.id in old and old[f.id].path != f.path
                  else f for f in (_folder(r) for r in payload)]
        changed = sum(1 for f in wanted if old.get(f.id) != f) + len(
            set(old) - {f.id for f in wanted})
        if changed:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_text(json.dumps([asdict(f) for f in wanted], ensure_ascii=False,
                                          indent=1), encoding="utf-8")
                tmp.replace(self._path)
                self._folders, self._mtime = wanted, self._path.stat().st_mtime
            log.info("watched folders: %d (%d changed)", len(wanted), changed)
        return changed


class FolderWatch:
    def __init__(self, folders: Folders, state_path: Path, ask, *, confirm=None,
                 quiet: timedelta = QUIET, stale: timedelta = STALE,
                 max_chars: int = MAX_CHARS, clock: Callable[[], float] = time.time) -> None:
        self._folders = folders
        self._path = state_path
        self._ask = ask
        # A file said to be unfinished is read again, alone, by this (a stronger model): read
        # beside an older report, a finished one was taken for one still being written.
        self._confirm = confirm
        self._quiet = quiet.total_seconds()
        self._stale = stale.total_seconds()
        self._max_chars = max_chars
        self._clock = clock
        self._status: dict[str, dict] = {}
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            self._state: dict[str, dict[str, list]] = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self._state = {}

    async def run(self) -> str:
        """The message for one digest time: a part per folder with news, "" when none has."""
        parts = []
        folders = self._folders.all()
        # A folder taken off the list forgets its files: added again, it takes a new baseline
        # instead of reporting all that was written while it was away.
        for gone in set(self._state) - {f.id for f in folders}:
            del self._state[gone]
        for folder in folders:
            if not folder.enabled:
                continue
            try:
                part = await self._one(folder)
            except Exception:  # one folder must never cost the others their turn
                log.exception("watched folder %s failed", folder.id)
                part = ""
            if part:
                parts.append(part)
        self._save()
        return "\n\n".join(parts)

    async def _one(self, folder: Folder) -> str:
        try:
            listing = self._listing(folder)
        except OSError:
            self._status[folder.id] = {"ok": False, "detail": texts.FOLDER_MISSING}
            return ""
        now = self._clock()
        since = self._busy(folder, now)
        if since is not None:
            # The writer is at work: nothing is read, and what it writes waits for its end.
            self._status[folder.id] = {"ok": True, "detail": texts.FOLDER_BUSY.format(
                name=folder.busy, since=datetime.fromtimestamp(since).strftime("%d.%m %H:%M"))}
            return ""
        reported = self._state.get(folder.id)
        if reported is None:  # just added: what is there now is old
            self._state[folder.id] = {n: list(v) for n, v in listing.items()}
            self._seen(folder, listing)
            return ""
        new = sorted((n for n in listing if self._due(reported.get(n), listing[n], now)),
                     key=lambda n: listing[n][1])
        if not new:
            self._seen(folder, listing)
            return ""
        blocks: dict[str, tuple[str, str]] = {}  # name: (the text shown, the stale mark)
        sent, budget = [], self._max_chars
        for name in new:
            read = self._text(Path(folder.path) / name, self._max_chars)
            if read is LATER:
                continue
            if read is None:  # not a text file: never worth a model call
                reported[name] = list(listing[name])
                continue
            text, longer = read
            if blocks and (longer or len(text) > budget):
                # A cut would lose the end, where the asks tend to be: it waits for the next
                # run. Only a file over the cap on its own is cut.
                break
            cut = text[:budget]
            budget -= len(cut)
            sent.append(name)
            stale = now - listing[name][1] >= self._stale
            blocks[name] = (cut + (f"\n{texts.FOLDER_CUT}" if longer or len(cut) < len(text)
                                   else ""),
                            P.FOLDER_STALE.format(hours=int(self._stale // 3600)) + "\n"
                            if stale else "")
        if not blocks:
            self._seen(folder, listing)
            return ""

        def shown(name: str, mark: str = "") -> str:
            body, stale_mark = blocks[name]
            return P.FOLDER_FILE.format(
                name=name, when=datetime.fromtimestamp(listing[name][1]).strftime("%d.%m %H:%M"),
                text=(mark or stale_mark) + body)

        async def summary(done: set[str]) -> dict:
            parts = [shown(n, P.FOLDER_DONE + "\n" if n in done else "") for n in sent]
            answer, _, _ = await self._ask(
                P.FOLDER_PROMPT, FOLDER_SCHEMA,
                "\n\n".join([P.FOLDER_INSTRUCTION.format(instruction=folder.instruction),
                             *parts]), MAX_TOKENS)
            return answer

        try:
            answer = await summary(set())
            named = answer.get("unfinished")
            unfinished = {str(n) for n in named if str(n) in blocks
                          and now - listing[str(n)][1] < self._stale} \
                if isinstance(named, list) else set()
            if unfinished and self._confirm is not None:
                finished = {n for n in sorted(unfinished) if not await self._still(shown(n))}
                if finished:
                    log.info("watched folder %s: %s read as finished on their own",
                             folder.id, ", ".join(sorted(finished)))
                    unfinished -= finished
                    answer = await summary(finished)
        except FilerError as e:
            log.warning("watched folder %s: the model could not be asked (%s); next time",
                        folder.id, e)
            self._status[folder.id] = {"ok": False, "detail": texts.FOLDER_MODEL_FAILED}
            return ""
        done = 0
        for name in sent:  # what did not fit waits for the next run
            if name in unfinished:
                reported[name] = [*listing[name], WAITING]
            else:
                reported[name] = list(listing[name])
                done += 1
        self._seen(folder, listing)
        if not done:  # all still being written: nothing to say yet
            return ""
        text = str(answer.get("text", "")).strip()
        if answer.get("attention") and text:
            return texts.FOLDER_ATTENTION.format(title=folder.name, text=text)
        return texts.FOLDER_OK.format(title=folder.name, n=done)

    async def _still(self, block: str) -> bool:
        """Is this one file, read on its own, still being written? Read twice, and either
        reading holds it: a report with every section written up front was read as finished
        one time in six, and sent half-done is the failure that matters."""
        for _ in range(CONFIRM_READS):
            answer, _, _ = await self._confirm(P.FOLDER_UNFINISHED_PROMPT, STILL_SCHEMA, block,
                                               MAX_TOKENS)
            if answer.get("still") is True:
                return True
        return False

    def _due(self, entry: list | None, seen: tuple[int, float], now: float) -> bool:
        """Is the file to be read now: new and quiet, or waiting to be finished and changed
        since (or unchanged past STALE)."""
        size, mtime = seen
        if now - mtime < self._quiet:
            return False
        if entry is None:
            return True
        if len(entry) < 3 or entry[2] != WAITING:
            return False  # reported
        return [size, mtime] != list(entry[:2]) or now - mtime >= self._stale

    def _busy(self, folder: Folder, now: float) -> float | None:
        """When the folder's busy file appeared, while it holds the folder. One left behind
        for STALE (a writer that died) no longer does."""
        if not folder.busy:
            return None
        try:
            mtime = (Path(folder.path) / folder.busy).stat().st_mtime
        except OSError:
            return None
        if now - mtime >= self._stale:
            log.warning("watched folder %s: %s is %d h old; read anyway", folder.id,
                        folder.busy, (now - mtime) // 3600)
            return None
        return mtime

    def _listing(self, folder: Folder) -> dict[str, tuple[int, float]]:
        root = Path(folder.path)
        if not root.is_dir():
            raise OSError(f"not a folder: {root}")
        out = {}
        for entry in root.iterdir():
            if entry.is_file() and fnmatch.fnmatch(entry.name, folder.pattern) \
                    and entry.name != folder.busy:
                stat = entry.stat()
                out[entry.name] = (stat.st_size, stat.st_mtime)
        return out

    @staticmethod
    def _text(path: Path, limit: int):
        """(text, longer than read) for a text file; None for one that is not text; LATER for
        one that cannot be opened now (locked by its writer). Reads at most what the cap can
        use, never a whole video someone dropped in the folder."""
        try:
            with path.open("rb") as f:
                data = f.read(limit * 4 + 4)  # a character is at most four bytes
                longer = bool(f.read(1))
        except OSError:
            return LATER
        if b"\x00" in data[:8192]:
            return None
        text = data.decode("utf-8", errors="replace")
        if text.count("\ufffd") > 2 + len(text) // 100:  # not UTF-8 text
            return None
        return text.rstrip("\ufffd"), longer

    def _seen(self, folder: Folder, listing: dict[str, tuple[int, float]]) -> None:
        last = max((m for _, m in listing.values()), default=None)
        self._status[folder.id] = {"ok": True, "detail": texts.FOLDER_SEEN.format(
            n=len(listing), last=datetime.fromtimestamp(last).strftime("%d.%m %H:%M")
            if last else "—")}

    def status(self) -> list[dict]:
        """For the admin page: each folder and what the last run made of it."""
        return [{"id": f.id, "title": f.name,
                 **self._status.get(f.id, {"ok": True, "detail": texts.FOLDER_NOT_YET})}
                for f in self._folders.all()]

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("could not keep the watched folders' state in %s: %s", self._path, e)
