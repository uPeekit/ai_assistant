"""app/google/: documents as sections, the sources registry, copies refreshed only on change,
and finding what a question needs — for the owner and for a group."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import texts
from app.google.auth import GoogleAuth, SignInNeeded
from app.google.client import DOC, FORM
from app.google.sections import doc_sections, form_sections, sheet_sections
from app.google.sources import GoogleSources, Sources, parse_link

GROUP = -100777


def para(text: str, style: str = "NORMAL_TEXT") -> dict:
    return {"paragraph": {"paragraphStyle": {"namedStyleType": style},
                          "elements": [{"textRun": {"content": text + "\n"}}]}}


NOTES = {"title": "Заметки клуба", "body": {"content": [
    para("Вступление о клубе"),
    para("Доктор Живаго", "HEADING_2"), para("21.09.2026"), para("Аня", "HEADING_3"),
    para("Понравились стихи в конце."),
    para("Ганнибал", "HEADING_2"), para("Спорили о Лектере."),
]}}
SHEET_TABS = ("Рейтинги", [("лист", [["", "min", "max", "avg", "Аня"], [],
                                      ["Доктор Живаго", "3", "5", "4.2", "5"],
                                      ["Ганнибал", "2", "4", "3.1", "4"]])])
QUIZ = {"info": {"title": "Ганнибал"}, "items": [
    {"title": "Ваше имя", "questionItem": {"question": {"questionId": "q1"}}},
    {"title": "Как звали доктора?", "questionItem": {"question": {
        "questionId": "q2", "grading": {"correctAnswers": {"answers": [{"value": "Лектер"}]}}}}},
]}
RESPONSES = [{"totalScore": 1, "answers": {
    "q1": {"textAnswers": {"answers": [{"value": "Аня"}]}},
    "q2": {"textAnswers": {"answers": [{"value": "Лектер"}]}}}}]


# ---- sections --------------------------------------------------------------------------------

def test_a_doc_cut_by_headings_is_one_section_per_heading_with_its_parts_inside():
    sections = doc_sections(NOTES, by_headings=True)
    assert [s.title for s in sections] == ["Заметки клуба", "Доктор Живаго", "Ганнибал"]
    assert "### Аня" in sections[1].text and "стихи" in sections[1].text


def test_a_doc_without_headings_becomes_overlapping_pieces():
    long = {"title": "Выводы", "body": {"content": [para("абзац " * 60) for _ in range(20)]}}
    pieces = doc_sections(long)
    assert len(pieces) > 3 and all(len(p.text) <= 2100 for p in pieces)
    assert pieces[0].title == texts.GOOGLE_PIECE.format(title="Выводы", n=1)


def test_a_sheet_row_names_itself_by_its_first_cell():
    [section] = sheet_sections(*SHEET_TABS)
    assert "Доктор Живаго — min: 3; max: 5; avg: 4.2; Аня: 5" in section.text


def test_a_quiz_lists_questions_correct_answers_and_each_response():
    [section] = form_sections(QUIZ, RESPONSES)
    assert section.title == "Ганнибал"
    assert f"2. Как звали доктора? ({texts.GOOGLE_CORRECT}: Лектер)" in section.text
    assert "Аня | Лектер" in section.text and texts.GOOGLE_SCORE.format(score=1) in section.text


def test_links_say_which_file_and_what_kind():
    assert parse_link("https://docs.google.com/document/d/AbC-1/edit") == ("AbC-1", "doc")
    assert parse_link("https://docs.google.com/spreadsheets/d/S1/edit#gid=0") == ("S1", "sheet")
    assert parse_link("https://docs.google.com/forms/d/F1/edit") == ("F1", "form")
    assert parse_link("https://drive.google.com/drive/folders/D1") == ("D1", "folder")
    assert parse_link("https://docs.google.com/forms/d/e/1FAIpQ/viewform") is None


# ---- sources and copies ------------------------------------------------------------------------

class FakeAuth:
    last_ok = None


class FakeClient:
    def __init__(self) -> None:
        self.auth = FakeAuth()
        self.modified = {"doc1": "t1", "sheet1": "t1", "folder1": "t1", "quiz1": "t1"}
        self.reads: list[str] = []
        self.searched: list[list[str]] = []
        self.delay = 0.0
        self.fail: Exception | None = None

    async def aclose(self):
        pass

    async def _maybe(self):
        if self.fail is not None:
            raise self.fail
        if self.delay:
            await asyncio.sleep(self.delay)

    async def file(self, file_id):
        await self._maybe()
        return {"id": file_id, "name": file_id, "modifiedTime": self.modified[file_id]}

    async def document(self, doc_id):
        self.reads.append(doc_id)
        return NOTES

    async def sheet_values(self, sheet_id):
        self.reads.append(sheet_id)
        return SHEET_TABS

    async def form(self, form_id):
        self.reads.append(form_id)
        return QUIZ, RESPONSES

    async def children(self, folder_id):
        await self._maybe()
        return [{"id": "quiz1", "name": "Ганнибал", "mimeType": FORM,
                 "modifiedTime": self.modified["quiz1"]}]

    async def search(self, words, limit=3):
        self.searched.append(words)
        return [{"id": "private1", "name": "Личный дневник", "mimeType": DOC,
                 "modifiedTime": "t1"}]


@pytest.fixture
def setup(tmp_path):
    sources = Sources(tmp_path / "sources.json")
    sources.save([
        {"link": "https://docs.google.com/document/d/doc1/edit", "title": "Заметки",
         "chats": [GROUP], "instruction": "одна глава на книгу", "split": "headings"},
        {"link": "https://docs.google.com/spreadsheets/d/sheet1/edit", "chats": [GROUP]},
        {"link": "https://drive.google.com/drive/folders/folder1", "chats": [GROUP],
         "instruction": "первый вопрос — имя"},
        {"link": "https://docs.google.com/document/d/mine/edit", "title": "Только моё"},
    ])
    client = FakeClient()
    clock = {"t": 1000.0}
    google = GoogleSources(client, sources, tmp_path / "copies", clock=lambda: clock["t"])
    return google, client, clock


async def test_a_group_gets_its_sources_only_and_never_drive_search(setup):
    google, client, _ = setup
    client.modified["mine"] = "t1"
    found = await google.find("что про Ганнибала?", GROUP, owner=False)
    names = {f.source for f in found}
    assert "Только моё" not in names and client.searched == []
    assert any(f.title == "Ганнибал" and f.instruction == "одна глава на книгу" for f in found)
    assert any(f.source == "Ганнибал" and f.instruction == "первый вопрос — имя" for f in found)


async def test_the_owner_gets_every_source_and_their_drive(setup):
    google, client, _ = setup
    client.modified["mine"] = "t1"
    await google.find("что про Ганнибала?", 1, owner=True)
    assert client.searched and "mine" in client.reads and "private1" in client.reads


async def test_a_file_is_read_again_only_when_it_changed(setup):
    google, client, clock = setup
    await google.find("Ганнибал", GROUP, owner=False)
    first = list(client.reads)
    await google.find("Ганнибал", GROUP, owner=False)
    assert client.reads == first  # checked within ten minutes: nothing asked
    clock["t"] += 601
    await google.find("Ганнибал", GROUP, owner=False)
    assert client.reads == first  # asked, unchanged: not read again
    client.modified["quiz1"] = "t2"
    clock["t"] += 601
    await google.find("Ганнибал", GROUP, owner=False)
    assert client.reads.count("quiz1") == 2  # the changed quiz only
    assert client.reads.count("doc1") == 1


async def test_a_slow_google_answers_from_the_copies_and_keeps_reading(setup, monkeypatch):
    from app.google import sources as mod

    google, client, clock = setup
    await google.find("Ганнибал", GROUP, owner=False)  # copies made
    monkeypatch.setattr(mod, "WAIT_S", 0.05)
    client.delay = 0.5
    clock["t"] += 601
    found = await google.find("Ганнибал", GROUP, owner=False)
    assert found  # from the copies, without waiting half a second
    await asyncio.sleep(0.6)  # the reading went on behind the answer


async def test_a_lost_sign_in_leaves_the_copies_and_is_said_once_a_day(setup):
    google, client, clock = setup
    await google.find("Ганнибал", GROUP, owner=False)
    client.fail = SignInNeeded("revoked")
    clock["t"] += 601
    assert await google.find("Ганнибал", GROUP, owner=False)
    assert google.sign_in_notice() == texts.GOOGLE_SIGN_IN
    assert google.sign_in_notice() == ""


def test_the_answerer_reads_within_its_budget_and_lists_the_rest():
    from app.google.sources import Found
    from app.vault.answer import _within

    found = {f"g{i}": Found(source="s", title=str(i), text="x" * 50) for i in range(1, 5)}
    assert _within(list(found), found, 120) == ["g1", "g2"]


async def test_every_section_is_handed_on_best_first(setup):
    google, _, _ = setup
    found = await google.find("Лектер", GROUP, owner=False)
    assert found[0].title == "Ганнибал" and len(found) >= 3  # the rest follow, by name


def test_a_source_needs_a_real_link(tmp_path):
    sources = Sources(tmp_path / "sources.json")
    with pytest.raises(ValueError):
        sources.save([{"link": "https://example.com/x"}])


# ---- the sign-in ----------------------------------------------------------------------

async def test_the_access_token_is_refreshed_and_kept(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s",
                                "token_uri": "https://oauth2.example/token"}), encoding="utf-8")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": f"a{len(calls)}", "expires_in": 3600})

    now = {"t": datetime(2026, 10, 16, 12, 0, tzinfo=UTC)}
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = GoogleAuth(path, http=http, now=lambda: now["t"])
    assert await auth.token() == "a1" and await auth.token() == "a1"  # kept for the hour
    now["t"] += timedelta(minutes=59, seconds=30)
    assert await auth.token() == "a2"  # a minute before it runs out: a new one
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "a2"
    await http.aclose()


async def test_a_refused_refresh_token_means_sign_in_again(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"refresh_token": "r", "client_id": "c"}), encoding="utf-8")
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(SignInNeeded):
        await GoogleAuth(path, http=http).token()
    with pytest.raises(SignInNeeded):
        await GoogleAuth(tmp_path / "missing.json").token()
    await http.aclose()


def test_how_a_doc_is_cut_is_the_sources_setting_not_the_codes():
    assert [s.title for s in doc_sections(NOTES)] == [
        texts.GOOGLE_PIECE.format(title="Заметки клуба", n=1)]  # default: pieces


async def test_choosing_another_cut_reads_the_doc_again(setup):
    google, client, _ = setup
    await google.find("Ганнибал", GROUP, owner=False)
    google._sources.save([{**s, "split": "pieces"} if s["file_id"] == "doc1" else s
                          for s in google._sources.dump()])
    await google.find("Ганнибал", GROUP, owner=False)
    assert client.reads.count("doc1") == 2


async def test_the_owners_instruction_counts_as_what_a_source_is_about(setup):
    google, _, _ = setup
    google._sources.save([{**s, "instruction": "оценки книг клуба"}
                          if s["file_id"] == "sheet1" else s for s in google._sources.dump()])
    found = await google.find("какие оценки у книг?", GROUP, owner=False)
    assert found and found[0].instruction == "оценки книг клуба"  # the sheet first


def test_a_sheet_tab_has_a_summary_of_its_number_columns():
    rows = [["", "avg", "Аня"], *[[f"книга {i}", str(i / 2), str(i)] for i in range(1, 9)]]
    sections = sheet_sections("Рейтинги", [("лист", rows)])
    summary = sections[0]
    assert summary.title == texts.GOOGLE_SUMMARY.format(title="Рейтинги / лист")
    assert "avg: " in summary.text and summary.text.index("книга 8 4") < summary.text.index(
        "книга 1 0.5")


def test_a_tables_summary_ranks_just_above_its_own_rows():
    from app.google.sections import Section
    from app.google.sources import _ranked

    candidates = [
        ("Рейтинги", "оценки книг", Section("Рейтинги / лист", "Ганнибал — avg: 3.1")),
        ("Рейтинги", "оценки книг", Section("Рейтинги (сводка)", "avg: …", summary=True)),
        ("Заметки", "", Section("часть 5", "средняя оценка книги была высокой")),
    ]
    found = _ranked("какая книга с лучшей оценкой?", candidates)
    assert found[0].title == "Рейтинги (сводка)"



# ---- from the branch review -------------------------------------------------------------

async def test_an_unshared_source_is_no_longer_quoted_and_the_page_says_so(setup):
    from app.google.errors import GoogleError

    google, client, clock = setup
    await google.find("Ганнибал", GROUP, owner=False)
    client.fail = GoogleError("Google answered 404", 404)
    clock["t"] += 601
    found = await google.find("Ганнибал", GROUP, owner=False)
    assert all(f.source != "Заметки" for f in found)  # the doc's old copy is not used
    assert any(s["error"] for s in google.status()["sources"])


async def test_a_lost_sign_in_is_not_cleared_by_answers_from_copies(setup):
    google, client, clock = setup
    await google.find("Ганнибал", GROUP, owner=False)
    client.fail = SignInNeeded("revoked")
    clock["t"] += 601
    await google.find("Ганнибал", GROUP, owner=False)
    assert google.sign_in_needed
    client.fail = None
    await google.find("Ганнибал", GROUP, owner=False)  # fresh copies: no new token
    assert google.sign_in_needed
    client.auth.last_ok = datetime.now(UTC) + timedelta(seconds=1)  # Google gave a token
    await google.find("Ганнибал", GROUP, owner=False)
    assert not google.sign_in_needed


async def test_drive_search_skips_files_inside_a_listed_folder(setup):
    google, client, _ = setup
    client.modified["mine"] = "t1"

    async def search(words, limit=3):
        client.searched.append(words)
        return [{"id": "quiz1", "name": "Ганнибал", "mimeType": FORM, "modifiedTime": "t1"}]

    client.search = search
    found = await google.find("Ганнибал", 1, owner=True)
    assert sum(1 for f in found if f.source == "Ганнибал") == 1  # not twice


def test_a_quiz_with_many_responses_is_blocks_and_a_score_summary():
    responses = [{"totalScore": n % 7, "answers": {
        "q1": {"textAnswers": {"answers": [{"value": f"участник {n}"}]}},
        "q2": {"textAnswers": {"answers": [{"value": "x" * 120}]}}}} for n in range(60)]
    sections = form_sections(QUIZ, responses)
    assert sections[0].summary and "участник 6 " in sections[0].text.split("\n")[0]
    assert len(sections) > 2 and all(len(s.text) <= 3300 for s in sections[1:])


def test_a_long_heading_section_is_split_not_cut():
    long = {"title": "Заметки", "body": {"content": [
        para("Доктор Живаго", "HEADING_2"), *[para("строка заметок " * 20) for _ in range(40)]]}}
    sections = doc_sections(long, by_headings=True)
    assert len(sections) > 1 and all(s.title.startswith("Доктор Живаго") for s in sections)


async def test_a_sheet_tab_with_a_quote_in_its_name_is_asked_for_safely():
    from app.google.client import GoogleClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("values:batchGet"):
            seen["ranges"] = request.url.params.get_list("ranges")
            return httpx.Response(200, json={"valueRanges": [{"values": [["a", "b"]]}]})
        return httpx.Response(200, json={"properties": {"title": "T"},
                                         "sheets": [{"properties": {"title": "It's #1?"}}]})

    class Auth:
        last_ok = None

        async def token(self, refused=""):
            return "t"

    client = GoogleClient(Auth(), http=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    title, tabs = await client.sheet_values("S1")
    assert seen["ranges"] == ["'It''s #1?'"] and tabs == [("It's #1?", [["a", "b"]])]
    await client.aclose()


async def test_no_network_for_the_token_is_a_google_error_not_a_crash(tmp_path):
    from app.google.errors import GoogleError

    path = tmp_path / "token.json"
    path.write_text(json.dumps({"refresh_token": "r", "client_id": "c"}), encoding="utf-8")

    def handler(request):
        raise httpx.ConnectError("no network")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(GoogleError):
        await GoogleAuth(path, http=http).token()
    await http.aclose()
