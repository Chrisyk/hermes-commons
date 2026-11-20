"""Pristine bundled/hub copies of a name that is taken over are retired with it (design decision 5).

Phase 6 verify found shared copies shadowed by pristine copies in other
profiles: a group mixing edited and pristine copies moved the edited one
and left the pristine ones local, where the local tier wins.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from commons.core.backup import tree_sha256
from commons.core.check import run_check
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.merge import accept, next_conflict, prepare, validate_staged
from commons.core.promote import promote
from commons.core.proposal import apply_choice
from commons.core.resolve import TIER_CREATE_DIR, resolve
from commons.core.restore import plan_restore, restore
from commons.core.verify import resolvable_names, verify
from tests.conftest import PROFILES, _classic_spec
from tests.fleet import dir_hash_md5, hub_content_hash, make_fleet, profile_home, skill_md, write_files
from tests.test_core_apply import layout_for, propose, run_apply

LATER = datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)


def _skills(root, p):
    return profile_home(root, p) / "skills"


def _state(root):
    return {p: tree_sha256(_skills(root, p)) for p in PROFILES}


def _taken(lay):
    data = json.loads(lay.config_path.read_text(encoding="utf-8"))
    return {(t["name"], t["profile"], t["kind"]) for t in data["taken_over"]}


def _shared_wins(root, lay, name):
    for p in discover(root):
        r = resolve(p, dir_hash_md5, hub_content_hash)[name]
        assert r.winner_tier == TIER_CREATE_DIR and r.winner_path.is_relative_to(lay.shared_root), p.name


def test_keep_choice_on_classic_fleet_retires_pristine_copies_and_verifies_clean(classic_fleet):
    root = classic_fleet
    before_names = resolvable_names(discover(root), dir_hash_md5, hub_content_hash)
    before = _state(root)
    prop = apply_choice(propose(root), "bundled-edited=keep:pocket")
    result = run_apply(root, prop)
    assert result.error is None, result.error
    lay = layout_for(root)
    for p in PROFILES:
        assert not (_skills(root, p) / "bundled" / "bundled-edited").exists(), p
        assert (_skills(root, p) / "bundled" / "bundled-pristine").is_dir(), p  # all-pristine: stays
    _shared_wins(root, lay, "bundled-edited")
    assert {
        ("bundled-edited", "default", "bundled-pristine"),
        ("bundled-edited", "device", "bundled-pristine"),
    } <= (_taken(lay))
    assert verify(before_names, discover(root), lay, dir_hash_md5, hub_content_hash) == []
    plan = plan_restore(lay, result.backup_id)
    out = restore(plan, discover(root), YamlFileWriter(), now=LATER)
    assert out.reports == []
    assert _state(root) == before


def _mixed_fleet(tmp_path):
    """classic_fleet plus 'mixed': edited identically in pikvm and pocket, pristine bundled elsewhere."""
    spec = _classic_spec()
    origin = {"SKILL.md": skill_md("mixed", "Bundled.", ["m1", "m2"])}
    edited = {"SKILL.md": skill_md("mixed", "Bundled.", ["m1", "m2", "local edit"])}
    for p in PROFILES:
        spec[p]["bundled/mixed"] = dict(edited if p in ("pikvm", "pocket") else origin)
        spec["_bundled"][p]["mixed"] = origin
    return make_fleet(tmp_path, spec)


def test_setup_move_retires_pristine_copies_and_check_tracks_them(tmp_path):
    root = _mixed_fleet(tmp_path)
    before_names = resolvable_names(discover(root), dir_hash_md5, hub_content_hash)
    result = run_apply(root)
    assert result.error is None, result.error
    lay = layout_for(root)
    _shared_wins(root, lay, "mixed")
    assert {
        ("mixed", p, k) for p, k in [("default", "bundled-pristine"), ("pikvm", "bundled-edited")]
    } <= _taken(lay)
    assert verify(before_names, discover(root), lay, dir_hash_md5, hub_content_hash) == []
    newer = [
        f for f in run_check(discover(root), lay, bundled_index={"mixed": "md5:new"}) if f.skill == "mixed"
    ]
    assert newer and all(f.code == "upstream-newer" for f in newer)


def _merge_bundled_edited(lay):
    task = next_conflict(lay, None)
    assert task.name == "bundled-edited"
    assert {c.profile for c in task.pinned} == {"default", "device"}
    prepare(task, "variant:pikvm")
    md = task.staging / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "extra line from pocket\n", encoding="utf-8")
    check = validate_staged(task)
    assert check.ok, check
    return accept(task, check, now=LATER)


def test_merge_accept_retires_pristine_copies(classic_fleet):
    root = classic_fleet
    before_names = resolvable_names(discover(root), dir_hash_md5, hub_content_hash)
    assert run_apply(root).error is None
    after_setup = _state(root)
    lay = layout_for(root)
    res = _merge_bundled_edited(lay)
    assert res.kept == []
    for p in PROFILES:
        assert not (_skills(root, p) / "bundled" / "bundled-edited").exists(), p
    retired = lay.backup_dir(res.backup_id) / "retired"
    assert (retired / "default" / "bundled" / "bundled-edited" / "SKILL.md").is_file()
    _shared_wins(root, lay, "bundled-edited")
    assert {("bundled-edited", p) for p in PROFILES} == {(n, p) for n, p, _k in _taken(lay)}
    assert verify(before_names, discover(root), lay, dir_hash_md5, hub_content_hash) == []
    restore(plan_restore(lay, res.backup_id), discover(root), YamlFileWriter(), now=LATER)
    assert _state(root) == after_setup


def test_merge_leaves_a_pristine_copy_edited_since_the_task(classic_fleet):
    root = classic_fleet
    assert run_apply(root).error is None
    lay = layout_for(root)
    md = _skills(root, "device") / "bundled" / "bundled-edited" / "SKILL.md"
    task = next_conflict(lay, None)
    md.write_text(md.read_text(encoding="utf-8") + "edited after the task\n", encoding="utf-8")
    prepare(task, "variant:pikvm")
    staged = task.staging / "SKILL.md"
    staged.write_text(staged.read_text(encoding="utf-8") + "extra line from pocket\n", encoding="utf-8")
    res = accept(task, validate_staged(task), now=LATER)
    assert md.is_file()
    assert any("device" in k for k in res.kept)
    assert not (_skills(root, "default") / "bundled" / "bundled-edited").exists()


def test_promote_retires_pristine_copies_in_profiles_that_read_the_shared_root(classic_fleet):
    root = classic_fleet
    assert run_apply(root).error is None
    lay = layout_for(root)
    # bundled-pristine stayed local everywhere; pocket edits its copy and promotes it.
    md = _skills(root, "pocket") / "bundled" / "bundled-pristine" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "pocket edit\n", encoding="utf-8")
    res = promote(
        "bundled-pristine", "pocket", lay, discover(root), dir_hash_md5, hub_content_hash, now=LATER
    )
    for p in PROFILES:
        assert not (_skills(root, p) / "bundled" / "bundled-pristine").exists(), p
    _shared_wins(root, lay, "bundled-pristine")
    kinds = {p: k for n, p, k in _taken(lay) if n == "bundled-pristine"}
    assert kinds == {
        "pocket": "bundled-edited",
        "default": "bundled-pristine",
        "device": "bundled-pristine",
        "pikvm": "bundled-pristine",
    }
    assert res.kept == []


def test_promote_keeps_pristine_copies_of_a_profile_outside_the_shared_root(classic_fleet):
    root = classic_fleet
    assert run_apply(root).error is None
    lay = layout_for(root)
    cfg = profile_home(root, "device") / "config.yaml"
    cfg.write_text("skills: {}\nplugins:\n  enabled: []\n", encoding="utf-8")
    write_files(_skills(root, "pocket") / "general" / "fresh", {"SKILL.md": skill_md("fresh", "d", ["f"])})
    md = _skills(root, "pocket") / "bundled" / "bundled-pristine" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "pocket edit\n", encoding="utf-8")
    promote("bundled-pristine", "pocket", lay, discover(root), dir_hash_md5, hub_content_hash, now=LATER)
    assert (_skills(root, "device") / "bundled" / "bundled-pristine").is_dir()
    assert not (_skills(root, "default") / "bundled" / "bundled-pristine").exists()
