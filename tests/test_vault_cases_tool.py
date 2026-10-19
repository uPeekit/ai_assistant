"""tools/vault_cases.py: a case's frozen context must be writable as YAML."""

from __future__ import annotations

import yaml

from app.vault.index import VaultIndex
from tools.vault_cases import case


def test_a_case_with_set_valued_context_is_written_as_yaml(tmp_path):
    (tmp_path / "Клуб").mkdir()
    for name in ("a", "b"):
        (tmp_path / f"Клуб/{name}.md").write_text("---\ntags:\n- club\n---\n", encoding="utf-8")
    index = VaultIndex(tmp_path)
    index.refresh()

    built = case(7, "привет", "2026-10-19T10:00:00+00:00", index)

    assert built["context"]["folder_tagged"] == ["Клуб"]
    yaml.safe_dump(built, allow_unicode=True)  # raised RepresenterError on a frozenset
