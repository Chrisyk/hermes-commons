"""Merge, promote and undo make every read-check under the repo lock, and pass git literal pathspecs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from commons.core import history as history_mod
from commons.core import merge as merge_mod
from commons.core.gitrepo import GitRepo
from commons.core.history import HistoryError, log, undo
from commons.core.journal import JournalError, operation
from commons.core.merge import MergeError, accept
from tests.fleet import skill_md, write_files
from tests.test_core_restore_ops import conflict_ready, set_up

LATER = datetime(2026, 11, 20, 17, 0, 0, tzinfo=UTC)
ODD_NAMES = ["日本語 スキル", "a [b] c*", "plain name"]


def add_shared(lay, folder: str, name: str, lines: list[str]) -> str:
    path = lay.shared_root / "general" / folder
    write_files(path, {"SKILL.md": skill_md(name, "Odd folder.", lines)})
    return GitRepo(lay.shared_root).commit_paths([path], f"add {name}")


@pytest.mark.parametrize("folder", ODD_NAMES)
def test_log_undo_commit_with_odd_folder_names(classic_fleet, folder):
    lay = set_up(classic_fleet)
    # A sibling whose name the glob in 'folder' would match, so a glob pathspec shows.
    add_shared(lay, "a b cd", "decoy", ["decoy"])
    first = add_shared(lay, folder, "odd", ["one"])
    assert first is not None
    md = lay.shared_root / "general" / folder / "SKILL.md"
    md.write_text(skill_md("odd", "Odd folder.", ["two"]), encoding="utf-8")
    second = GitRepo(lay.shared_root).commit_paths([md.parent], "edit odd")
    assert second is not None
    assert [s for _, _, s in log(lay, "odd")] == ["edit odd", "add odd"]
    assert [s for _, _, s in log(lay, "decoy")] == ["add decoy"]
    res = undo(lay, "odd", now=LATER)
    assert "one" in md.read_text(encoding="utf-8") and res.sha
    md.write_text(skill_md("odd", "Odd folder.", ["three"]), encoding="utf-8")
    with pytest.raises(HistoryError, match="uncommitted"):
        undo(lay, "odd", now=LATER)
    GitRepo(lay.shared_root).commit_paths([md.parent], "three")
    undo(lay, "odd", to=first, now=LATER)
    assert "one" in md.read_text(encoding="utf-8")
    assert GitRepo(lay.shared_root).status_porcelain() == []


def test_undo_dirty_check_sees_only_its_folder(classic_fleet):
    lay = set_up(classic_fleet)
    add_shared(lay, "a [b] c*", "odd", ["one"])
    md = lay.shared_root / "general" / "a [b] c*" / "SKILL.md"
    md.write_text(skill_md("odd", "x", ["two"]), encoding="utf-8")
    GitRepo(lay.shared_root).commit_paths([md.parent], "edit odd")
    write_files(lay.shared_root / "general" / "a b cd", {"SKILL.md": skill_md("other", "dirty", ["x"])})
    assert undo(lay, "odd", now=LATER).sha  # the dirty sibling does not block


def test_shared_edit_before_accept_is_refused(classic_fleet):
    task, check = conflict_ready(classic_fleet, with_shared=True)
    shared_md = task.shared_root / task.target_rel / "SKILL.md"
    shared_md.write_text(shared_md.read_text(encoding="utf-8") + "late edit\n", encoding="utf-8")
    head = GitRepo(task.shared_root).head()
    with pytest.raises(MergeError, match="shared copy changed"):
        accept(task, check, now=LATER)
    assert "late edit" in shared_md.read_text(encoding="utf-8")
    assert GitRepo(task.shared_root).head() == head


def test_accept_checks_run_under_the_lock(classic_fleet, monkeypatch):
    task, check = conflict_ready(classic_fleet, with_shared=True)
    repo = GitRepo(task.shared_root)
    seen = []
    real_check, real_target = merge_mod._check_accept, merge_mod._check_target

    def check_accept(*args):
        seen.append(("accept", repo.holds_lock()))
        return real_check(*args)

    def check_target(*args):
        seen.append(("target", repo.holds_lock()))
        return real_target(*args)

    monkeypatch.setattr(merge_mod, "_check_accept", check_accept)
    monkeypatch.setattr(merge_mod, "_check_target", check_target)
    assert not repo.holds_lock()
    accept(task, check, now=LATER)
    assert seen == [("accept", True), ("target", True)]


def test_undo_checks_run_under_the_lock(classic_fleet, monkeypatch):
    lay = set_up(classic_fleet)
    md = lay.shared_root / "general" / "same" / "SKILL.md"
    md.write_text(skill_md("same", "Edited.", ["x"]), encoding="utf-8")
    repo = GitRepo(lay.shared_root)
    repo.commit_paths([md.parent], "edit")
    seen = []
    real_dirty, real_folder = history_mod._dirty, history_mod.skill_folder

    def dirty(r, rel):
        seen.append(("dirty", repo.holds_lock()))
        return real_dirty(r, rel)

    def folder(*args):
        seen.append(("folder", repo.holds_lock()))
        return real_folder(*args)

    monkeypatch.setattr(history_mod, "_dirty", dirty)
    monkeypatch.setattr(history_mod, "skill_folder", folder)
    undo(lay, "same", now=LATER)
    assert seen == [("folder", True), ("dirty", True)]


def test_retire_shared_rechecks_the_fingerprint_before_moving(classic_fleet):
    task, _check = conflict_ready(classic_fleet, with_shared=True)
    shared = next(v for v in task.variants if v.profile == "shared")
    src = task.shared_root / task.target_rel
    (src / "SKILL.md").write_text("changed under us\n", encoding="utf-8")
    with (
        pytest.raises(JournalError, match="changed"),
        operation(task.layout, [], "merge-x", "merge", LATER) as op,
    ):
        op.retire_shared(task.name, task.target_rel, expected=shared.fingerprint)
    assert (src / "SKILL.md").read_text(encoding="utf-8") == "changed under us\n"


def test_status_paths_is_not_quoted(tmp_path):
    repo = GitRepo(tmp_path)
    repo.init()
    (tmp_path / "日本 x").mkdir()
    (tmp_path / "日本 x" / "f").write_text("1", encoding="utf-8")
    (tmp_path / 'q"uote').write_text("1", encoding="utf-8")
    assert repo.status_paths(tmp_path / "日本 x") == ["日本 x/f"]
    assert repo.status_paths(tmp_path / 'q"uote') == ['q"uote']
