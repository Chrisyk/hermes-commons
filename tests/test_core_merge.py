"""Tests for commons.core.merge: the agent-assisted conflict merge queue."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from commons.core.backup import tree_sha256
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.merge import (
    MergeError,
    accept,
    load_task,
    next_conflict,
    prepare,
    skip,
    validate_staged,
)
from commons.core.proposal import build_proposal
from commons.core.resolve import TIER_CREATE_DIR, resolve
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files
from tests.test_core_apply import layout_for, run_apply

LATER = datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)


def set_up(root):
    result = run_apply(root)
    assert result.error is None, result.error
    return layout_for(root)


def conflict_task(lay):
    """The task for 'conflict' (bundled-edited comes first and is skipped)."""
    skip(lay, "bundled-edited")
    task = next_conflict(lay, None)
    assert task is not None and task.name == "conflict"
    return task


def staged_md(task):
    return task.staging / "SKILL.md"


def add_line(path, line):
    path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def test_next_conflict_lists_variants(classic_fleet):
    lay = set_up(classic_fleet)
    task = next_conflict(lay, None)
    assert task.name == "bundled-edited"
    assert {v.profile for v in task.variants} == {"pikvm", "pocket"}
    assert all(v.unique_lines == 1 for v in task.variants)
    assert all(v.size > 0 and v.mtime for v in task.variants)
    assert task.staging == lay.staging_dir / "bundled-edited"
    assert task.target_rel == "bundled/bundled-edited"


def test_skip_keeps_it_a_conflict(classic_fleet):
    lay = set_up(classic_fleet)
    skip(lay, "bundled-edited")
    assert next_conflict(lay, None).name == "conflict"
    assert next_conflict(lay, None, include_skipped=True).name == "bundled-edited"
    data = json.loads(lay.config_path.read_text(encoding="utf-8"))
    assert "bundled-edited" in data["skipped"] and "bundled-edited" not in data.get("resolved", [])


def test_all_lines_kept_accepts_and_profiles_resolve_merged_copy(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    add_line(staged_md(task), "D_device_only")
    check = validate_staged(task)
    assert check.problems == [] and check.lost_lines == [] and check.lost_files == []
    res = accept(task, check, now=LATER)
    merged = lay.shared_root / "general" / "conflict" / "SKILL.md"
    assert "D_device_only" in merged.read_text(encoding="utf-8")
    for p in discover(root):
        r = resolve(p, dir_hash_md5, hub_content_hash)["conflict"]
        assert r.winner_tier == TIER_CREATE_DIR and r.winner_path == merged, p.name
    retired = lay.backup_dir(res.backup_id) / "retired"
    assert (retired / "default" / "general" / "conflict" / "SKILL.md").is_file()
    assert (retired / "device" / "general" / "conflict" / "SKILL.md").is_file()
    assert res.kept == []
    assert GitRepo(lay.shared_root).log(merged.parent, n=1)[0][2] == "merge conflict"
    data = json.loads(lay.config_path.read_text(encoding="utf-8"))
    assert "conflict" in data["resolved"]
    assert next_conflict(lay, None) is None
    assert not task.staging.exists()


def test_one_missing_line_is_refused_and_reported(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    check = validate_staged(task)
    assert [(lost.profile, lost.file, lost.line) for lost in check.lost_lines] == [
        ("device", "SKILL.md", "D_device_only")
    ]
    before = tree_sha256(profile_home(root, "device") / "skills")
    with pytest.raises(MergeError, match="D_device_only"):
        accept(task, check, now=LATER)
    assert tree_sha256(profile_home(root, "device") / "skills") == before
    assert not (lay.shared_root / "general" / "conflict").exists()


def test_force_accepts_a_kept_variant(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    check = validate_staged(task)
    res = accept(task, check, force=True, now=LATER)
    assert (lay.shared_root / "general" / "conflict" / "SKILL.md").is_file()
    device_copy = lay.backup_dir(res.backup_id) / "retired" / "device" / "general" / "conflict"
    assert "D_device_only" in (device_copy / "SKILL.md").read_text(encoding="utf-8")


def test_task_survives_a_reload(classic_fleet):
    lay = set_up(classic_fleet)
    task = conflict_task(lay)
    prepare(task, "variant:device")
    again = load_task(lay, "conflict")
    assert again == task


def test_profile_copy_edited_after_prepare_is_not_retired(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    add_line(staged_md(task), "D_device_only")
    device_md = profile_home(root, "device") / "skills" / "general" / "conflict" / "SKILL.md"
    add_line(device_md, "late edit")
    check = validate_staged(task)
    res = accept(task, check, force=True, now=LATER)
    assert device_md.is_file() and "late edit" in device_md.read_text(encoding="utf-8")
    assert any("device" in k and "changed" in k for k in res.kept)
    assert not (profile_home(root, "default") / "skills" / "general" / "conflict").exists()


def test_missing_reference_that_no_variant_has_is_only_a_note(classic_fleet):
    lay = set_up(classic_fleet)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    add_line(staged_md(task), "D_device_only")
    add_line(staged_md(task), "See [extra](references/extra.md) and `scripts/run.sh`.")
    write_files(task.staging, {"scripts/run.sh": "echo hi\n"})
    check = validate_staged(task)
    assert check.missing_refs == [] and check.ok
    assert any("references/extra.md" in n and "no copy has" in n for n in check.notes)


def test_missing_reference_that_a_variant_has_blocks_accept(classic_fleet):
    root = classic_fleet
    write_files(
        profile_home(root, "device") / "skills" / "general" / "conflict", {"references/extra.md": "x\n"}
    )
    lay = set_up(root)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    add_line(staged_md(task), "D_device_only")
    add_line(staged_md(task), "See [extra](references/extra.md).")
    check = validate_staged(task)
    assert check.missing_refs == ["references/extra.md"]
    assert any("references/extra.md" in p and "device:" in p for p in check.problems)
    with pytest.raises(MergeError, match="references/extra.md"):
        accept(task, check, force=True, now=LATER)


def test_name_and_frontmatter_are_checked(classic_fleet):
    lay = set_up(classic_fleet)
    task = conflict_task(lay)
    prepare(task, "empty")
    staged_md(task).write_text(skill_md("other", "Wrong name.", ["A"]), encoding="utf-8")
    assert any("name" in p for p in validate_staged(task).problems)
    staged_md(task).write_text("no frontmatter here\n", encoding="utf-8")
    assert any("frontmatter" in p for p in validate_staged(task).problems)


def test_binary_file_missing_from_staging_is_lost(classic_fleet):
    root = classic_fleet
    write_files(profile_home(root, "device") / "skills" / "general" / "conflict", {"assets/x.bin": b"\0\1\2"})
    lay = set_up(root)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    add_line(staged_md(task), "D_device_only")
    check = validate_staged(task)
    assert [(f.profile, f.file) for f in check.lost_files] == [("device", "assets/x.bin")]
    write_files(task.staging, {"assets/renamed.bin": b"\0\1\2"})
    assert validate_staged(task).lost_files == []


def test_existing_shared_copy_is_retired_into_the_backup(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    shared_dir = lay.shared_root / "general" / "conflict"
    write_files(shared_dir, {"SKILL.md": skill_md("conflict", "Diverged copies.", ["A", "S_shared_only"])})
    GitRepo(lay.shared_root).commit_paths([shared_dir], "add shared conflict")
    prop = build_proposal(discover(root), lay, dir_hash_md5, hub_content_hash, {}, home=root)
    skip(lay, "bundled-edited")
    task = next_conflict(lay, prop)
    assert task.name == "conflict" and "shared" in {v.profile for v in task.variants}
    prepare(task, "shared")
    for line in ("B", "C_default_only", "D_device_only"):
        add_line(staged_md(task), line)
    check = validate_staged(task)
    assert check.lost_lines == []
    res = accept(task, check, now=LATER)
    old = lay.backup_dir(res.backup_id) / "retired" / "shared" / "general" / "conflict" / "SKILL.md"
    assert "S_shared_only" in old.read_text(encoding="utf-8")
    assert "D_device_only" in (shared_dir / "SKILL.md").read_text(encoding="utf-8")


def test_prepare_refuses_existing_staging(classic_fleet):
    lay = set_up(classic_fleet)
    task = conflict_task(lay)
    prepare(task, "variant:default")
    with pytest.raises(MergeError, match="already"):
        prepare(task, "variant:device")


def test_code_span_pattern_is_the_backtick_pattern():
    from commons.core import merge as merge_mod

    tick = chr(96)
    assert tick not in merge_mod._CODE_SPAN.pattern
    expected = "(T+)(?!T).+?(?<!T)\\1(?!T)".replace("T", tick)
    assert merge_mod._CODE_SPAN.pattern.replace("\\x60", tick) == expected
    sample = f"a {tick}[x](references/a.md){tick} b {tick * 2}c{tick}d{tick * 2} e"
    assert merge_mod._CODE_SPAN.sub("", sample) == "a  b  e"
