"""Read-only calls to Google's REST APIs (Drive, Docs, Sheets, Forms) as the owner.

A thin layer: an authorised GET, one retry with a fresh token after a 401, and errors that say
which kind of failure it was — the caller decides whether a stale copy will do."""

from __future__ import annotations

import logging

import httpx

from app.google.auth import GoogleAuth
from app.google.errors import GoogleError, SignInNeeded

log = logging.getLogger(__name__)

DRIVE = "https://www.googleapis.com/drive/v3"
DOCS = "https://docs.googleapis.com/v1"
SHEETS = "https://sheets.googleapis.com/v4"
FORMS = "https://forms.googleapis.com/v1"
TIMEOUT_S = 10.0
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
FORM = "application/vnd.google-apps.form"
FOLDER = "application/vnd.google-apps.folder"
KIND_OF = {DOC: "doc", SHEET: "sheet", FORM: "form", FOLDER: "folder"}


__all__ = ["GoogleClient", "GoogleError", "SignInNeeded", "KIND_OF"]


class GoogleClient:
    def __init__(self, auth: GoogleAuth, *, http: httpx.AsyncClient | None = None) -> None:
        self._auth = auth
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S)

    @property
    def auth(self) -> GoogleAuth:
        return self._auth

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, url: str, params=None) -> dict:
        refused = ""
        for attempt in (1, 2):
            token = await self._auth.token(refused=refused)
            try:
                resp = await self._http.get(url, params=params,
                                            headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise GoogleError(type(e).__name__) from None
            if resp.status_code == 401 and attempt == 1:
                refused = token  # expired between the check and the call: a new one, once
                continue
            if resp.status_code >= 400:
                raise GoogleError(f"Google answered {resp.status_code}", resp.status_code)
            return resp.json()
        raise SignInNeeded("Google keeps refusing the token")

    # ---- Drive -----------------------------------------------------------------------------

    async def file(self, file_id: str) -> dict:
        return await self.get(f"{DRIVE}/files/{file_id}",
                              {"fields": "id,name,mimeType,modifiedTime,trashed",
                               "supportsAllDrives": "true"})

    async def children(self, folder_id: str) -> list[dict]:
        """The docs, sheets and forms directly in a folder (not sub-folders)."""
        out: list[dict] = []
        token = ""
        kinds = " or ".join(f"mimeType='{m}'" for m in (DOC, SHEET, FORM))
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed=false and ({kinds})",
                      "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
                      "pageSize": "200", "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            page = await self.get(f"{DRIVE}/files", params)
            out += page.get("files", [])
            token = page.get("nextPageToken", "")
            if not token:
                return out

    async def search(self, words: list[str], limit: int = 3) -> list[dict]:
        """Docs and sheets whose text holds every one of `words` (Drive's full-text index)."""
        if not words:
            return []
        clauses = " and ".join("fullText contains '{}'".format(w.replace("'", "\\'"))
                               for w in words)
        page = await self.get(f"{DRIVE}/files", {
            "q": f"({clauses}) and trashed=false and (mimeType='{DOC}' or mimeType='{SHEET}')",
            "fields": "files(id,name,mimeType,modifiedTime)", "pageSize": str(limit)})
        return page.get("files", [])

    # ---- the documents themselves ----------------------------------------------------------

    async def document(self, doc_id: str) -> dict:
        return await self.get(f"{DOCS}/documents/{doc_id}")

    async def sheet_values(self, sheet_id: str) -> tuple[str, list[tuple[str, list[list]]]]:
        """The spreadsheet's title and every tab's values, in one request. A tab's name is a
        quoted A1 range ("It's" → 'It''s'), sent as a parameter, so any name is safe."""
        meta = await self.get(f"{SHEETS}/spreadsheets/{sheet_id}",
                              {"fields": "properties.title,sheets.properties.title"})
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if not tabs:
            return meta.get("properties", {}).get("title", ""), []
        ranges = [("ranges", "'" + tab.replace("'", "''") + "'") for tab in tabs]
        values = await self.get(f"{SHEETS}/spreadsheets/{sheet_id}/values:batchGet", ranges)
        found = [v.get("values", []) for v in values.get("valueRanges", [])]
        return meta.get("properties", {}).get("title", ""), list(zip(tabs, found, strict=False))

    async def form(self, form_id: str) -> tuple[dict, list[dict]]:
        """The form (questions, quiz answers) and every response."""
        form = await self.get(f"{FORMS}/forms/{form_id}")
        responses: list[dict] = []
        token = ""
        while True:
            params = {"pageToken": token} if token else None
            page = await self.get(f"{FORMS}/forms/{form_id}/responses", params)
            responses += page.get("responses", [])
            token = page.get("nextPageToken", "")
            if not token:
                return form, responses
