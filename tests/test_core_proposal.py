"""Tests for commons.core.proposal: what setup would do on a fleet."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from commons.core.fleet import discover
from commons.core.layout import Layout
from commons.core.proposal import (
    ConfigChange,
    Proposal,
    apply_choice,
    build_proposal,
    render_markdown,
)
from tests.conftest import PROFILES
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files


def _layout(root: Path) -> Layout:
    return Layout(shared_root=root / "shared", state_dir=root / "commons")


def _propose(root: Path, cron=None, layout: Layout | None = None) -> Proposal:
    lay = layout or _layout(root)
    return build_proposal(discover(root), lay, dir_hash_md5, hub_content_hash, cron or {}, home=root)


def _moves(p: Proposal) -> dict:
    return {m.name: m for m in p.moves}


def test_classic_moves(classic_fleet):
    p = _propose(classic_fleet)
    moves = _moves(p)
    assert set(moves) == {"same", "contained"}
    same = moves["same"]
    assert (same.source_profile, same.source_rel, same.target_rel) == (
        "default",
        "general/same",
        "general/same",
    )
    assert same.retire == [(pr, "general/same") for pr in PROFILES]
    contained = moves["contained"]
    assert (contained.source_profile, contained.target_rel) == ("device", "general/contained")
    assert contained.retire == [(pr, "general/contained") for pr in PROFILES]


def test_classic_conflicts_and_left_alone(classic_fleet):
    p = _propose(classic_fleet)
    conflicts = {c.name: c for c in p.conflicts}
    assert set(conflicts) == {"bundled-edited", "conflict"}
    assert all(c.action == "merge" for c in p.conflicts)
    variants = {(v.profile, v.rel): v.unique_lines for v in conflicts["conflict"].variants}
    assert variants == {("default", "general/conflict"): 1, ("device", "general/conflict"): 1}
    edited = {v.profile for v in conflicts["bundled-edited"].variants}
    assert edited == {"pikvm", "pocket"}
    assert sorted(p.stays_local) == ["bundled-pristine", "hubskill", "linked"]
    assert p.in_profile_dups == ["dup"]
    assert p.unreadable == [] and p.nested == []
    assert p.taken_over == []


def test_classic_summary(classic_fleet):
    s = _propose(classic_fleet).summary
    assert (s.names, s.identical, s.contained, s.conflicts, s.moves, s.retires) == (8, 1, 1, 2, 2, 8)
    assert s.differing == 4  # contained, conflict, bundled-edited, dup


def test_config_changes_for_every_profile(classic_fleet):
    p = _propose(classic_fleet)
    shared = str(classic_fleet / "shared")
    assert set(p.config_changes) == set(PROFILES)
    for changes in p.config_changes.values():
        assert changes == [
            ConfigChange("skills.create_dir", None, shared),
            ConfigChange("plugins.enabled", [], ["commons"]),
        ]


def test_json_round_trip(classic_fleet):
    p = _propose(classic_fleet)
    q = Proposal.from_json(p.to_json())
    assert q == p
    assert q.to_json() == p.to_json()
    r = apply_choice(p, "conflict=keep:device")
    assert Proposal.from_json(r.to_json()) == r


def test_keep_device_turns_conflict_into_move(classic_fleet):
    p = _propose(classic_fleet)
    q = apply_choice(p, "conflict=keep:device")
    assert "conflict" not in {c.name for c in q.conflicts}
    m = _moves(q)["conflict"]
    assert (m.source_profile, m.source_rel, m.target_rel) == (
        "device",
        "general/conflict",
        "general/conflict",
    )
    assert m.retire == [("default", "general/conflict"), ("device", "general/conflict")]
    assert q.summary.moves == 3 and q.summary.conflicts == 1 and q.summary.retires == 10
    assert "conflict" in {c.name for c in p.conflicts}  # the original is untouched


def test_keep_on_bundled_edited_records_take_over(classic_fleet):
    q = apply_choice(_propose(classic_fleet), "bundled-edited=keep:pocket")
    m = _moves(q)["bundled-edited"]
    assert m.source_profile == "pocket"
    # default and device hold the pristine bundled copy: taken over too, so it cannot shadow (decision 5).
    assert m.retire == [(p, "bundled/bundled-edited") for p in PROFILES]
    assert {(t.name, t.profile, t.kind) for t in q.taken_over} == {
        ("bundled-edited", "default", "bundled-pristine"),
        ("bundled-edited", "device", "bundled-pristine"),
        ("bundled-edited", "pikvm", "bundled-edited"),
        ("bundled-edited", "pocket", "bundled-edited"),
    }
    assert all(t.origin_hash for t in q.taken_over)


def test_merge_choice_and_errors(classic_fleet):
    p = _propose(classic_fleet)
    assert apply_choice(p, "conflict=merge") == p
    for bad in ("nope=merge", "conflict=keep:pikvm", "conflict=keep", "conflict", "conflict=drop", "=merge"):
        with pytest.raises(ValueError):
            apply_choice(p, bad)


def test_render_markdown(classic_fleet):
    text = render_markdown(apply_choice(_propose(classic_fleet), "conflict=merge"))
    assert text.index("## Counts") < text.index("## Moves") < text.index("## Conflicts")
    assert "- same: copy default:general/same to general/same" in text
    assert "default:general/conflict: 1 unique lines" in text
    assert "skills.create_dir: (unset) -> " in text
    assert "plugins.enabled: [] -> [commons]" in text
    assert "stays local (pristine bundled or hub, or symlinked): bundled-pristine, hubskill, linked" in text
    tail = text[text.index("## What next") :]
    assert "review" in tail and "apply" in tail and "cancel" in tail and "--choose S=keep:P" in tail
    assert "\u2014" not in text


def test_bundled_edited_identical_copies_move_and_are_taken_over(fleet):
    origin = {"SKILL.md": skill_md("b", "d", ["o"])}
    edited = {"SKILL.md": skill_md("b", "d", ["o", "edit"])}
    root = fleet(
        {
            "default": {"x/b": dict(edited)},
            "device": {"x/b": dict(edited)},
            "_bundled": {"default": {"b": origin}, "device": {"b": origin}},
        }
    )
    p = _propose(root)
    assert set(_moves(p)) == {"b"}
    assert {(t.profile, t.kind) for t in p.taken_over} == {
        ("default", "bundled-edited"),
        ("device", "bundled-edited"),
    }


@pytest.mark.skipif(sys.platform == "win32", reason="the test builds POSIX-style mixed-separator path strings")
def test_cron_rewrites_only_for_existing_files(classic_fleet):
    old = classic_fleet / "profiles" / "pikvm" / "skills" / "general" / "contained"
    jobs = {
        "pikvm": [
            {"id": "a1", "prompt": f"read {old}/SKILL.md and {old}/missing.txt"},
            {"id": "a2", "prompt": "nothing here"},
        ]
    }
    p = _propose(classic_fleet, cron=jobs)
    shared = classic_fleet / "shared"
    assert [(r.profile, r.job_id, r.field, r.old_path, r.new_path) for r in p.cron_rewrites] == [
        ("pikvm", "a1", "prompt", f"{old}/SKILL.md", f"{shared}/general/contained/SKILL.md")
    ]


def _write_config(root: Path, profile: str, config: dict) -> None:
    (profile_home(root, profile) / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_adopt_existing_shared_root(fleet, tmp_path):
    same = {"SKILL.md": skill_md("same", "d", ["S"])}
    other = {"SKILL.md": skill_md("other", "d", ["O"])}
    root = fleet(
        {
            "default": {"general/pinned": {"SKILL.md": skill_md("pinned", "d", ["P"])}},
            "device": {},
            "_bundled": {"default": {"pinned": {"SKILL.md": skill_md("pinned", "d", ["P"])}}},
        }
    )
    shared = tmp_path / "shared"
    write_files(shared / "general" / "same", same)
    write_files(shared / "tools" / "other", other)
    for profile in ("default", "device"):
        _write_config(root, profile, {"skills": {"create_dir": str(shared)}, "plugins": {"enabled": ["x"]}})
    p = _propose(root)
    assert p.moves == [] and p.summary.retires == 0 and p.conflicts == []
    assert p.config_changes == {
        "default": [ConfigChange("plugins.enabled", ["x"], ["x", "commons"])],
        "device": [ConfigChange("plugins.enabled", ["x"], ["x", "commons"])],
    }


def test_shared_copy_is_kept_and_local_copies_retired(fleet, tmp_path):
    files = {"SKILL.md": skill_md("same", "d", ["S"])}
    root = fleet({"default": {"general/same": dict(files)}, "device": {}})
    shared = tmp_path / "shared"
    write_files(shared / "lib" / "same", files)
    m = _moves(_propose(root))["same"]
    assert (m.source_profile, m.target_rel, m.retire) == ("shared", "lib/same", [("default", "general/same")])


def test_shared_copy_contained_in_local_is_conflict(fleet, tmp_path):
    root = fleet({"default": {"general/s": {"SKILL.md": skill_md("s", "d", ["a", "b"])}}})
    write_files(tmp_path / "shared" / "general" / "s", {"SKILL.md": skill_md("s", "d", ["a"])})
    p = _propose(root)
    assert p.moves == []
    c = p.conflicts[0]
    assert c.name == "s" and c.shared_rel == "general/s"
    assert {v.profile for v in c.variants} == {"default", "shared"}
    with pytest.raises(ValueError):
        apply_choice(p, "s=keep:default")
    q = apply_choice(p, "s=keep:shared")
    m = _moves(q)["s"]
    assert (m.source_profile, m.retire) == ("shared", [("default", "general/s")])


def test_shared_copy_containing_local_is_kept(fleet, tmp_path):
    root = fleet({"default": {"general/s": {"SKILL.md": skill_md("s", "d", ["a"])}}})
    write_files(tmp_path / "shared" / "general" / "s", {"SKILL.md": skill_md("s", "d", ["a", "b"])})
    m = _moves(_propose(root))["s"]
    assert (m.source_profile, m.retire) == ("shared", [("default", "general/s")])


def test_target_clash_is_blocked(fleet, tmp_path):
    root = fleet({"default": {"general/s": {"SKILL.md": skill_md("s", "d", ["a"])}}})
    write_files(tmp_path / "shared" / "general" / "s", {"SKILL.md": skill_md("t", "d", ["other"])})
    p = _propose(root)
    assert p.blocked == ["s"] and "s" not in _moves(p)


def test_layout_and_shared_root_are_checked(classic_fleet):
    with pytest.raises(ValueError):
        _propose(classic_fleet, layout=Layout(classic_fleet / "skills" / "shared", classic_fleet / "commons"))
    with pytest.raises(ValueError):
        _propose(classic_fleet, layout=Layout(classic_fleet / "shared", classic_fleet / "skills" / "st"))


def test_proposal_reads_nothing_it_should_not_write(classic_fleet):
    before = {p: p.read_bytes() for p in classic_fleet.rglob("*") if p.is_file()}
    _propose(classic_fleet)
    after = {p: p.read_bytes() for p in classic_fleet.rglob("*") if p.is_file()}
    assert before == after
    assert not (classic_fleet / "shared").exists()
    assert not (classic_fleet / "commons").exists()


# ------------------------------------------------------------------ pristine copies of a taken-over name


def _mixed_spec(edited_in=("pikvm", "pocket"), hub=False):
    """classic_fleet's bundled-edited, edited identically (a move) in *edited_in*, pristine elsewhere."""
    origin = {"SKILL.md": skill_md("b", "Bundled.", ["o1", "o2"])}
    edited = {"SKILL.md": skill_md("b", "Bundled.", ["o1", "o2", "edit"])}
    spec = {p: {"x/b": dict(edited if p in edited_in else origin)} for p in PROFILES}
    if hub:
        spec["_hub"] = {p: {"b": "x/b"} for p in PROFILES}
    else:
        spec["_bundled"] = {p: {"b": origin} for p in PROFILES}
    return spec


def _hub_pristine_locks(root, edited_in):
    """Make each non-edited profile's hub lock record its own (pristine) copy's hash."""
    for p in PROFILES:
        skills = profile_home(root, p) / "skills"
        lock = json.loads((skills / ".hub" / "lock.json").read_text())
        if p not in edited_in:
            lock["installed"]["b"]["content_hash"] = hub_content_hash(skills / "x" / "b")
        else:
            lock["installed"]["b"]["content_hash"] = "sha256:old"
        (skills / ".hub" / "lock.json").write_text(json.dumps(lock))


@pytest.mark.parametrize("hub", [False, True])
def test_move_retires_pristine_copies_of_the_same_name(fleet, hub):
    root = fleet(_mixed_spec(hub=hub))
    if hub:
        _hub_pristine_locks(root, ("pikvm", "pocket"))
    p = _propose(root)
    m = _moves(p)["b"]
    assert m.source_profile in ("pikvm", "pocket")
    assert m.retire == [(prof, "x/b") for prof in PROFILES]
    pristine = "hub-pristine" if hub else "bundled-pristine"
    edited = "hub-edited" if hub else "bundled-edited"
    assert {(t.profile, t.kind) for t in p.taken_over} == {
        ("default", pristine),
        ("device", pristine),
        ("pikvm", edited),
        ("pocket", edited),
    }
    assert p.stays_local == []


def test_only_pristine_copies_still_stay_local(fleet):
    root = fleet(_mixed_spec(edited_in=()))
    p = _propose(root)
    assert p.moves == [] and p.conflicts == [] and p.taken_over == []
    assert p.stays_local == ["b"]


def test_conflict_keeps_its_pristine_copies_for_the_keep_choice(classic_fleet):
    p = _propose(classic_fleet)
    (c,) = [c for c in p.conflicts if c.name == "bundled-edited"]
    assert {r.profile for r in c.pinned} == {"default", "device"}
    assert all(r.kind == "bundled-pristine" and r.fingerprint for r in c.pinned)
    assert Proposal.from_json(p.to_json()) == p
