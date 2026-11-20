"""Proposal safety: old create_dir roots, shared skills dirs, keep:P targets, a changed fleet."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from commons.core.apply import apply, intents, load_journal
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.proposal import Proposal, apply_choice, render_markdown
from commons.core.restore import plan_restore, restore
from commons.core.verify import resolvable_names, verify
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files
from tests.test_core_apply import NOW, layout_for, propose, run_apply
from tests.test_core_crash import state
from tests.test_core_restore import LATER


def set_create_dir(root: Path, profile: str, value: Path) -> None:
    cfg = profile_home(root, profile) / "config.yaml"
    data = yaml.safe_load(cfg.read_text()) or {}
    data.setdefault("skills", {})["create_dir"] = str(value)
    cfg.write_text(yaml.safe_dump(data))


def test_create_dir_elsewhere_is_adopted_not_retired(classic_fleet):
    root = classic_fleet
    other = root / "myskills"
    write_files(other / "x/only", {"SKILL.md": skill_md("only", "d", ["o"])})
    (other / "general/same").mkdir(parents=True)
    for f in (root / "skills/general/same").iterdir():
        (other / "general/same" / f.name).write_bytes(f.read_bytes())
    set_create_dir(root, "device", other)
    profiles = discover(root)
    before = resolvable_names(profiles, dir_hash_md5, hub_content_hash)
    prop = propose(root)
    assert prop.roots == {"device:create_dir": other}
    moves = {m.name: m for m in prop.moves}
    assert (moves["only"].source_profile, moves["only"].target_rel, moves["only"].retire) == (
        "device:create_dir",
        "x/only",
        [],
    )
    assert all(not p.endswith(":create_dir") for m in prop.moves for p, _ in m.retire)
    assert set(prop.left_in_place) == {"device:create_dir:x/only", "device:create_dir:general/same"}
    assert "left in place at old create_dir" in render_markdown(prop)
    prop = Proposal.from_json(prop.to_json())
    r = run_apply(root, prop)
    assert r.error is None and r.problems == []
    assert (root / "shared/x/only/SKILL.md").is_file()
    assert (other / "x/only/SKILL.md").is_file() and (other / "general/same/SKILL.md").is_file()
    assert verify(before, discover(root), layout_for(root), dir_hash_md5, hub_content_hash) == []


def test_create_dir_conflict_is_noted(classic_fleet):
    root = classic_fleet
    other = root / "myskills"
    write_files(other / "general/same", {"SKILL.md": skill_md("same", "d", ["different"])})
    set_create_dir(root, "device", other)
    prop = propose(root)
    assert "same" in {c.name for c in prop.conflicts}
    assert any("device: same at its old skills.create_dir" in n for n in prop.notes)


def test_symlinked_shared_skills_dir(classic_fleet):
    root = classic_fleet
    pocket = profile_home(root, "pocket") / "skills"
    shutil.rmtree(pocket)
    pocket.symlink_to(profile_home(root, "pikvm") / "skills")
    before = state(root)
    prop = propose(root)
    assert prop.aliases == {"pocket": "pikvm"}
    assert "shared skills dir: pocket uses pikvm's folder" in prop.notes
    for m in prop.moves:
        assert len({p for p, _ in m.retire}) == len(m.retire)
        assert "pocket" not in {p for p, _ in m.retire}
    r = run_apply(root, prop)
    assert r.error is None, r.error
    journal = load_journal(layout_for(root), r.backup_id)
    srcs = [i["args"]["src"] for i in intents(journal, "retire")]
    assert len(srcs) == len(set(srcs))
    plan = plan_restore(layout_for(root), r.backup_id)
    res = restore(plan, discover(root), YamlFileWriter(), now=LATER)
    assert state(root) == before, res.reports


def test_external_dir_equal_to_shared_root_is_a_note(classic_fleet):
    root = classic_fleet
    cfg = profile_home(root, "pikvm") / "config.yaml"
    data = yaml.safe_load(cfg.read_text()) or {}
    data.setdefault("skills", {})["external_dirs"] = [str(root / "shared")]
    cfg.write_text(yaml.safe_dump(data))
    prop = propose(root)
    assert any("pikvm: skills.external_dirs already lists the shared root" in n for n in prop.notes)


def _shared_skill(root: Path, rel: str, name: str) -> None:
    write_files(root / "shared" / rel, {"SKILL.md": skill_md(name, "d", ["x"])})


def test_keep_onto_claimed_target_is_refused(classic_fleet):
    _shared_skill(classic_fleet, "general/conflict/sub", "other")
    prop = Proposal.from_json(propose(classic_fleet).to_json())
    assert ("general/conflict/sub", "other") in prop.shared_claimed
    with pytest.raises(ValueError, match="overlaps other in the shared root"):
        apply_choice(prop, "conflict=keep:device")


def test_keep_nested_inside_another_skill_is_refused(classic_fleet):
    _shared_skill(classic_fleet, "general", "umbrella")
    prop = propose(classic_fleet)
    with pytest.raises(ValueError, match="overlaps umbrella"):
        apply_choice(prop, "conflict=keep:device")


def test_keep_onto_reserved_top_is_refused(classic_fleet):
    prop = propose(classic_fleet)
    conflict = next(c for c in prop.conflicts if c.name == "conflict")
    for v in conflict.variants:
        v.rel = ".git/conflict"
    with pytest.raises(ValueError, match="not a place a skill can land"):
        apply_choice(prop, "conflict=keep:device")


def test_profile_homes_mismatch_is_refused(classic_fleet):
    prop = propose(classic_fleet)
    prop.profile_homes["pikvm"] = classic_fleet / "elsewhere"
    r = apply(prop, discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW)
    assert r.error and "profile homes differ" in r.error and r.backup_id is None


def test_extra_profile_is_refused(classic_fleet):
    prop = propose(classic_fleet)
    del prop.profile_homes["pocket"]
    r = apply(prop, discover(classic_fleet), layout_for(classic_fleet), YamlFileWriter(), NOW)
    assert r.error and "not in the proposal: pocket" in r.error


def test_to_json_is_ascii(classic_fleet):
    write_files(classic_fleet / "skills/general/日本", {"SKILL.md": skill_md("日本", "d", ["x"])})
    text = propose(classic_fleet).to_json()
    assert text.isascii()
    assert "日本" in {m.name for m in Proposal.from_json(text).moves}
