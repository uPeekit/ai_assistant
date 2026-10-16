"""tools/rename_tag.py: the tag changes everywhere it is a tag, and nowhere else."""

from __future__ import annotations

from tools.rename_tag import main, rename


def test_the_tag_changes_and_links_words_and_addresses_do_not():
    text = ("---\ntag: club\nnotion: https://t.me/clubhouse\n---\n\n"
            "- [ ] выводы запостить #club 🔁 every day\n"
            "```tasks\ntags include #club\n```\n"
            "- [[club|клуб]] `#club` — страница; #club_old #club/sub остаются\n")
    out = rename(text, "club", "club_my")
    assert "tag: club_my" in out and "https://t.me/clubhouse" in out
    assert "- [ ] выводы запостить #club_my 🔁" in out
    assert "tags include #club_my" in out and "`#club_my`" in out
    assert "[[club|клуб]]" in out and "#club_old" in out and "#club/sub" in out
    linked = rename("см. [[Области/club#club]] и https://t.me/page#club, (#club)\n", "club",
                    "club_my")
    assert linked == "см. [[Области/club#club]] и https://t.me/page#club, (#club_my)\n"


def test_tag_lists_in_properties_both_styles():
    assert "tags: [клуб, club_my]" in rename("---\ntags: [клуб, club]\n---\n", "club",
                                             "club_my")
    out = rename("---\ntags:\n  - club\n  - other\naliases:\n  - club\n---\n", "club",
                 "club_my")
    assert "  - club_my\n  - other\naliases:\n  - club\n" in out  # an alias is not a tag


def test_the_tool_backs_up_what_it_changes_and_skips_the_archive(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".stversions").mkdir(parents=True)
    (vault / "Задачи.md").write_bytes(b"- [ ] x #club\r\n")
    (vault / ".stversions/Задачи~old.md").write_text("- [ ] x #club\n", encoding="utf-8")
    (vault / "Другое.md").write_text("ничего\n", encoding="utf-8")
    backup = tmp_path / "backup"
    assert main(["--vault", str(vault), "--from", "club", "--to", "club_my",
                 "--backup", str(backup)]) == 0
    assert (vault / "Задачи.md").read_bytes() == b"- [ ] x #club_my\r\n"  # CRLF kept
    assert (backup / "Задачи.md").read_bytes() == b"- [ ] x #club\r\n"
    assert "#club\n" in (vault / ".stversions/Задачи~old.md").read_text(encoding="utf-8")
    assert not (backup / "Другое.md").exists()
