"""Tests for commons.core.verify: what is wrong with a fleet after setup."""

from __future__ import annotations

import shutil
from pathlib import Path

from commons.core.fleet import discover
from commons.core.layout import Layout
from commons.core.verify import resolvable_names, verify
from tests.fleet import dir_hash_md5, hub_content_hash, skill_md, write_files
from tests.test_core_apply import layout_for, run_apply


def _verify(root: Path, before, overrides=(), layout: Layout | None = None) -> list[str]:
    return verify(
        before, discover(root), layout or layout_for(root), dir_hash_md5, hub_content_hash, overrides
    )


def _before(root: Path):
    return resolvable_names(discover(root), dir_hash_md5, hub_content_hash)


def test_resolvable_names_skip_ambiguous(classic_fleet):
    names = _before(classic_fleet)
    assert "dup" not in names["device"]
    assert {"same", "contained", "conflict", "linked", "hubskill"} <= names["default"]


def test_clean_after_apply(classic_fleet):
    before = _before(classic_fleet)
    assert run_apply(classic_fleet).error is None
    assert _verify(classic_fleet, before) == []


def test_stray_local_copy_is_one_problem(classic_fleet):
    before = _before(classic_fleet)
    run_apply(classic_fleet)
    write_files(
        classic_fleet / "profiles" / "pocket" / "skills" / "x" / "same",
        {"SKILL.md": skill_md("same", "d", ["stray"])},
    )
    problems = _verify(classic_fleet, before)
    assert len(problems) == 1
    assert "pocket" in problems[0] and "same" in problems[0] and "shadow" in problems[0]


def test_intentional_override_is_no_problem(classic_fleet):
    before = _before(classic_fleet)
    run_apply(classic_fleet)
    write_files(
        classic_fleet / "profiles" / "pocket" / "skills" / "x" / "same",
        {"SKILL.md": skill_md("same", "d", ["mine"])},
    )
    assert _verify(classic_fleet, before, ["pocket:same"]) == []
    assert _verify(classic_fleet, before, [{"profile": "pocket", "name": "same"}]) == []
    assert len(_verify(classic_fleet, before, ["device:same"])) == 1


def test_lost_name_is_a_problem(classic_fleet):
    before = _before(classic_fleet)
    run_apply(classic_fleet)
    shutil.rmtree(classic_fleet / "shared" / "general" / "contained")
    problems = _verify(classic_fleet, before)
    assert len(problems) == 4
    assert all("contained" in p and "no longer resolves" in p for p in problems)


def test_ambiguity_in_shared_root_is_a_problem(classic_fleet):
    before = _before(classic_fleet)
    run_apply(classic_fleet)
    write_files(classic_fleet / "shared" / "other" / "same", {"SKILL.md": skill_md("same", "d", ["twin"])})
    problems = _verify(classic_fleet, before)
    # every profile loses 'same' (ambiguous now) and sees the ambiguity
    assert sum("is ambiguous in the shared root" in p for p in problems) == 4
    assert sum("no longer resolves" in p for p in problems) == 4


def test_state_dir_inside_a_skills_root_is_a_problem(classic_fleet):
    before = _before(classic_fleet)
    run_apply(classic_fleet)
    bad = Layout(classic_fleet / "shared", classic_fleet / "shared" / "state")
    problems = _verify(classic_fleet, before, layout=bad)
    assert len(problems) == 1 and "state dir" in problems[0]
