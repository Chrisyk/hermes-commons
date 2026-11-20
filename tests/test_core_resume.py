"""Rerun after a crash: apply(..., resume=id) finishes an interrupted setup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from commons.core import apply as apply_mod
from commons.core.apply import apply, intents, load_journal, load_setup_proposal, unfinished_setups
from commons.core.backup import tree_sha256
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.restore import plan_restore, restore
from tests.conftest import PROFILES
from tests.fleet import profile_home
from tests.test_core_apply import NOW, layout_for, propose
from tests.test_core_crash import Crash, after_nth, state
from tests.test_core_restore import LATER


def end_state(root: Path) -> dict:
    """Everything an apply leaves behind that must not depend on whether it was interrupted."""
    shared = root / "shared"
    repo = GitRepo(shared)
    return {
        # file names and bytes per profile (a symlink's target names its own tmp root: name only)
        "profiles": {
            p: {
                f.relative_to(profile_home(root, p)).as_posix(): None if f.is_symlink() else f.read_bytes()
                for f in sorted((profile_home(root, p) / "skills").rglob("*"))
                if f.is_symlink() or f.is_file()
            }
            for p in PROFILES
        },
        "shared": {
            p.relative_to(shared).as_posix(): p.read_bytes()
            for p in sorted(shared.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(shared).parts
        },
        "log": [subject for _, _, subject in repo.log(n=50)],
        "tracked": repo._run("ls-files", read_only=True).stdout,
    }


@pytest.fixture
def reference(tmp_path_factory):
    """End state of an uninterrupted apply on a fresh classic fleet."""
    from tests.conftest import _classic_spec
    from tests.fleet import make_fleet

    root = make_fleet(tmp_path_factory.mktemp("ref"), _classic_spec())
    prop = propose(root)
    r = apply(prop, discover(root), layout_for(root), YamlFileWriter(), NOW, home=root)
    assert r.error is None
    return end_state(root)


def crash_partial_copy(root: Path, monkeypatch) -> str:
    """Crash in the middle of the second copy: its target holds only part of the files."""
    real = apply_mod.copy_tree
    calls = []

    def half(src, dst):
        calls.append(src)
        if len(calls) == 2:
            dst.mkdir(parents=True)
            (dst / "SKILL.md").write_bytes((Path(src) / "SKILL.md").read_bytes()[:10])
            raise Crash("mid copy")
        return real(src, dst)

    monkeypatch.setattr(apply_mod, "copy_tree", half)
    with pytest.raises(Crash):
        apply(propose(root), discover(root), layout_for(root), YamlFileWriter(), NOW, home=root)
    monkeypatch.undo()
    (bid,) = os.listdir(layout_for(root).backups_dir)
    return bid


def test_rerun_without_resume_is_refused(classic_fleet, monkeypatch):
    bid = crash_partial_copy(classic_fleet, monkeypatch)
    assert unfinished_setups(layout_for(classic_fleet)) == [bid]
    r = apply(
        propose(classic_fleet), discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW
    )
    assert r.error and bid in r.error and "resume" in r.error and "restore" in r.error
    assert os.listdir(layout_for(classic_fleet).backups_dir) == [bid]


def test_resume_after_partial_copy_matches_uninterrupted(classic_fleet, monkeypatch, reference):
    root = classic_fleet
    bid = crash_partial_copy(root, monkeypatch)
    journal = load_journal(layout_for(root), bid)
    pending = [i for i in intents(journal) if i["state"] == "pending"]
    assert [i["op"] for i in pending] == ["copy"]
    target = Path(pending[0]["args"]["target"])
    partial = target.read_bytes() if target.is_file() else (target / "SKILL.md").read_bytes()
    prop = load_setup_proposal(layout_for(root), bid)
    r = apply(prop, discover(root), layout_for(root), YamlFileWriter(), NOW, home=root, resume=bid)
    assert r.error is None and r.problems == [] and r.backup_id == bid
    journal = load_journal(layout_for(root), bid)
    assert journal["complete"] is True
    assert all(i["state"] == "done" for i in intents(journal))
    (aside,) = intents(journal, "copy")[1]["args"]["partial"]
    assert (Path(aside) / "SKILL.md").read_bytes() == partial  # set aside, never deleted
    assert Path(aside).is_relative_to(layout_for(root).backup_dir(bid) / "partial")
    mine = end_state(root)
    assert mine["log"] == reference["log"]
    assert mine["tracked"] == reference["tracked"]
    assert mine["shared"].keys() == reference["shared"].keys()
    for rel, data in mine["shared"].items():
        if rel != ".commons/config.json":
            assert data == reference["shared"][rel], rel
    assert mine["profiles"] == reference["profiles"]


def test_resume_after_crash_mid_retire(classic_fleet, monkeypatch):
    root = classic_fleet
    before = state(root)
    after_nth(monkeypatch, apply_mod, "retire", 3)
    with pytest.raises(Crash):
        apply(propose(root), discover(root), layout_for(root), YamlFileWriter(), NOW, home=root)
    monkeypatch.undo()
    (bid,) = os.listdir(layout_for(root).backups_dir)
    prop = load_setup_proposal(layout_for(root), bid)
    r = apply(prop, discover(root), layout_for(root), YamlFileWriter(), NOW, home=root, resume=bid)
    assert r.error is None and r.problems == []
    journal = load_journal(layout_for(root), bid)
    assert journal["complete"] and len(journal["resumed"]) == 1
    retires = intents(journal, "retire")
    assert len(retires) == 8 and all(i["state"] == "done" for i in retires)
    assert len({i["args"]["src"] for i in retires}) == 8
    for p in PROFILES:
        assert not (profile_home(root, p) / "skills/general/same").exists()
    # and the whole thing still restores
    plan = plan_restore(layout_for(root), bid)
    res = restore(plan, discover(root), YamlFileWriter(), now=LATER)
    assert state(root) == before, res.reports


def test_resume_after_crash_after_commit(classic_fleet, monkeypatch, reference):
    root = classic_fleet
    after_nth(monkeypatch, GitRepo, "commit_paths", 1)
    with pytest.raises(Crash):
        apply(propose(root), discover(root), layout_for(root), YamlFileWriter(), NOW, home=root)
    monkeypatch.undo()
    (bid,) = os.listdir(layout_for(root).backups_dir)
    prop = load_setup_proposal(layout_for(root), bid)
    r = apply(prop, discover(root), layout_for(root), YamlFileWriter(), NOW, home=root, resume=bid)
    assert r.error is None
    assert end_state(root)["log"] == reference["log"]


def test_snapshot_never_commits_a_pending_copy(classic_fleet, monkeypatch):
    """A restored, interrupted setup leaves a partial copy; the next setup must not snapshot it."""
    root = classic_fleet
    bid = crash_partial_copy(root, monkeypatch)
    plan_restore(layout_for(root), bid)
    restore(plan_restore(layout_for(root), bid), discover(root), YamlFileWriter(), now=LATER)
    journal = load_journal(layout_for(root), bid)
    target = Path(next(i for i in intents(journal, "copy") if i["state"] == "pending")["args"]["target"])
    assert target.exists()
    partial = tree_sha256(target)
    rel = target.relative_to(root / "shared").as_posix()
    later = NOW.replace(hour=14)
    r = apply(propose(root), discover(root), layout_for(root), YamlFileWriter(), later, home=root)
    assert r.error is None, r.error
    repo = GitRepo(root / "shared")
    for sha, _, subject in repo.log(n=20):
        if subject == "commons: snapshot before setup":
            files = repo._run("show", "--name-only", "--format=", sha, read_only=True).stdout
            assert rel not in files
    # restore keeps the shared root as it is: the partial copy is still there, untouched and untracked
    assert tree_sha256(target) == partial
    assert rel not in repo._run("ls-files", read_only=True).stdout


def test_resume_wrong_proposal_or_finished_is_refused(classic_fleet, monkeypatch):
    root = classic_fleet
    bid = crash_partial_copy(root, monkeypatch)
    other = propose(root)
    r = apply(other, discover(root), layout_for(root), YamlFileWriter(), NOW, resume=bid)
    assert r.error and "another proposal" in r.error
    prop = load_setup_proposal(layout_for(root), bid)
    r = apply(prop, discover(root), layout_for(root), YamlFileWriter(), NOW, home=root, resume=bid)
    assert r.error is None
    r = apply(prop, discover(root), layout_for(root), YamlFileWriter(), NOW, resume=bid)
    assert r.error and "already finished" in r.error
