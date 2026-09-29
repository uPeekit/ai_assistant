"""The local mail classifier, and the shadow digest that compares it with Claude."""

from __future__ import annotations

import json

import httpx
import pytest

from app.mail.classify import OTHER, Classifier
from app.mail.local import LocalClassifier
from app.mail.service import MailService, MailState, ShadowRun, shadow_digest
from tests.test_mail import BUCKETS, FakeMailbox, message
from tests.test_vault_filer import FakeAnthropic

OVERFLOW = object()  # an answer that reports the prompt filled the context window


def ollama(*answers: object, seen: list | None = None) -> httpx.MockTransport:
    """An Ollama that replies with each answer in turn; a string answer is sent verbatim so a
    test can hand it something that is not JSON. `seen` collects every chat request body."""
    replies = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("messages"):  # the unload call the classifier makes on close
            return httpx.Response(200, json={})
        if seen is not None:
            seen.append(body)
        answer = replies.pop(0) if replies else {}
        if isinstance(answer, Exception):
            raise answer
        if answer is OVERFLOW:
            return httpx.Response(200, json={"message": {"content": "{}"},
                                             "prompt_eval_count": 8192, "eval_count": 1})
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(200, json={"message": {"content": content},
                                         "prompt_eval_count": 11, "eval_count": 7})

    return httpx.MockTransport(handler)


def local(*answers: object, batch: int = 10, seen: list | None = None) -> LocalClassifier:
    return LocalClassifier("http://x", "m", BUCKETS, batch=batch,
                           transport=ollama(*answers, seen=seen))


@pytest.mark.asyncio
async def test_the_prompt_is_sent_as_a_system_message():
    """/api/chat silently ignores a top-level "system" field: a model sent one never sees the
    instructions, and every comparison made with it measures a model that was not asked."""
    seen: list = []
    await local({"messages": []}, seen=seen).sort([message("1")])
    body = seen[0]
    assert "system" not in body
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_a_batch_that_overflows_the_context_is_split_until_it_fits():
    """Ollama drops the head of an overlong prompt without a word; the answer to half a
    question is not an answer. Split and ask again, and every email still comes back."""
    seen: list = []
    classifier = local(
        OVERFLOW,                                                               # 1-4 at once
        {"messages": [{"id": "1", "bucket": "bills", "summary": "a"},
                      {"id": "2", "bucket": "bills", "summary": "b"}]},        # 1-2
        {"messages": [{"id": "3", "bucket": "personal", "summary": "c"},
                      {"id": "4", "bucket": "personal", "summary": "d"}]},     # 3-4
        batch=4, seen=seen)
    sorted_, _, _ = await classifier.sort([message(str(i)) for i in range(1, 5)])
    assert [(s.message.uid, s.bucket) for s in sorted_] == [
        ("1", "bills"), ("2", "bills"), ("3", "personal"), ("4", "personal")]
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_one_email_too_long_for_the_context_is_still_listed():
    classifier = local(OVERFLOW)
    sorted_, _, _ = await classifier.sort([message("1")])
    assert sorted_ == []  # nothing was answered at all: say why rather than show a guess
    assert "context" in classifier.last_error


@pytest.mark.asyncio
async def test_the_local_model_sorts_into_the_configured_buckets():
    sorted_, _, _ = await local({"messages": [
        {"id": "1", "bucket": "bills", "summary": "Счёт за электричество"}]}).sort(
        [message("1")])
    assert [(s.bucket, s.summary) for s in sorted_] == [("bills", "Счёт за электричество")]


@pytest.mark.asyncio
async def test_a_bucket_the_local_model_invented_still_falls_back_to_other():
    """`gate` is the whole safety story: a small model that answers off-list changes nothing."""
    sorted_, _, _ = await local({"messages": [
        {"id": "1", "bucket": "totally made up", "summary": "..."}]}).sort([message("1")])
    assert [s.bucket for s in sorted_] == [OTHER]


@pytest.mark.asyncio
async def test_a_local_model_that_never_answers_says_so_instead_of_sorting_into_other():
    """The real digest turns a failed batch into `other` so the mail is still listed. Here that
    would be a lie: it reads as a judgement the model made, when the model said nothing."""
    classifier = local("not json at all")
    sorted_, _, _ = await classifier.sort([message("1"), message("2")])
    assert sorted_ == []
    assert classifier.last_error == "not JSON"


@pytest.mark.asyncio
async def test_one_bad_batch_among_good_ones_still_lists_its_mail():
    """A model that mostly works is still worth comparing; only a total failure is an error."""
    classifier = local("rubbish",
                       {"messages": [{"id": "2", "bucket": "bills", "summary": "s"}]},
                       batch=1)
    sorted_, _, _ = await classifier.sort([message("1"), message("2")])
    assert [s.bucket for s in sorted_] == [OTHER, "bills"]


@pytest.mark.asyncio
async def test_a_long_run_is_chunked_and_the_results_are_merged():
    """Batch size is a context-window concern, not a result concern: every message comes back."""
    answers = [{"messages": [{"id": str(i), "bucket": "bills", "summary": f"s{i}"}]}
               for i in (1, 2, 3)]
    sorted_, _, _ = await local(*answers, batch=1).sort(
        [message("1"), message("2"), message("3")])
    assert [s.message.uid for s in sorted_] == ["1", "2", "3"]
    assert {s.bucket for s in sorted_} == {"bills"}


def svc(tmp_path, box, primary, *shadows) -> MailService:
    return MailService(box, primary, MailState(tmp_path / "state.json"),
                       buckets=BUCKETS, shadows=list(shadows))


def named(model: str, *answers: object) -> LocalClassifier:
    return LocalClassifier("http://x", model, BUCKETS, transport=ollama(*answers))


@pytest.mark.asyncio
async def test_both_models_see_the_same_mail_and_the_mailbox_is_read_once(tmp_path):
    box = FakeMailbox([([message("10"), message("11")], "1")])
    primary = Classifier("", "m", BUCKETS, client=FakeAnthropic({"messages": [
        {"id": "10", "bucket": "bills", "summary": "a"},
        {"id": "11", "bucket": "bills", "summary": "b"}]}))
    run = await svc(tmp_path, box, primary, local({"messages": [
        {"id": "10", "bucket": OTHER, "summary": "c"},
        {"id": "11", "bucket": "bills", "summary": "d"}]})).run()
    assert len(box.asked) == 1
    assert [s.bucket for s in run.sorted] == ["bills", "bills"]
    assert [s.bucket for s in run.shadows[0].sorted] == [OTHER, "bills"]


@pytest.mark.asyncio
async def test_every_local_model_sorts_the_same_mail_in_the_order_they_are_listed(tmp_path):
    box = FakeMailbox([([message("10")], "1")])
    primary = Classifier("", "m", BUCKETS, client=FakeAnthropic({"messages": [
        {"id": "10", "bucket": "bills", "summary": "a"}]}))
    run = await svc(tmp_path, box, primary,
                    named("qwen3:8b", {"messages": [{"id": "10", "bucket": "bills",
                                                     "summary": "b"}]}),
                    named("gemma3:1b", {"messages": [{"id": "10", "bucket": "personal",
                                                      "summary": "c"}]})).run()
    assert len(box.asked) == 1
    assert [(s.model, [x.bucket for x in s.sorted]) for s in run.shadows] == [
        ("qwen3:8b", ["bills"]), ("gemma3:1b", ["personal"])]


@pytest.mark.asyncio
async def test_a_shadow_that_fails_never_touches_the_real_digest_or_the_next_shadow(tmp_path):
    box = FakeMailbox([([message("10")], "1")])
    primary = Classifier("", "m", BUCKETS, client=FakeAnthropic({"messages": [
        {"id": "10", "bucket": "bills", "summary": "a"}]}))
    run = await svc(tmp_path, box, primary,
                    named("broken", httpx.ConnectError("down")),
                    named("fine", {"messages": [{"id": "10", "bucket": "bills",
                                                 "summary": "b"}]})).run()
    assert [s.bucket for s in run.sorted] == ["bills"]
    broken, fine = run.shadows
    assert broken.sorted == [] and broken.error
    assert [s.bucket for s in fine.sorted] == ["bills"] and not fine.error


def test_the_shadow_digest_names_the_model_and_what_it_cost():
    text = shadow_digest(ShadowRun("mistral-nemo:12b", error="ollama is not running", ms=1234),
                         BUCKETS)
    assert "mistral-nemo:12b" in text
    assert "ollama is not running" in text


def test_the_setting_lists_models_separated_by_commas(env):
    from app.config import Settings

    def models(value: str) -> list[str]:
        return Settings(_env_file=None, mail_shadow_model=value).mail_shadow_models

    assert models("") == []
    assert models("qwen3:8b") == ["qwen3:8b"]
    assert models(" qwen3:8b, gemma3:1b ,") == ["qwen3:8b", "gemma3:1b"]


@pytest.mark.asyncio
async def test_the_model_is_unloaded_as_soon_as_the_digest_is_sorted():
    """A digest runs twice a day. A model kept resident in between holds the card the Notion
    fallback and the transcriber need, so it is let go the moment its answer is in."""
    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(200, json={"message": {"content": json.dumps({"messages": [
            {"id": "1", "bucket": "bills", "summary": "s"}]})}, "prompt_eval_count": 5})

    classifier = LocalClassifier("http://x", "m", BUCKETS, transport=httpx.MockTransport(handler))
    await classifier.sort([message("1")])
    path, body = requests[-1]
    assert path == "/api/generate" and body == {"model": "m", "keep_alive": 0}
