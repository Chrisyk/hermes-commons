"""Tests for commons.core.restore: surgical undo of a setup from its journal."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from commons.core import apply as apply_mod
from commons.core.backup import tree_sha256
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.restore import REPORT_KEYS_ONLY, plan_restore, restore
from tests.conftest import PROFILES
from tests.fleet import profile_home, skill_md, write_files
from tests.test_core_apply import layout_for, run_apply

LATER = datetime(2026, 11, 20, 13, 0, 0, tzinfo=UTC)


def snapshot(root: Path) -> dict[str, tuple[str | None, bytes]]:
    return snapshot_of(root, PROFILES)


def snapshot_of(root: Path, profiles) -> dict[str, tuple[str | None, bytes]]:
    out = {}
    for p in profiles:
        home = profile_home(root, p)
        out[p] = (tree_sha256(home / "skills"), (home / "config.yaml").read_bytes())
    return out


def do_restore(root: Path, backup_id: str, copy_new_to=None):
    plan = plan_restore(layout_for(root), backup_id)
    return plan, restore(plan, discover(root), YamlFileWriter(), copy_new_to or {}, now=LATER)


def test_restore_without_other_changes_is_byte_identical(classic_fleet):
    before = snapshot(classic_fleet)
    result = run_apply(classic_fleet)
    assert result.error is None
    assert snapshot(classic_fleet) != before
    plan, res = do_restore(classic_fleet, result.backup_id)
    assert plan.complete and len(plan.configs) == 4 and len(plan.retired) == 8
    assert plan.shared_changes == []
    assert snapshot(classic_fleet) == before
    assert res.reports == []
    shared = classic_fleet / "shared"
    assert (shared / "general" / "same" / "SKILL.md").is_file()  # the shared root is untouched


def test_shared_edit_survives_and_can_be_copied(classic_fleet):
    result = run_apply(classic_fleet)
    shared = classic_fleet / "shared"
    md = shared / "general" / "same" / "SKILL.md"
    edited = skill_md("same", "Identical everywhere.", ["S1", "S2", "learned after setup"])
    md.write_text(edited, encoding="utf-8")
    new = shared / "general" / "fresh" / "SKILL.md"
    new.parent.mkdir(parents=True)
    new.write_text(skill_md("fresh", "New.", ["n"]), encoding="utf-8")
    shared_before = tree_sha256(shared)
    plan, res = do_restore(classic_fleet, result.backup_id, {"same": [], "fresh": ["pikvm"]})
    assert {(c.name, c.status) for c in plan.shared_changes} == {("same", "changed"), ("fresh", "new")}
    assert any("same" in r and "changed since setup" in r for r in res.reports)
    assert tree_sha256(shared) == shared_before
    assert md.read_text(encoding="utf-8") == edited
    copied = classic_fleet / "profiles" / "pikvm" / "skills" / "general" / "fresh" / "SKILL.md"
    assert copied.read_text(encoding="utf-8") == new.read_text(encoding="utf-8")


def test_copy_new_to_puts_edited_skill_into_profile(classic_fleet):
    result = run_apply(classic_fleet)
    md = classic_fleet / "shared" / "general" / "contained" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    plan = plan_restore(layout_for(classic_fleet), result.backup_id)
    # pocket's contained copy goes back too, so put the edit into a profile without one
    (classic_fleet / "profiles" / "spare" / "skills").mkdir(parents=True)
    (classic_fleet / "profiles" / "spare" / "config.yaml").write_text("{}\n", encoding="utf-8")
    res = restore(plan, discover(classic_fleet), YamlFileWriter(), {"contained": ["spare"]}, now=LATER)
    dest = classic_fleet / "profiles" / "spare" / "skills" / "general" / "contained" / "SKILL.md"
    assert dest.read_text(encoding="utf-8").endswith("edited\n")
    assert any("spare: copied shared contained" in d for d in res.done)


def test_copy_new_to_wins_over_put_back(classic_fleet):
    result = run_apply(classic_fleet)
    md = classic_fleet / "shared" / "general" / "contained" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    _, res = do_restore(classic_fleet, result.backup_id, {"contained": ["pocket"]})
    dest = classic_fleet / "profiles" / "pocket" / "skills" / "general" / "contained" / "SKILL.md"
    assert dest.read_text(encoding="utf-8").endswith("edited\n")
    assert any("pocket" in r and "exists now" in r for r in res.reports)
    retired = (
        layout_for(classic_fleet).backup_dir(result.backup_id)
        / "retired"
        / "pocket"
        / "general"
        / "contained"
    )
    assert retired.is_dir()  # nothing deleted: the original stays in the backup


def test_config_edited_after_setup_gets_key_level_revert(classic_fleet):
    result = run_apply(classic_fleet)
    cfg = profile_home(classic_fleet, "device") / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["model"] = "kept-edit"
    data["plugins"]["enabled"].append("other")
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    _, res = do_restore(classic_fleet, result.backup_id)
    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert after["model"] == "kept-edit"
    assert after["plugins"]["enabled"] == ["other"]
    assert "create_dir" not in after.get("skills", {})
    assert any(r.startswith(f"device: {REPORT_KEYS_ONLY}") for r in res.reports)


def test_restore_after_forced_step6_failure(classic_fleet, monkeypatch):
    before = snapshot(classic_fleet)
    real = apply_mod.retire
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("disk on fire")
        return real(*args, **kwargs)

    monkeypatch.setattr(apply_mod, "retire", flaky)
    result = run_apply(classic_fleet)
    assert result.error
    monkeypatch.undo()
    plan, res = do_restore(classic_fleet, result.backup_id)
    assert not plan.complete and len(plan.retired) == 1
    assert len([d for d in res.done if "put back" in d]) == 1
    assert snapshot(classic_fleet) == before


def test_failure_at_first_retire_restores_only_configs(classic_fleet, monkeypatch):
    before = snapshot(classic_fleet)

    def boom(*args, **kwargs):
        raise OSError("no retire today")

    monkeypatch.setattr(apply_mod, "retire", boom)
    result = run_apply(classic_fleet)
    assert result.error and "no retire today" in result.error
    plan, res = do_restore(classic_fleet, result.backup_id)
    assert plan.retired == [] and len(plan.configs) == 4
    assert [d for d in res.done if "put back" in d] == []
    assert snapshot(classic_fleet) == before


def test_cron_restored_or_left_alone(classic_fleet):
    root = classic_fleet
    old = profile_home(root, "pikvm") / "skills" / "general" / "same"
    jobs = profile_home(root, "pikvm") / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"jobs": [{"id": "j", "prompt": f"{old}/SKILL.md"}]}), encoding="utf-8")
    original = jobs.read_bytes()
    result = run_apply(root)
    assert jobs.read_bytes() != original
    _, res = do_restore(root, result.backup_id)
    assert jobs.read_bytes() == original
    # second round: the job file changes after setup and is left alone
    result = run_apply(root)
    data = json.loads(jobs.read_text(encoding="utf-8"))
    data["jobs"].append({"id": "k", "prompt": "new job"})
    jobs.write_text(json.dumps(data), encoding="utf-8")
    changed = jobs.read_bytes()
    _, res = do_restore(root, result.backup_id)
    assert jobs.read_bytes() == changed
    assert any("changed after setup; left alone" in r for r in res.reports)


def test_shadowed_after_lists_profiles_that_still_read_shared(fleet, tmp_path):
    files = {"SKILL.md": skill_md("same", "d", ["S"])}
    root = fleet({"default": {"general/same": dict(files)}, "device": {"general/same": dict(files)}})
    shared = root / "shared"
    write_files(shared / "general" / "same", files)
    # default already read the shared root before setup; device did not
    cfg = profile_home(root, "default") / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"skills": {"create_dir": str(shared)}, "plugins": {"enabled": []}}), encoding="utf-8"
    )
    before = snapshot_of(root, ("default", "device"))
    result = run_apply(root)
    assert result.error is None, result.error
    plan, res = do_restore(root, result.backup_id)
    assert plan.shadowed_after == [("default", "same")]
    assert any("default: the restored same shadows" in r for r in res.reports)
    assert snapshot_of(root, ("default", "device")) == before


def test_bad_copy_new_to_is_refused(classic_fleet):
    result = run_apply(classic_fleet)
    plan = plan_restore(layout_for(classic_fleet), result.backup_id)
    for bad in ({"nope": ["pikvm"]}, {"same": ["ghost"]}):
        with pytest.raises(ValueError):
            restore(plan, discover(classic_fleet), YamlFileWriter(), bad, now=LATER)
