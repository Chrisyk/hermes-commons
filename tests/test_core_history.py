"""Tests for commons.core.history: per-skill log and undo in the shared root."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from commons.core.backup import tree_sha256
from commons.core.gitrepo import GitRepo
from commons.core.history import HistoryError, log, undo
from tests.fleet import skill_md, write_files
from tests.test_core_apply import layout_for, run_apply

LATER = datetime(2026, 11, 20, 16, 0, 0, tzinfo=UTC)


def set_up(root):
    result = run_apply(root)
    assert result.error is None, result.error
    return layout_for(root)


def edit(lay, rel, text, message):
    md = lay.shared_root / rel / "SKILL.md"
    md.write_text(text, encoding="utf-8")
    return GitRepo(lay.shared_root).commit_paths([md.parent], message)


def test_log_lists_commits_touching_the_folder(classic_fleet):
    lay = set_up(classic_fleet)
    sha = edit(lay, "general/same", skill_md("same", "Edited.", ["S1"]), "edited same")
    edit(lay, "general/contained", skill_md("contained", "Edited.", ["c"]), "edited contained")
    entries = log(lay, "same")
    assert entries[0][0] == sha and entries[0][2] == "edited same"
    assert all(subject != "edited contained" for _, _, subject in entries)
    assert entries[-1][2].startswith("commons setup")
    assert len(log(lay, "same", n=1)) == 1


def test_undo_reverts_a_single_folder_commit(classic_fleet):
    lay = set_up(classic_fleet)
    md = lay.shared_root / "general" / "same" / "SKILL.md"
    original = md.read_bytes()
    edit(lay, "general/same", skill_md("same", "Edited.", ["S1", "bad edit"]), "edited same")
    res = undo(lay, "same", now=LATER)
    assert md.read_bytes() == original
    repo = GitRepo(lay.shared_root)
    assert repo.head() == res.sha and repo.status_porcelain() == []
    assert log(lay, "same", n=1)[0][2].startswith("Revert")


def test_undo_refuses_a_multi_folder_commit(classic_fleet):
    lay = set_up(classic_fleet)
    for rel in ("general/same", "general/contained"):
        (lay.shared_root / rel / "SKILL.md").write_text(
            skill_md(rel.split("/")[1], "Both.", ["x"]), encoding="utf-8"
        )
    repo = GitRepo(lay.shared_root)
    repo.commit_paths([lay.shared_root / "general"], "edited both")
    head = repo.head()
    with pytest.raises(HistoryError, match="--to"):
        undo(lay, "same", now=LATER)
    assert repo.head() == head


def test_undo_to_sha_restores_bytes_and_keeps_added_files(classic_fleet):
    lay = set_up(classic_fleet)
    folder = lay.shared_root / "general" / "same"
    good = log(lay, "same", n=1)[0][0]
    before = tree_sha256(folder)
    write_files(folder, {"references/new.md": "added later\n", "notes.txt": "changed notes\n"})
    edit(lay, "general/same", skill_md("same", "Edited.", ["S1", "S2", "S3"]), "two edits")
    edit(lay, "general/same", skill_md("same", "Edited again.", ["S9"]), "third edit")
    res = undo(lay, "same", to=good[:10], now=LATER)
    assert tree_sha256(folder) == before
    aside = res.set_aside
    assert [p.name for p in aside] == ["references"]
    assert (aside[0] / "new.md").read_text(encoding="utf-8") == "added later\n"
    assert lay.backup_dir(res.backup_id) in aside[0].parents
    repo = GitRepo(lay.shared_root)
    assert repo.log(n=1)[0][2] == f"undo same to {good[:7]}"
    assert repo.status_porcelain() == []


def test_undo_refuses_uncommitted_changes(classic_fleet):
    lay = set_up(classic_fleet)
    md = lay.shared_root / "general" / "same" / "SKILL.md"
    edit(lay, "general/same", skill_md("same", "Edited.", ["S1"]), "edited same")
    md.write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(HistoryError, match="uncommitted"):
        undo(lay, "same", now=LATER)
    assert md.read_text(encoding="utf-8") == "uncommitted\n"


def test_unknown_skill(classic_fleet):
    lay = set_up(classic_fleet)
    with pytest.raises(HistoryError, match="no skill"):
        log(lay, "nope")
