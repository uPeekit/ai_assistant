"""Benchmark models on the Obsidian filer: Ollama models by name, Claude by API id.

    # draft the expected answers with Claude, for the user to correct
    uv run python -m tools.benchmark_filer --label claude-haiku-4-5

    # score models against those answers
    uv run python -m tools.benchmark_filer --models mistral-nemo:12b,qwen3:8b,gemma3:4b \
        [--write documentation/FILER_BENCHMARK.md]

Every model is given exactly the context frozen into the case file, so a run is reproducible
and all models are judged on the same vault.

What is scored, on the *first* action of the answer (a message asks for one thing in almost
every real case, and comparing two whole lists position by position measures alignment rather
than accuracy — `n` catches the rest):

  valid   the answer parsed and held at least one action
  n       the number of actions matches
  action  task/note/append/update/log/search/inbox
  note    which note it writes to, scored only where the expected answer names one
  folder  likewise
  tags    F1 over the tag set, scored only where the expected answer has tags
  all     action, note, folder and tags all correct

`safe`/`wrong` follows the existing benchmark's spirit: falling back to `inbox` is safe — the
user's words are kept and they can see them — while writing to the wrong note is wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.ollama import wants_think_flag
from app.llm.prompts import FILER_PROMPT, filer_message
from app.vault.filer import FILER_SCHEMA, Filer, FilerError, VaultContext
from app.vault.staged import StagedFiler

DEFAULT_CASES = Path("data/eval/vault_cases.yaml")
# "staged:claude-haiku-4-5" or "staged:qwen3:8b": that model behind the staged reader.
STAGED = "staged:"
SCORED = ("action", "note", "folder", "tags")


class _Keys(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore")
    anthropic_api_key: SecretStr = SecretStr("")


class OllamaFiler:
    """The filer's prompt and schema, answered by a local model.

    Identical system prompt, user message and JSON schema to `app.vault.filer.Filer`, so the
    only difference a benchmark measures is the model itself.
    """

    def __init__(self, base_url: str, model: str, *, num_ctx: int = 8192,
                 timeout_s: float = 300.0) -> None:
        self.model = model
        self._num_ctx = num_ctx
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def aclose(self) -> None:
        """Free the card before the next model loads: 8 GB holds one of these at a time."""
        try:
            await self._client.post("/api/generate", json={
                "model": self.model, "keep_alive": 0})
        except httpx.HTTPError:
            pass
        await self._client.aclose()

    async def file(self, message: str, ctx: VaultContext) -> tuple[list[dict], int, int]:
        body = {
            "model": self.model,
            # A system *message*: /api/chat ignores a top-level "system" field outright, and a
            # model that never saw the prompt is not the model being measured.
            "messages": [{"role": "system", "content": FILER_PROMPT},
                         {"role": "user", "content": filer_message(message, ctx.json())}],
            "stream": False,
            "format": FILER_SCHEMA,
            "options": {"temperature": 0.0, "num_ctx": self._num_ctx},
            "keep_alive": "30m",
        }
        if wants_think_flag(self.model):
            body["think"] = False  # a reasoning preamble is neither wanted nor scored
        try:
            resp = await self._client.post("/api/chat", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise FilerError(f"ollama: {e}") from None
        data = resp.json()
        text = (data.get("message") or {}).get("content", "")
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            raise FilerError("not JSON") from None
        actions = answer.get("actions")
        if not isinstance(actions, list):
            raise FilerError("no actions") from None
        return (actions, data.get("prompt_eval_count", 0) or 0,
                data.get("eval_count", 0) or 0)


@dataclass
class Result:
    id: str
    ms: int
    valid: bool = False
    error: str = ""
    got: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    n_ok: bool = False
    fields: dict[str, bool] = field(default_factory=dict)
    scored: set[str] = field(default_factory=set)
    labelled: bool = True

    @property
    def all_ok(self) -> bool:
        return self.valid and self.n_ok and all(self.fields.values())

    @property
    def safe(self) -> bool:
        """Right, or wrong in the way that keeps the user's words where they can see them."""
        return self.all_ok or self.got.get("action") == "inbox"


def _first(actions: list[dict]) -> dict:
    return actions[0] if actions and isinstance(actions[0], dict) else {}


def _tags(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    return {str(t).strip().lstrip("#").casefold() for t in raw if str(t).strip()}


def score(case: dict, actions: list[dict], ms: int, error: str = "") -> Result:
    r = Result(id=case["id"], ms=ms, error=error)
    expect = case.get("expect")
    if expect is None:
        r.labelled = False
    if error:
        return r
    r.valid = bool(actions)
    r.actions = actions
    first = _first(actions)
    r.got = {"action": str(first.get("action", "")), "note": str(first.get("note", "")),
             "folder": str(first.get("folder", "")), "tags": sorted(_tags(first.get("tags")))}
    if not r.labelled or not isinstance(expect, dict):
        return r
    want = expect.get("actions") or []
    r.n_ok = len(actions) == len(want)
    first_want = _first(want)
    for name in ("action", "note", "folder"):
        wanted = str(first_want.get(name, "")).strip()
        if not wanted:
            continue  # nothing claimed, nothing scored
        r.scored.add(name)
        r.fields[name] = str(first.get(name, "")).strip().casefold() == wanted.casefold()
    want_tags = _tags(first_want.get("tags"))
    if want_tags:
        r.scored.add("tags")
        got_tags = _tags(first.get("tags"))
        hit = len(want_tags & got_tags)
        precision = hit / len(got_tags) if got_tags else 0.0
        recall = hit / len(want_tags)
        r.fields["tags"] = precision == 1.0 and recall == 1.0
    return r


_SHOWN = ("note", "to", "folder", "title", "heading", "text", "task", "due", "repeat",
          "due_from", "due_to", "scope", "body", "props", "tags", "done", "countdown")


def brief(action: dict) -> str:
    """One action as a person reads it: its kind and only the fields that say something."""
    parts = [str(action.get("action", "?"))]
    for name in _SHOWN:
        value = action.get(name)
        if name == "props" and isinstance(value, list):
            value = {str(x.get("name")): x.get("value") for x in value
                     if isinstance(x, dict) and str(x.get("value", "")).strip()}
        if value in (None, "", [], {}, False, "any"):
            continue
        parts.append(f"{name}={json.dumps(value, ensure_ascii=False)}")
    return " ".join(parts)


def same(one: list[dict], two: list[dict]) -> bool:
    """Do two answers do the same thing? Compared on what is written where, not on wording:
    the kind of each action, the note or folder it goes to, whether it carries a date and
    whether it ticks something off."""
    def key(action: dict) -> tuple:
        return (str(action.get("action", "")), str(action.get("note", "")).casefold(),
                str(action.get("folder", "")).casefold(), bool(action.get("due")),
                action.get("done") is True)
    return sorted(map(key, one)) == sorted(map(key, two))


def disagreements(cases: list[dict], answers: dict[str, dict[str, Result]]) -> str:
    """Every case where a model's answer differs from the expected one, both shown."""
    lines = ["# Where the readers disagree", "",
             "`expected` is the answer in the case file; judge each pair on its own.", ""]
    for model, by_id in answers.items():
        differing = [c for c in cases if c["id"] in by_id and not same(
            (c.get("expect") or {}).get("actions") or [], by_id[c["id"]].actions)]
        lines += [f"## {model} — {len(differing)} of {len(by_id)} differ", ""]
        for case in differing:
            got = by_id[case["id"]]
            text = " ".join(str(case["text"]).split())
            lines.append(f"### {case['id']} — «{text}»")
            lines.append("- expected:")
            lines += [f"  - {brief(x)}"
                      for x in (case.get("expect") or {}).get("actions") or []]
            lines.append(f"- {model}:" + (f" {got.error}" if got.error else ""))
            lines += [f"  - {brief(x)}" for x in got.actions]
            lines.append("")
    return "\n".join(lines)


@dataclass
class Summary:
    model: str
    n: int
    labelled: int
    valid: int
    n_ok: int
    per_field: dict[str, tuple[int, int]]
    all_ok: int
    safe: int
    wrong: int
    p50: int
    p95: int


def summarize(model: str, results: list[Result]) -> Summary:
    lab = [r for r in results if r.labelled]
    per: dict[str, tuple[int, int]] = {}
    for name in SCORED:
        rs = [r for r in lab if name in r.scored]
        per[name] = (sum(1 for r in rs if r.fields.get(name)), len(rs))
    times = sorted(r.ms for r in results) or [0]
    return Summary(
        model=model, n=len(results), labelled=len(lab),
        valid=sum(1 for r in results if r.valid), n_ok=sum(1 for r in lab if r.n_ok),
        per_field=per, all_ok=sum(1 for r in lab if r.all_ok),
        safe=sum(1 for r in lab if r.safe), wrong=sum(1 for r in lab if not r.safe),
        p50=int(statistics.median(times)),
        p95=int(times[min(len(times) - 1, int(len(times) * 0.95))]),
    )


def _pct(hit: int, total: int) -> str:
    return f"{round(100 * hit / total)}%" if total else "-"


def render(summaries: list[Summary]) -> str:
    head = ("| model | cases | valid | count | " + " | ".join(SCORED)
            + " | all | safe | wrong | p50 ms | p95 ms |")
    rule = "|" + "---|" * (9 + len(SCORED))
    lines = [head, rule]
    for s in summaries:
        cells = [_pct(s.per_field[f][0], s.per_field[f][1]) for f in SCORED]
        lines.append(
            f"| {s.model} | {s.n} | {_pct(s.valid, s.n)} | {_pct(s.n_ok, s.labelled)} | "
            + " | ".join(cells)
            + f" | {_pct(s.all_ok, s.labelled)} | {_pct(s.safe, s.labelled)} | {s.wrong} "
              f"| {s.p50} | {s.p95} |")
    return "\n".join(lines)


def ollama_ask(base_url: str, model: str, *, num_ctx: int, timeout_s: float):
    """The staged reader's question, put to a local model. Returns (ask, close)."""
    client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def ask(system: str, schema: dict, content: str) -> tuple[dict, int, int]:
        body = {"model": model, "stream": False, "format": schema, "keep_alive": "30m",
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": content}],
                "options": {"temperature": 0.0, "num_ctx": num_ctx}}
        if wants_think_flag(model):
            body["think"] = False
        try:
            resp = await client.post("/api/chat", json=body)
            resp.raise_for_status()
            data = resp.json()
            answer = json.loads((data.get("message") or {}).get("content", ""))
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            raise FilerError(f"ollama: {type(e).__name__}") from None
        if not isinstance(answer, dict):
            raise FilerError("not an object")
        return (answer, data.get("prompt_eval_count", 0) or 0,
                data.get("eval_count", 0) or 0)

    async def close() -> None:
        try:
            await client.post("/api/generate", json={"model": model, "keep_alive": 0})
        except httpx.HTTPError:
            pass
        await client.aclose()

    return ask, close


def _filer(a: argparse.Namespace, model: str) -> Filer | OllamaFiler | StagedFiler:
    if model.startswith(STAGED):
        inner = model[len(STAGED):]
        if inner.startswith("claude-"):
            key = _Keys(_env_file=a.env).anthropic_api_key.get_secret_value()
            if not key:
                raise SystemExit(f"ANTHROPIC_API_KEY is not set ({a.env})")
            staged = StagedFiler.claude(key, inner, timeout_s=a.timeout)
        else:
            ask, close = ollama_ask(a.ollama, inner, num_ctx=a.num_ctx, timeout_s=a.timeout)
            staged = StagedFiler(ask, inner, close)
        staged.model = model  # the table names the reader, not only the model under it
        return staged
    if model.startswith("claude-"):
        key = _Keys(_env_file=a.env).anthropic_api_key.get_secret_value()
        if not key:
            raise SystemExit(f"ANTHROPIC_API_KEY is not set ({a.env})")
        return Filer(key, model, timeout_s=a.timeout)
    return OllamaFiler(a.ollama, model, num_ctx=a.num_ctx, timeout_s=a.timeout)


async def run_model(client: Filer | OllamaFiler | StagedFiler, cases: list[dict],
                    limit: int = 0) -> list[Result]:
    out: list[Result] = []
    for i, case in enumerate(cases[:limit] if limit else cases, 1):
        ctx = VaultContext(**case["context"])
        start = time.monotonic()
        try:
            actions, _, _ = await client.file(case["text"], ctx)
            ms = int((time.monotonic() - start) * 1000)
            out.append(score(case, actions, ms))
        except FilerError as e:
            ms = int((time.monotonic() - start) * 1000)
            out.append(score(case, [], ms, error=str(e)))
        except Exception as e:  # a local model can fail in ways the Claude path cannot
            ms = int((time.monotonic() - start) * 1000)
            out.append(score(case, [], ms, error=f"{type(e).__name__}: {e}"))
        print(f"  {i}/{len(cases)} {out[-1].id} "
              f"{'ok' if out[-1].valid else out[-1].error or 'invalid'} "
              f"{out[-1].ms} ms", flush=True)
    return out


async def label(a: argparse.Namespace, cases: list[dict], path: Path) -> int:
    """Draft `expect` for every case with one model, for the user to correct."""
    client = _filer(a, a.label)
    print(f"== labelling with {a.label} ==", flush=True)
    try:
        done = 0
        for i, case in enumerate(cases[:a.limit] if a.limit else cases, 1):
            ctx = VaultContext(**case["context"])
            try:
                actions, _, _ = await client.file(case["text"], ctx)
            except (FilerError, Exception) as e:  # noqa: B014 - report and carry on
                print(f"  {i}/{len(cases)} {case['id']} FAILED: {e}", flush=True)
                continue
            case["expect"] = {"actions": actions, "drafted_by": a.label, "reviewed": False}
            done += 1
            print(f"  {i}/{len(cases)} {case['id']} ok", flush=True)
    finally:
        await client.aclose()
    path.write_text(yaml.safe_dump({"cases": cases}, allow_unicode=True, sort_keys=False,
                                   width=100), encoding="utf-8")
    print(f"\ndrafted {done}/{len(cases)} -> {path}")
    print("Review the `expect` blocks and set `reviewed: true` as you go.")
    return 0


async def main_async(a: argparse.Namespace) -> int:
    doc = yaml.safe_load(a.cases.read_text(encoding="utf-8"))
    cases = doc.get("cases") or []
    if not cases:
        print(f"no cases in {a.cases}", file=sys.stderr)
        return 2
    if a.label:
        return await label(a, cases, a.cases)

    reviewed = sum(1 for c in cases if (c.get("expect") or {}).get("reviewed"))
    summaries: list[Summary] = []
    misses: dict[str, list[Result]] = {}
    answers: dict[str, dict[str, Result]] = {}
    for model in a.models.split(","):
        model = model.strip()
        if not model:
            continue
        print(f"\n== {model} ==", flush=True)
        client = _filer(a, model)
        try:
            results = await run_model(client, cases, a.limit)
        finally:
            await client.aclose()
        summaries.append(summarize(model, results))
        answers[model] = {r.id: r for r in results}
        misses[model] = [r for r in results if r.labelled and not r.all_ok]

    table = render(summaries)
    print("\n" + table)
    print(f"\ncases {len(cases)}, human-reviewed {reviewed}")
    if a.disagree:
        a.disagree.write_text(disagreements(cases, answers), encoding="utf-8")
        print(f"wrote {a.disagree}")
    if a.write:
        lines = [f"# Filer benchmark — {datetime.now(UTC).date().isoformat()}", "",
                 f"Cases: `{a.cases}` ({len(cases)}, human-reviewed {reviewed}), "
                 f"num_ctx={a.num_ctx}, temperature=0.", "", table, ""]
        for model, fails in misses.items():
            lines.append(f"## {model} — {len(fails)} miss(es)")
            for r in fails:
                lines.append(f"- `{r.id}`: {r.error or r.got}")
            lines.append("")
        a.write.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {a.write}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p.add_argument("--models", default="mistral-nemo:12b")
    p.add_argument("--label", default="", help="draft expected answers with this model")
    p.add_argument("--ollama", default="http://127.0.0.1:11434")
    p.add_argument("--env", default="C:/apps/ai_assistant/.env")
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--write", type=Path)
    p.add_argument("--disagree", type=Path,
                   help="write the cases where a model's whole answer differs from the "
                        "expected one, side by side, for a person to judge")
    a = p.parse_args(argv)
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    raise SystemExit(main())
