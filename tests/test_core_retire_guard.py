"""A profile copy is retired only when the profile loads the name from the shared root afterwards."""

from __future__ import annotations

from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.merge import accept, next_conflict, prepare, saved_proposal, skip, validate_staged
from commons.core.promote import promote
from commons.core.proposal import build_proposal
from commons.core.restore import plan_restore, restore
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files
from tests.test_core_apply import layout_for, run_apply
from tests.test_core_restore import LATER

FRESH = {"SKILL.md": skill_md("fresh", "New in pocket.", ["F1", "F2"])}


def set_up_then_restore(root):
    res = run_apply(root)
    assert res.error is None, res.error
    lay = layout_for(root)
    restore(plan_restore(lay, res.backup_id), discover(root), YamlFileWriter(), now=LATER)
    for p in discover(root):
        assert p.create_dir() is None, p.name
    return lay, res.backup_id


def test_saved_proposal_skips_a_restored_setup(classic_fleet):
    lay, _bid = set_up_then_restore(classic_fleet)
    assert saved_proposal(lay) is None
    assert next_conflict(lay) is None


def test_accept_after_restore_leaves_local_copies(classic_fleet):
    root = classic_fleet
    lay, _bid = set_up_then_restore(root)
    prop = build_proposal(discover(root), lay, dir_hash_md5, hub_content_hash, {}, home=root)
    skip(lay, "bundled-edited")
    task = next_conflict(lay, prop)
    assert task.name == "conflict"
    prepare(task, "variant:default")
    md = task.staging / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "D_device_only\n", encoding="utf-8")
    check = validate_staged(task)
    assert check.ok, check
    res = accept(task, check, now=LATER)
    for profile in ("default", "device"):
        assert (profile_home(root, profile) / "skills" / "general" / "conflict" / "SKILL.md").is_file()
        assert f"{profile} does not use the shared root; its copy was left in place" in res.kept
    assert (lay.shared_root / "general" / "conflict" / "SKILL.md").is_file()


def test_promote_after_restore_leaves_the_local_copy(classic_fleet):
    root = classic_fleet
    lay, _bid = set_up_then_restore(root)
    local = profile_home(root, "pocket") / "skills" / "general" / "fresh"
    write_files(local, FRESH)
    res = promote("fresh", "pocket", lay, discover(root), dir_hash_md5, hub_content_hash, LATER)
    assert (local / "SKILL.md").is_file()
    assert res.kept == ["pocket does not use the shared root; its copy was left in place"]
    assert GitRepo(lay.shared_root).log(n=1)[0][2] == "promote fresh from pocket"


def test_promote_after_setup_still_retires_the_local_copy(classic_fleet):
    root = classic_fleet
    res = run_apply(root)
    assert res.error is None
    lay = layout_for(root)
    write_files(profile_home(root, "pocket") / "skills" / "general" / "fresh", FRESH)
    out = promote("fresh", "pocket", lay, discover(root), dir_hash_md5, hub_content_hash, LATER)
    assert out.kept == []
    assert not (profile_home(root, "pocket") / "skills" / "general" / "fresh").exists()
