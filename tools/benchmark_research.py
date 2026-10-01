"""The research loop against the old server-side search, on the same questions.

    uv run python -m tools.benchmark_research [--model claude-sonnet-5] [--only kv,milk]

Needs ANTHROPIC_API_KEY, read from the production .env and never printed. Costs real money:
about $1-3 for the full set, mostly the old path's. The table goes to stdout; the answers go to
data/eval/web/benchmark-<date>.md (git-ignored: listings carry sellers' names and numbers), to
be read for correctness — a fast answer that is wrong is not a win.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.research import ResearchError, WebResearcher, estimate_cost
from app.web.reader import Reader

QUESTIONS = [
    ("kv", "найди на kv.ee квартиру Kalevipoja põik 3-120 и детали объявления",
     "Kalevipoja põik 3-120 kv.ee"),
    ("flats", "найди квартиры в Ласнамяэ до 150 000 евро", "квартиры Lasnamäe продажа до 150000 €"),
    ("panels", "где дешевле клееный щит из сосны — K-Rauta или Ehituse ABC",
     "liimpuit mänd hind K-Rauta Ehituse ABC"),
    ("milk", "сравни цену молока в Rimi и Selver", "piim hind Rimi Selver"),
    ("iphone", "где дешевле всего iPhone 16 в Эстонии", "iPhone 16 hind Eesti"),
    ("borscht", "найди рецепт борща из копчёной курицы", "рецепт борща из копченой курицы"),
    ("trip", "план поездки с ребёнком Хельсинки–Стокгольм на выходные в октябре",
     "Хельсинки Стокгольм с ребёнком выходные октябрь"),
    ("pelevin", "список всех романов Виктора Пелевина", "все романы Виктора Пелевина список"),
]
OUT_DIR = Path("data/eval/web")


class _Keys(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    anthropic_api_key: SecretStr = SecretStr("")


class Meter:
    """Wraps a client's messages.create and adds up usage over every call a lookup makes —
    the old path's server-side loop included, which reports nothing of its own."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages = SimpleNamespace(create=self._create)
        self.reset()

    def reset(self) -> None:
        self.calls = self.input = self.output = self.cache_read = self.cache_write = 0
        self.searches = 0

    async def _create(self, **kwargs: Any) -> Any:
        resp = await self._client.messages.create(**kwargs)
        u = resp.usage
        self.calls += 1
        self.input += getattr(u, "input_tokens", 0) or 0
        self.output += getattr(u, "output_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        tools = getattr(u, "server_tool_use", None)
        self.searches += getattr(tools, "web_search_requests", 0) or 0
        return resp

    async def close(self) -> None:
        await self._client.close()


def build(engine: str, key: str, model: str, meter: Meter) -> WebResearcher:
    if engine == "server":  # what production runs before the switch
        return WebResearcher(key, model, client=meter, max_searches=3, deadline_s=600.0,
                             engine="server")
    return WebResearcher(key, model, client=meter, max_searches=4, max_reads=8,
                         soft_deadline_s=90.0, deadline_s=180.0, reader=Reader(),
                         engine="loop")


async def run(args: argparse.Namespace) -> int:
    key = _Keys(_env_file=args.env).anthropic_api_key.get_secret_value()
    if not key:
        raise SystemExit(f"ANTHROPIC_API_KEY is not set ({args.env})")
    only = set(args.only.split(",")) if args.only else None
    rows, answers = [], []
    for engine in ("server", "loop"):
        meter = Meter(anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=600))
        researcher = build(engine, key, args.model, meter)
        try:
            for name, request, query in QUESTIONS:
                if only and name not in only:
                    continue
                meter.reset()
                start = time.monotonic()
                try:
                    answer, ok = await researcher.research(request, query, "text"), True
                except ResearchError as e:
                    answer, ok = f"[{type(e).__name__}: {e}]", False
                seconds = time.monotonic() - start
                cost = estimate_cost(args.model, input_tokens=meter.input,
                                     output_tokens=meter.output, cache_read=meter.cache_read,
                                     cache_write=meter.cache_write, searches=meter.searches)
                rows.append((name, engine, ok, seconds, meter.input + meter.cache_read
                             + meter.cache_write, meter.output, cost))
                answers.append(f"## {name} — {engine} ({seconds:.0f} s, ~${cost:.3f})\n\n{answer}")
                print(f"{name:8} {engine:6} {'ok' if ok else 'FAIL':4} {seconds:5.0f} s  "
                      f"~${cost:.3f}", flush=True)
        finally:
            await researcher.aclose()
    print("\n| question | engine | answered | s | tokens in | out | ~$ |\n"
          "|---|---|---|---|---|---|---|")
    for name, engine, ok, s, tin, tout, cost in rows:
        print(f"| {name} | {engine} | {'yes' if ok else 'no'} | {s:.0f} | {tin:,} | {tout:,} "
              f"| {cost:.3f} |")
    for engine in ("server", "loop"):
        mine = [r for r in rows if r[1] == engine]
        print(f"{engine}: {sum(r[2] for r in mine)}/{len(mine)} answered, "
              f"{sum(r[3] for r in mine):.0f} s, ~${sum(r[6] for r in mine):.2f}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"benchmark-{date.today().isoformat()}.md"
    out.write_text("\n\n".join(answers), encoding="utf-8")
    print(f"answers: {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="claude-sonnet-5")
    p.add_argument("--only", default="", help="comma-separated question keys")
    p.add_argument("--env", default="C:/apps/ai_assistant/.env")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
