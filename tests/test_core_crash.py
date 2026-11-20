"""Hard crashes during apply: restore must undo exactly what happened.

Each test makes apply die with a BaseException that is not an Exception
(like a kill or a KeyboardInterrupt) right after one action and before its
journal intent is flipped to done, then restores and checks that every
profile's skills tree, config.yaml and cron jobs.json are byte-identical to
before setup.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from commons.core import apply as apply_mod
from commons.core import paths as paths_mod
from commons.core.apply import intents, load_journal
from commons.core.backup import tree_sha256
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.restore import RestoreError, plan_restore, restore
from tests.conftest import PROFILES
from tests.fleet import profile_home
from tests.test_core_apply import NOW, layout_for, propose
from tests.test_core_restore import LATER


class Crash(BaseException):
    """A hard stop no `except Exception` catches."""


def state(root: Path) -> dict:
    out = {}
    for p in PROFILES:
        home = profile_home(root, p)
        jobs = home / "cron" / "jobs.json"
        out[p] = (
            tree_sha256(home / "skills"),
            (home / "config.yaml").read_bytes(),
            jobs.read_bytes() if jobs.exists() else None,
        )
    return out


def add_cron(root: Path, profiles=("default", "pikvm")) -> None:
    for p in profiles:
        old = profile_home(root, p) / "skills" / "general" / "same"
        jobs = profile_home(root, p) / "cron" / "jobs.json"
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.write_text(
            json.dumps({"jobs": [{"id": "j", "prompt": f"read {old}/SKILL.md"}]}), encoding="utf-8"
        )


def add_md_reference(root: Path) -> None:
    old = profile_home(root, "pikvm") / "skills" / "general" / "same"
    ref = profile_home(root, "device") / "skills" / "general" / "contained" / "references" / "r.md"
    ref.parent.mkdir(parents=True)
    ref.write_text(f"see {old}\n", encoding="utf-8")


def crash_apply(root: Path, writer=None):
    prop = propose(root)
    with pytest.raises(Crash):
        apply_mod.apply(prop, discover(root), layout_for(root), writer or YamlFileWriter(), NOW, home=root)
    (bid,) = os.listdir(layout_for(root).backups_dir)
    return bid


def restore_and_check(root: Path, bid: str, before: dict, pending_op: str):
    journal = load_journal(layout_for(root), bid)
    assert journal["complete"] is False and journal["error"].startswith("Crash")
    pending = [i for i in intents(journal) if i["state"] == "pending"]
    assert [i["op"] for i in pending] == [pending_op]
    plan = plan_restore(layout_for(root), bid)
    res = restore(plan, discover(root), YamlFileWriter(), now=LATER)
    assert state(root) == before, res.reports
    return res


def after_nth(monkeypatch, obj, name: str, n: int, when=lambda *a, **k: True):
    """Make obj.name run for real, then raise Crash on the n-th matching call."""
    real = getattr(obj, name)
    calls = []

    def wrapper(*args, **kwargs):
        out = real(*args, **kwargs)
        if when(*args, **kwargs):
            calls.append(args)
            if len(calls) == n:
                raise Crash(f"after {name} #{n}")
        return out

    monkeypatch.setattr(obj, name, wrapper)
    return calls


def test_crash_after_copy(classic_fleet, monkeypatch):
    before = state(classic_fleet)
    after_nth(monkeypatch, apply_mod, "copy_tree", 1)
    bid = crash_apply(classic_fleet)
    monkeypatch.undo()
    res = restore_and_check(classic_fleet, bid, before, "copy")
    assert any("partial copy" in r for r in res.reports)


def test_crash_after_commit(classic_fleet, monkeypatch):
    before = state(classic_fleet)
    after_nth(monkeypatch, GitRepo, "commit_paths", 1)
    bid = crash_apply(classic_fleet)
    monkeypatch.undo()
    restore_and_check(classic_fleet, bid, before, "commit")
    assert GitRepo(classic_fleet / "shared").head() is not None


class SetThenCrash(YamlFileWriter):
    def __init__(self, profile: str):
        self.profile = profile

    def set(self, profile, key, value):
        super().set(profile, key, value)
        if profile.name == self.profile:
            raise Crash("after config_set")


def test_crash_after_config_set_before_list_add(classic_fleet):
    before = state(classic_fleet)
    bid = crash_apply(classic_fleet, SetThenCrash("pikvm"))
    restore_and_check(classic_fleet, bid, before, "config_set")


@pytest.mark.parametrize("n", [1, 2, 8])
def test_crash_mid_retire(classic_fleet, monkeypatch, n):
    before = state(classic_fleet)
    after_nth(monkeypatch, apply_mod, "retire", n)
    bid = crash_apply(classic_fleet)
    monkeypatch.undo()
    res = restore_and_check(classic_fleet, bid, before, "retire")
    assert len([d for d in res.done if "put back" in d]) == n


def test_crash_before_retire_rename(classic_fleet, monkeypatch):
    """Pending retire whose action never ran: nothing to undo for it."""
    before = state(classic_fleet)
    real = apply_mod.retire
    calls = []

    def crashy(*args, **kwargs):
        calls.append(args)
        if len(calls) == 3:
            raise Crash("before retire #3")
        return real(*args, **kwargs)

    monkeypatch.setattr(apply_mod, "retire", crashy)
    bid = crash_apply(classic_fleet)
    monkeypatch.undo()
    res = restore_and_check(classic_fleet, bid, before, "retire")
    assert len([d for d in res.done if "put back" in d]) == 2


def test_crash_after_first_of_two_cron_writes(classic_fleet, monkeypatch):
    add_cron(classic_fleet)
    before = state(classic_fleet)
    after_nth(monkeypatch, paths_mod, "write_atomic", 1, when=lambda path, data: path.name == "jobs.json")
    bid = crash_apply(classic_fleet)
    monkeypatch.undo()
    res = restore_and_check(classic_fleet, bid, before, "cron_write")
    assert len([d for d in res.done if "jobs.json restored" in d]) == 1


def test_crash_mid_md_rewrite(classic_fleet, monkeypatch):
    add_md_reference(classic_fleet)
    add_cron(classic_fleet, ("pikvm",))
    before = state(classic_fleet)
    after_nth(monkeypatch, paths_mod, "write_atomic", 1, when=lambda path, data: path.suffix == ".md")
    bid = crash_apply(classic_fleet)
    monkeypatch.undo()
    res = restore_and_check(classic_fleet, bid, before, "md_write")
    assert any("r.md was rewritten by setup; kept" in r for r in res.reports)


def test_keyboard_interrupt_is_recorded_and_reraised(classic_fleet, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(apply_mod, "retire", interrupted)
    prop = propose(classic_fleet)
    with pytest.raises(KeyboardInterrupt):
        apply_mod.apply(prop, discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW)
    (bid,) = os.listdir(layout_for(classic_fleet).backups_dir)
    assert load_journal(layout_for(classic_fleet), bid)["error"].startswith("KeyboardInterrupt")


def test_retired_copy_changed_is_reported_and_left(classic_fleet):
    before = state(classic_fleet)
    result = apply_mod.apply(
        propose(classic_fleet), discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW
    )
    journal = load_journal(layout_for(classic_fleet), result.backup_id)
    changed, gone = (Path(i["args"]["dst"]) for i in intents(journal, "retire")[:2])
    (changed / "notes.txt").write_text("tampered\n", encoding="utf-8")
    shutil.rmtree(gone)
    plan = plan_restore(layout_for(classic_fleet), result.backup_id)
    res = restore(plan, discover(classic_fleet), YamlFileWriter(), now=LATER)
    assert any("does not match the journal" in r for r in res.reports)
    assert any("from the backup snapshot" in d for d in res.done)
    assert (changed / "notes.txt").read_text(encoding="utf-8") == "tampered\n"  # left in place
    after = state(classic_fleet)
    profile = intents(journal, "retire")[0]["args"]["profile"]
    assert {p: v for p, v in after.items() if p != profile} == {
        p: v for p, v in before.items() if p != profile
    }


def test_backup_problems_are_reported(classic_fleet):
    result = apply_mod.apply(
        propose(classic_fleet), discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW
    )
    snap = (
        layout_for(classic_fleet).backup_dir(result.backup_id)
        / "device"
        / "skills"
        / "a"
        / "dup"
        / "SKILL.md"
    )
    snap.write_text("x", encoding="utf-8")
    plan = plan_restore(layout_for(classic_fleet), result.backup_id)
    res = restore(plan, discover(classic_fleet), YamlFileWriter(), now=LATER)
    assert any("skills tree does not match manifest" in r for r in res.reports)
    assert any("put back" in d for d in res.done)


def test_restore_log_appends_runs(classic_fleet):
    result = apply_mod.apply(
        propose(classic_fleet), discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW
    )
    for _ in range(2):
        plan = plan_restore(layout_for(classic_fleet), result.backup_id)
        restore(plan, discover(classic_fleet), YamlFileWriter(), now=LATER)
    log = json.loads((layout_for(classic_fleet).backup_dir(result.backup_id) / "restore.json").read_text())
    assert len(log["runs"]) == 2


def test_restore_holds_the_shared_lock(classic_fleet, monkeypatch):
    result = apply_mod.apply(
        propose(classic_fleet), discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW
    )
    from commons.core import restore as restore_mod

    seen = []
    real = restore_mod._restore_all

    def spy(*args, **kwargs):
        seen.append(GitRepo(classic_fleet / "shared")._holds_lock())
        return real(*args, **kwargs)

    monkeypatch.setattr(restore_mod, "_restore_all", spy)
    plan = plan_restore(layout_for(classic_fleet), result.backup_id)
    restore(plan, discover(classic_fleet), YamlFileWriter(), now=LATER)
    assert seen == [True]


@pytest.mark.parametrize("bid", ["20990101T000000Z-setup", "../x", ""])
def test_missing_backup_id_is_a_restore_error(classic_fleet, bid):
    with pytest.raises(RestoreError):
        plan_restore(layout_for(classic_fleet), bid)


# ------------------------------------------------------------------ reviewer probes


def test_probe_crash_after_retire_before_journal(classic_fleet, monkeypatch):
    before = state(classic_fleet)
    real = apply_mod.retire
    done = []

    def crashy(*a, **k):
        d = real(*a, **k)
        done.append(d)
        if len(done) == 2:
            raise KeyboardInterrupt
        return d

    monkeypatch.setattr(apply_mod, "retire", crashy)
    with pytest.raises(KeyboardInterrupt):
        apply_mod.apply(
            propose(classic_fleet), discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW
        )
    monkeypatch.undo()
    (bid,) = os.listdir(layout_for(classic_fleet).backups_dir)
    plan = plan_restore(layout_for(classic_fleet), bid)
    restore(plan, discover(classic_fleet), YamlFileWriter(), now=LATER)
    assert not done[1].exists(), "retired copy not put back by restore"
    assert state(classic_fleet) == before


def test_probe_cron_rewrite_failure_is_journaled(classic_fleet, monkeypatch):
    add_cron(classic_fleet)
    before = state(classic_fleet)
    real = paths_mod._rewrite_cron
    calls = []

    def flaky(path, f, b):
        calls.append(path)
        if len(calls) == 2:
            raise TimeoutError("lock busy")
        return real(path, f, b)

    monkeypatch.setattr(paths_mod, "_rewrite_cron", flaky)
    r = apply_mod.apply(
        propose(classic_fleet),
        discover(classic_fleet),
        layout_for(classic_fleet),
        YamlFileWriter(),
        NOW,
        home=classic_fleet,
    )
    assert r.error and "lock busy" in r.error
    journal = load_journal(layout_for(classic_fleet), r.backup_id)
    cron = intents(journal, "cron_write")
    assert [c["args"]["path"] for c in cron] == [str(calls[0])]
    monkeypatch.undo()
    plan = plan_restore(layout_for(classic_fleet), r.backup_id)
    restore(plan, discover(classic_fleet), YamlFileWriter(), now=LATER)
    assert state(classic_fleet) == before
