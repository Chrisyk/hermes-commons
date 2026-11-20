"""restore undoes every Phase 4 operation (merge accept, promote, demote, undo).

Each crash test makes the operation die with a BaseException that is not an
Exception right after its n-th journaled action and before that intent is
flipped to done (n = 0: the operation finishes), then restores its backup and
checks that every profile AND the shared root are back to their bytes from
before the operation (tree_sha256; .git and the lock file aside).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from commons.core import journal as journal_mod
from commons.core import restore as restore_mod
from commons.core.backup import file_sha256, path_sha256, tree_sha256
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.history import log, undo
from commons.core.merge import accept, next_conflict, prepare, skip, validate_staged
from commons.core.moves import copy_staging
from commons.core.promote import demote, promote
from commons.core.proposal import build_proposal
from commons.core.restore import plan_restore, restore
from tests.conftest import PROFILES
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files
from tests.test_core_apply import layout_for, run_apply
from tests.test_core_restore import LATER

CORE = Path(__file__).resolve().parent.parent / "commons" / "core"
FRESH = {"SKILL.md": skill_md("fresh", "New in pocket.", ["F1", "F2"]), "references/r.md": "ref\n"}


class Crash(BaseException):
    """A hard stop no `except Exception` catches."""


def state(root: Path) -> dict:
    shared = root / "shared"
    out: dict = {p: tree_sha256(profile_home(root, p) / "skills") for p in PROFILES}
    out["shared"] = {
        e.name: path_sha256(e) for e in sorted(shared.iterdir()) if e.name not in (".git", ".commons")
    }
    out["config.json"] = file_sha256(shared / ".commons" / "config.json")
    return out


def crash_after(monkeypatch, n: int) -> list[str]:
    """Make Operation._act raise Crash right after the n-th action, before journal.done (0: never)."""
    real = journal_mod.Operation._act
    calls: list[str] = []

    def _act(self, op, args, action):
        def wrapped():
            out = action()
            calls.append(op)
            if len(calls) == n:
                raise Crash(f"after {op} #{n}")
            return out

        return real(self, op, args, wrapped)

    monkeypatch.setattr(journal_mod.Operation, "_act", _act)
    return calls


def run_crashing(monkeypatch, n: int, fn):
    calls = crash_after(monkeypatch, n)
    if n == 0:
        fn()
    else:
        with pytest.raises(Crash):
            fn()
    monkeypatch.undo()
    return calls


def newest_backup(root: Path) -> str:
    """The backup made last (ids carry the clock each test passed, so order by creation instead)."""
    dirs = list(layout_for(root).backups_dir.iterdir())
    return max(dirs, key=lambda d: (d / "manifest.json").stat().st_mtime_ns).name


def restore_newest(root: Path):
    bid = newest_backup(root)
    plan = plan_restore(layout_for(root), bid)
    return restore(plan, discover(root), YamlFileWriter(), now=LATER)


def check_back(root: Path, before: dict) -> None:
    res = restore_newest(root)
    assert state(root) == before, (res.done, res.reports)
    assert GitRepo(root / "shared").status_porcelain() == []
    return res


def set_up(root):
    result = run_apply(root)
    assert result.error is None, result.error
    return layout_for(root)


# ------------------------------------------------------------------ every op has a handler


_WRITES = (
    r"(?:_act|begin|record|act|move|copy_in)\(\s*\"([a-z_]+)\"",
    r"\bop\s*=\s*\"([a-z_]+)\"\s*if\b[^\n]*else\s*\"([a-z_]+)\"",
)


def written_ops() -> set[str]:
    ops: set[str] = set()
    for path in sorted(CORE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in _WRITES:
            for m in re.finditer(pattern, text):
                ops |= {g for g in m.groups() if g}
    return ops


def test_restore_handles_every_op_written_anywhere():
    ops = written_ops()
    # The grep must actually see the writers, or this test proves nothing.
    assert {"retire", "retire_shared", "place", "shared_copy", "profile_copy", "set_aside"} <= ops
    assert {"cron_write", "md_write", "git_init", "revert", "checkout", "config_json", "task_file"} <= ops
    missing = sorted(ops - restore_mod.HANDLED_OPS)
    assert missing == [], f"restore has no handler for {missing}"


# ------------------------------------------------------------------ merge accept


def conflict_ready(root, with_shared: bool = False):
    lay = set_up(root)
    prop = None
    if with_shared:
        shared_dir = lay.shared_root / "general" / "conflict"
        write_files(shared_dir, {"SKILL.md": skill_md("conflict", "Diverged copies.", ["A", "S_shared_only"])})
        GitRepo(lay.shared_root).commit_paths([shared_dir], "add shared conflict")
        prop = build_proposal(discover(root), lay, dir_hash_md5, hub_content_hash, {}, home=root)
    skip(lay, "bundled-edited")
    task = next_conflict(lay, prop)
    assert task.name == "conflict"
    prepare(task, "shared" if with_shared else "variant:default")
    md = task.staging / "SKILL.md"
    for line in ("B", "C_default_only", "D_device_only", "S_shared_only"):
        if line not in md.read_text(encoding="utf-8"):
            md.write_text(md.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    check = validate_staged(task)
    assert check.ok, check
    return task, check


@pytest.mark.parametrize("n", range(8))
def test_crash_in_accept_is_restored(classic_fleet, monkeypatch, n):
    task, check = conflict_ready(classic_fleet)
    before = state(classic_fleet)
    staged = tree_sha256(task.staging)
    calls = run_crashing(monkeypatch, n, lambda: accept(task, check, now=LATER))
    if n == 0:
        assert calls == ["place", "commit", "retire", "retire", "config_json", "commit", "task_file"]
    check_back(classic_fleet, before)
    assert tree_sha256(task.staging) == staged  # the merge staging is back too


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_crash_in_accept_over_a_shared_copy_is_restored(classic_fleet, monkeypatch, n):
    task, check = conflict_ready(classic_fleet, with_shared=True)
    before = state(classic_fleet)
    calls = run_crashing(monkeypatch, n, lambda: accept(task, check, now=LATER))
    if n == 0:
        assert calls[:3] == ["retire_shared", "place", "commit"]
    res = check_back(classic_fleet, before)
    if n == 0:
        assert any("commons: restore conflict" == s for _, _, s in GitRepo(classic_fleet / "shared").log(n=1))
        assert any("shared: general/conflict put back" in d for d in res.done)


def test_half_written_place_is_moved_aside(classic_fleet, monkeypatch):
    task, check = conflict_ready(classic_fleet)
    before = state(classic_fleet)
    target = classic_fleet / "shared" / "general" / "conflict"
    real = journal_mod.copy_noreplace

    def half(src, dst):
        staging = copy_staging(dst)
        staging.mkdir(parents=True)
        (staging / "SKILL.md").write_text("half", encoding="utf-8")
        raise Crash("mid copy")

    monkeypatch.setattr(journal_mod, "copy_noreplace", half)
    with pytest.raises(Crash):
        accept(task, check, now=LATER)
    monkeypatch.setattr(journal_mod, "copy_noreplace", real)
    assert not target.exists() and copy_staging(target).exists()
    res = check_back(classic_fleet, before)
    assert any("half-written" in d for d in res.done)
    assert not copy_staging(target).exists()


# ------------------------------------------------------------------ promote and demote


@pytest.mark.parametrize("n", range(4))
def test_crash_in_promote_is_restored(classic_fleet, monkeypatch, n):
    root = classic_fleet
    set_up(root)
    write_files(profile_home(root, "pocket") / "skills" / "general" / "fresh", FRESH)
    before = state(root)
    calls = run_crashing(
        monkeypatch,
        n,
        lambda: promote("fresh", "pocket", layout_for(root), discover(root), dir_hash_md5, hub_content_hash, LATER),
    )
    if n == 0:
        assert calls == ["shared_copy", "commit", "retire"]
    check_back(root, before)


@pytest.mark.parametrize("n", range(4))
def test_crash_in_demote_is_restored(classic_fleet, monkeypatch, n):
    root = classic_fleet
    set_up(root)
    before = state(root)
    calls = run_crashing(monkeypatch, n, lambda: demote("same", "pikvm", layout_for(root), discover(root), LATER))
    if n == 0:
        assert calls == ["profile_copy", "config_json", "commit"]
    check_back(root, before)


def test_half_written_profile_copy_is_moved_aside(classic_fleet, monkeypatch):
    root = classic_fleet
    set_up(root)
    before = state(root)
    dest = profile_home(root, "pikvm") / "skills" / "general" / "same"

    def half(src, dst):
        copy_staging(dst).mkdir(parents=True)
        raise Crash("mid copy")

    monkeypatch.setattr(journal_mod, "copy_noreplace", half)
    with pytest.raises(Crash):
        demote("same", "pikvm", layout_for(root), discover(root), LATER)
    monkeypatch.undo()
    assert copy_staging(dest).exists()
    check_back(root, before)
    assert not copy_staging(dest).exists()


# ------------------------------------------------------------------ undo


def edited_history(lay):
    folder = lay.shared_root / "general" / "same"
    good = log(lay, "same", n=1)[0][0]
    write_files(folder, {"references/new.md": "added later\n", "notes.txt": "changed notes\n"})
    (folder / "SKILL.md").write_text(skill_md("same", "Edited.", ["S1", "S2", "S3"]), encoding="utf-8")
    GitRepo(lay.shared_root).commit_paths([folder], "two edits")
    return good


@pytest.mark.parametrize("n", range(4))
def test_crash_in_undo_to_is_restored(classic_fleet, monkeypatch, n):
    lay = set_up(classic_fleet)
    good = edited_history(lay)
    before = state(classic_fleet)
    calls = run_crashing(monkeypatch, n, lambda: undo(lay, "same", to=good, now=LATER))
    if n == 0:
        assert calls == ["set_aside", "checkout", "commit"]
    check_back(classic_fleet, before)


@pytest.mark.parametrize("n", range(2))
def test_crash_in_undo_revert_is_restored(classic_fleet, monkeypatch, n):
    lay = set_up(classic_fleet)
    md = lay.shared_root / "general" / "same" / "SKILL.md"
    md.write_text(skill_md("same", "Edited.", ["S1", "bad edit"]), encoding="utf-8")
    GitRepo(lay.shared_root).commit_paths([md.parent], "edited same")
    before = state(classic_fleet)
    calls = run_crashing(monkeypatch, n, lambda: undo(lay, "same", now=LATER))
    if n == 0:
        assert calls == ["revert"]
    check_back(classic_fleet, before)


def test_restore_leaves_a_folder_edited_after_undo(classic_fleet):
    lay = set_up(classic_fleet)
    good = edited_history(lay)
    undo(lay, "same", to=good, now=LATER)
    md = lay.shared_root / "general" / "same" / "SKILL.md"
    md.write_text("edited after the undo\n", encoding="utf-8")
    res = restore_newest(classic_fleet)
    assert md.read_text(encoding="utf-8") == "edited after the undo\n"
    assert any("changed since the undo" in r for r in res.reports)
