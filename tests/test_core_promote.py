"""Tests for commons.core.promote: moving one skill into or out of the shared root."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from commons.core.backup import tree_sha256
from commons.core.check import run_check
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.promote import PromoteError, demote, promote
from commons.core.resolve import TIER_CREATE_DIR, TIER_LOCAL, resolve
from commons.core.restore import plan_restore, restore
from tests.conftest import PROFILES
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files
from tests.test_core_apply import layout_for, run_apply

LATER = datetime(2026, 11, 20, 15, 0, 0, tzinfo=UTC)
FRESH = {"SKILL.md": skill_md("fresh", "New in pocket.", ["F1", "F2"]), "references/r.md": "ref\n"}


def set_up(root):
    result = run_apply(root)
    assert result.error is None, result.error
    return layout_for(root)


def skills(root, profile):
    return profile_home(root, profile) / "skills"


def do_promote(root, name, profile):
    return promote(name, profile, layout_for(root), discover(root), dir_hash_md5, hub_content_hash, now=LATER)


def do_demote(root, name, profile):
    return demote(name, profile, layout_for(root), discover(root), now=LATER)


def test_promote_then_other_profiles_resolve_the_shared_copy(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    write_files(skills(root, "pocket") / "general" / "fresh", FRESH)
    res = do_promote(root, "fresh", "pocket")
    shared_md = lay.shared_root / "general" / "fresh" / "SKILL.md"
    assert shared_md.is_file() and (shared_md.parent / "references" / "r.md").is_file()
    assert not (skills(root, "pocket") / "general" / "fresh").exists()
    retired = lay.backup_dir(res.backup_id) / "retired" / "pocket" / "general" / "fresh" / "SKILL.md"
    assert retired.is_file()
    for p in discover(root):
        r = resolve(p, dir_hash_md5, hub_content_hash)["fresh"]
        assert r.winner_tier == TIER_CREATE_DIR and r.winner_path == shared_md, p.name
    assert res.others == []
    assert GitRepo(lay.shared_root).log(n=1)[0][2] == "promote fresh from pocket"


def test_promote_reports_other_copies_as_shadowed(classic_fleet):
    root = classic_fleet
    set_up(root)
    write_files(skills(root, "pocket") / "general" / "fresh", FRESH)
    write_files(skills(root, "pikvm") / "x" / "fresh", {"SKILL.md": skill_md("fresh", "Own.", ["P"])})
    res = do_promote(root, "fresh", "pocket")
    assert res.others == ["pikvm"]
    shadowed = [f for f in run_check(discover(root), layout_for(root), home=root) if f.code == "shadowed"]
    assert [(f.profile, f.skill) for f in shadowed] == [("pikvm", "fresh")]


def test_promote_refused_on_differing_shared_copy(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    write_files(skills(root, "pikvm") / "general" / "same", {"SKILL.md": skill_md("same", "Mine.", ["P"])})
    head = GitRepo(lay.shared_root).head()
    with pytest.raises(PromoteError, match="merge"):
        do_promote(root, "same", "pikvm")
    assert (skills(root, "pikvm") / "general" / "same").is_dir()
    assert GitRepo(lay.shared_root).head() == head


def test_promote_of_an_identical_copy_only_retires_it(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    shared_same = lay.shared_root / "general" / "same"
    write_files(skills(root, "pikvm") / "general" / "same", {})
    for f in shared_same.rglob("*"):
        if f.is_file():
            write_files(
                skills(root, "pikvm") / "general" / "same",
                {f.relative_to(shared_same).as_posix(): f.read_bytes()},
            )
    res = do_promote(root, "same", "pikvm")
    assert res.sha is None and not (skills(root, "pikvm") / "general" / "same").exists()


def test_promote_unknown_skill(classic_fleet):
    set_up(classic_fleet)
    with pytest.raises(PromoteError, match="no own copy"):
        do_promote(classic_fleet, "nothing-here", "pocket")


def test_demote_creates_override_and_check_stops_flagging(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    res = do_demote(root, "same", "pikvm")
    local = skills(root, "pikvm") / "general" / "same"
    assert tree_sha256(local) == tree_sha256(lay.shared_root / "general" / "same")
    data = json.loads(lay.config_path.read_text(encoding="utf-8"))
    assert {"name": "same", "profile": "pikvm"} in data["intentional_overrides"]
    r = resolve(discover(root)[2], dir_hash_md5, hub_content_hash)["same"]
    assert r.winner_tier == TIER_LOCAL
    assert [f for f in run_check(discover(root), lay, home=root) if f.code == "shadowed"] == []
    assert GitRepo(lay.shared_root).status_porcelain() == []
    assert res.path == local
    with pytest.raises(PromoteError, match="already"):
        do_demote(root, "same", "pikvm")


def test_promote_is_undone_by_restore(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    write_files(skills(root, "pocket") / "general" / "fresh", FRESH)
    before = tree_sha256(skills(root, "pocket"))
    res = do_promote(root, "fresh", "pocket")
    plan = plan_restore(lay, res.backup_id)
    restore(plan, discover(root), YamlFileWriter(), now=LATER)
    assert tree_sha256(skills(root, "pocket")) == before


def test_demote_is_undone_by_restore(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    before = {p: tree_sha256(skills(root, p)) for p in PROFILES}
    res = do_demote(root, "same", "pikvm")
    plan = plan_restore(lay, res.backup_id)
    out = restore(plan, discover(root), YamlFileWriter(), now=LATER)
    assert {p: tree_sha256(skills(root, p)) for p in PROFILES} == before
    aside = out.pre_restore_dir / "displaced" / "pikvm" / "general" / "same" / "SKILL.md"
    assert aside.is_file()  # moved aside, never deleted
    r = resolve(discover(root)[2], dir_hash_md5, hub_content_hash)["same"]
    assert r.winner_tier == TIER_CREATE_DIR
