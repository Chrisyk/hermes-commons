"""Dropped files, stale overrides, the resume/restore hint, journaled skip, undo of a merge commit."""

from __future__ import annotations

import json
import os
import shutil

import pytest

from commons.core.apply import load_journal
from commons.core.check import fix_safe, run_check
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.history import HistoryError, undo
from commons.core.journal import operation
from commons.core.merge import SKIPPED, accept, next_conflict, prepare, skip, validate_staged
from commons.core.promote import demote
from commons.core.restore import plan_restore, restore
from tests.fleet import profile_home, write_files
from tests.test_core_apply import layout_for
from tests.test_core_restore import LATER
from tests.test_core_restore_ops import conflict_ready, newest_backup, set_up


def codes(root, code):
    return [f for f in run_check(discover(root), layout_for(root), home=root) if f.code == code]


def test_dropped_files_are_noted_even_when_their_lines_survive(classic_fleet):
    root = classic_fleet
    variant = profile_home(root, "device") / "skills" / "general" / "conflict"
    write_files(variant, {"references/copy.md": "A\n", "empty.txt": ""})
    os.symlink("SKILL.md", variant / "alias.md")
    lay = set_up(root)
    skip(lay, "bundled-edited")
    task = next_conflict(lay)
    prepare(task, "variant:default")
    md = task.staging / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "D_device_only\n", encoding="utf-8")
    check = validate_staged(task)
    assert check.ok, check  # nothing blocks: line 'A' is elsewhere, the rest is empty or a link
    notes = "\n".join(check.notes)
    assert "references/copy.md (file)" in notes
    assert "empty.txt (empty file)" in notes
    assert "alias.md (symlink)" in notes


def test_stale_override_is_flagged(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    res = demote("same", "pikvm", lay, discover(root), LATER)
    assert codes(root, "stale-override") == []
    shutil.rmtree(res.path)
    found = codes(root, "stale-override")
    assert [(f.severity, f.profile, f.skill) for f in found] == [("warn", "pikvm", "same")]


def test_restore_of_demote_reports_the_override_it_left(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    res = demote("same", "pikvm", lay, discover(root), LATER)
    # The config changed after the demote, so restore keeps it (and the override in it).
    data = json.loads(lay.config_path.read_text(encoding="utf-8"))
    data["note"] = "edited later"
    lay.config_path.write_text(json.dumps(data), encoding="utf-8")
    out = restore(plan_restore(lay, res.backup_id), discover(root), YamlFileWriter(), now=LATER)
    assert any("intentional override for same was left" in r for r in out.reports)
    assert [f.skill for f in codes(root, "stale-override")] == ["same"]


def test_unfinished_hint_says_resume_only_for_setup(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    try:
        with operation(lay, discover(root), "promote-x", "promote"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    (msg,) = [f.message for f in codes(root, "unfinished-apply")]
    assert "restore" in msg and "resume" not in msg
    bid = newest_backup(root)
    (lay.backup_dir(bid) / "proposal.json").write_text("{}", encoding="utf-8")
    (msg,) = [f.message for f in codes(root, "unfinished-apply")]
    assert "resume" in msg
    (lay.shared_root / "general" / "same" / "notes.txt").write_text("x\n", encoding="utf-8")
    refused = fix_safe(codes(root, "uncommitted"), lay)
    assert "resume" in refused.message


def test_skip_is_journaled_and_restorable(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    before = lay.config_path.read_bytes()
    bid = skip(lay, "conflict", now=LATER)
    journal = load_journal(lay, bid)
    assert journal["kind"] == "skip" and journal["complete"]
    assert "conflict" in json.loads(lay.config_path.read_text(encoding="utf-8"))[SKIPPED]
    restore(plan_restore(lay, bid), discover(root), YamlFileWriter(), now=LATER)
    assert lay.config_path.read_bytes() == before
    assert GitRepo(lay.shared_root).status_porcelain() == []


def test_undo_of_a_merge_commit_points_at_restore(classic_fleet):
    task, check = conflict_ready(classic_fleet)
    res = accept(task, check, now=LATER)
    lay = task.layout
    head = GitRepo(lay.shared_root).head()
    with pytest.raises(HistoryError, match=f"restore {res.backup_id}"):
        undo(lay, "conflict", now=LATER)
    assert GitRepo(lay.shared_root).head() == head
    out = undo(lay, "conflict", now=LATER, force=True)
    assert out.reverted == res.sha
