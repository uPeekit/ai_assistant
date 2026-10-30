"""What Undo takes back: one `executions` row's record of a message's writes.

Rows written before the bot was vault-only hold other kinds of record (a Notion page to
archive, a plan's batch). They still parse — a reply to one of those messages has to be read
to be recognised as nothing to fix — and their Notion parts are simply ignored."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.vault.writer import VaultUndo


class UndoRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # "vault" for every row written now.
    kind: str = "vault"
    # Files the turn wrote (app/vault/writer.py), put back newest first.
    vault: list[VaultUndo] = []
    # Reminders the turn set (app/reminders/): Undo cancels them.
    reminders: list[int] = []
    # An old plan's writes, one record each.
    batch: list[UndoRecord] = []

    def vault_undos(self) -> list[VaultUndo]:
        """Every vault write under this record, oldest first, the ones inside a batch too."""
        out = list(self.vault)
        for part in self.batch:
            out += part.vault_undos()
        return out
