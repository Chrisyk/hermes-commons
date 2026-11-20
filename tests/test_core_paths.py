"""Tests for commons.core.paths: stale skill paths in cron prompts and shared markdown."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from commons.core.fleet import discover
from commons.core.paths import (
    Finding,
    find_stale_paths,
    load_jobs_file,
    rewrite,
    rewrite_backup_path,
)
from tests.fleet import skill_md, write_files


def _jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"jobs": jobs, "updated_at": "2026-11-20T00:00:00"}, indent=2), encoding="utf-8"
    )


@pytest.fixture
def setup(classic_fleet, tmp_path):
    """Pretend 'same' and 'contained' were moved from default into the shared root."""
    shared = tmp_path / "shared"
    for rel in ("general/same", "general/contained"):
        src = classic_fleet / "skills" / rel
        for f in src.rglob("*"):
            if f.is_file():
                write_files(shared / rel, {str(f.relative_to(src)): f.read_bytes()})
    moved = {classic_fleet / "skills" / r: shared / r for r in ("general/same", "general/contained")}
    return classic_fleet, shared, moved


def test_cron_prompt_and_reference_file(setup, tmp_path):
    root, shared, moved = setup
    old = root / "skills" / "general" / "same"
    jobs_path = root / "cron" / "jobs.json"
    prompt = f"Read {old}/SKILL.md, then {old}/notes.txt."
    _jobs(jobs_path, [{"id": "j1", "prompt": prompt, "skills": ["same"]}])
    ref = shared / "general" / "contained" / "references" / "see.md"
    write_files(ref.parent, {"see.md": f"See `{old}/notes.txt` and {old}.\n"})
    profiles = discover(root)
    _, jobs = load_jobs_file(jobs_path)
    found = find_stale_paths(profiles, shared, moved, {"default": jobs}, home=tmp_path)
    cron = [f for f in found if f.kind == "cron"]
    files = [f for f in found if f.kind == "skill-file"]
    assert [(f.old, f.new) for f in cron] == [
        (f"{old}/SKILL.md", f"{shared}/general/same/SKILL.md"),
        (f"{old}/notes.txt", f"{shared}/general/same/notes.txt"),
    ]
    assert all(f.new_exists and f.job_id == "j1" and f.field == "prompt" for f in cron)
    assert {(f.old, f.new) for f in files} == {
        (f"{old}/notes.txt", f"{shared}/general/same/notes.txt"),
        (str(old), f"{shared}/general/same"),
    }
    assert all(f.path == ref for f in files)
    before_jobs, before_ref = jobs_path.read_bytes(), ref.read_bytes()
    backup = tmp_path / "state" / "backups" / "b1"
    done, skipped = rewrite(found, backup, {"default": jobs_path})
    assert len(done) == 4 and skipped == []
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert data["jobs"][0]["prompt"] == (
        f"Read {shared}/general/same/SKILL.md, then {shared}/general/same/notes.txt."
    )
    assert data["jobs"][0]["skills"] == ["same"]
    assert ref.read_text(encoding="utf-8") == (
        f"See `{shared}/general/same/notes.txt` and {shared}/general/same.\n"
    )
    assert rewrite_backup_path(backup, cron[0]).read_bytes() == before_jobs
    assert rewrite_backup_path(backup, files[0]).read_bytes() == before_ref


def test_tilde_form_is_matched_and_kept(setup):
    root, shared, moved = setup
    home = root.parent
    tilde_old = "~/" + (root / "skills" / "general" / "same").relative_to(home).as_posix()
    jobs = [{"id": "j", "prompt": f"open {tilde_old}/SKILL.md now"}]
    found = find_stale_paths(discover(root), shared, moved, {"default": jobs}, home=home)
    assert [(f.old, f.new) for f in found] == [
        (f"{tilde_old}/SKILL.md", "~/" + (shared / "general/same/SKILL.md").relative_to(home).as_posix())
    ]


def test_missing_target_is_reported_not_rewritten(setup, tmp_path):
    root, shared, moved = setup
    old = root / "skills" / "general" / "same"
    jobs_path = root / "cron" / "jobs.json"
    _jobs(jobs_path, [{"id": "j1", "prompt": f"use {old}/gone.md"}])
    before = jobs_path.read_bytes()
    _, jobs = load_jobs_file(jobs_path)
    found = find_stale_paths(discover(root), shared, moved, {"default": jobs}, home=tmp_path)
    assert len(found) == 1 and not found[0].new_exists
    done, skipped = rewrite(found, tmp_path / "b", {"default": jobs_path})
    assert done == [] and skipped == found
    assert jobs_path.read_bytes() == before
    assert not (tmp_path / "b").exists()


def test_component_boundaries(setup, tmp_path):
    root, shared, moved = setup
    old = root / "skills" / "general" / "same"
    text = " ".join(
        [f"{old}bar/SKILL.md", f"{old}-x", f"/x{old}", f"{old}.bak", f"{old}/SKILL.md.", f"({old})"]
    )
    found = find_stale_paths(
        discover(root), shared, moved, {"default": [{"id": "j", "prompt": text}]}, home=tmp_path
    )
    assert [f.old for f in found] == [f"{old}/SKILL.md", str(old)]


def test_foo_never_matches_inside_foobar(tmp_path):
    a = tmp_path / "a" / "skills"
    write_files(tmp_path / "new" / "foo", {"SKILL.md": "x\n"})
    moved = {a / "foo": tmp_path / "new" / "foo"}
    jobs = {"default": [{"id": "j", "prompt": f"{a}/foobar/SKILL.md {a}/foo/SKILL.md"}]}
    home = tmp_path / "h"
    (home / "skills").mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    found = find_stale_paths(discover(home), tmp_path / "none", moved, jobs, home=tmp_path)
    assert [f.old for f in found] == [f"{a}/foo/SKILL.md"]


def test_git_dir_and_other_profiles_are_ignored(setup, tmp_path):
    root, shared, moved = setup
    old = root / "skills" / "general" / "same"
    write_files(shared / ".git", {"notes.md": f"{old}\n"})
    jobs = {"ghost": [{"id": "j", "prompt": str(old)}]}
    assert find_stale_paths(discover(root), shared, moved, jobs, home=tmp_path) == []


def test_exists_hook_and_missing_cron_path(setup, tmp_path):
    root, shared, moved = setup
    old = root / "skills" / "general" / "same"
    jobs = {"device": [{"id": "j", "prompt": f"{old}/SKILL.md"}]}
    found = find_stale_paths(discover(root), shared, moved, jobs, home=tmp_path, exists=lambda p: False)
    assert [f.new_exists for f in found] == [False]
    real = find_stale_paths(discover(root), shared, moved, jobs, home=tmp_path)
    done, skipped = rewrite(real, tmp_path / "b", {})
    assert done == [] and skipped == real


def test_rewrite_keeps_mode_and_bare_list_shape(setup, tmp_path):
    root, shared, moved = setup
    old = root / "skills" / "general" / "same"
    jobs_path = root / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(json.dumps([{"id": 7, "prompt": f"{old}/SKILL.md"}]), encoding="utf-8")
    os.chmod(jobs_path, 0o640)
    _, jobs = load_jobs_file(jobs_path)
    found = find_stale_paths(discover(root), shared, moved, {"default": jobs}, home=tmp_path)
    done, _ = rewrite(found, tmp_path / "b", {"default": jobs_path})
    assert len(done) == 1
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data[0]["prompt"] == f"{shared}/general/same/SKILL.md"
    assert jobs_path.stat().st_mode & 0o777 == 0o640


def test_finding_is_frozen():
    f = Finding("cron", "x", "a", "b", True)
    with pytest.raises(AttributeError):
        f.old = "c"  # type: ignore[misc]


def test_skill_file_in_moved_skill_md(setup, tmp_path):
    root, shared, moved = setup
    old = root / "skills" / "general" / "contained"
    md = shared / "general" / "same" / "SKILL.md"
    md.write_text(skill_md("same", "d", [f"see {old}/SKILL.md"]), encoding="utf-8")
    found = find_stale_paths(discover(root), shared, moved, {}, home=tmp_path)
    assert [(f.path, f.new_exists) for f in found] == [(md, True)]
