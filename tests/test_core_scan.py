"""Tests for commons.core.scan skill walk and provenance."""

import json
import os
from pathlib import Path

import pytest

from commons.core.fleet import discover
from commons.core.scan import read_manifest, read_skill_name, scan_profile, tree_hash
from tests.fleet import dir_hash_md5, hub_content_hash, skill_md, write_files


def _scan(root, name):
    profile = {p.name: p for p in discover(root)}[name]
    return scan_profile(profile, dir_hash_md5, hub_content_hash)


def _by_rel(copies):
    return {c.rel: c for c in copies}


def test_default_kinds(classic_fleet):
    kinds = {c.rel: c.kind for c in _scan(classic_fleet, "default")}
    assert kinds == {
        "bundled/bundled-edited": "bundled-pristine",
        "bundled/bundled-pristine": "bundled-pristine",
        "general/conflict": "own",
        "general/contained": "own",
        "general/linked": "symlink",
        "general/same": "own",
        "tools/hubskill": "hub-pristine",
    }


def test_results_sorted_by_rel_and_ids(classic_fleet):
    copies = _scan(classic_fleet, "default")
    assert [c.rel for c in copies] == sorted(c.rel for c in copies)
    assert copies[0].id == "default:bundled/bundled-edited"
    assert all(c.profile == "default" for c in copies)


def test_origin_hashes(classic_fleet):
    copies = _by_rel(_scan(classic_fleet, "default"))
    pristine = copies["bundled/bundled-pristine"]
    assert pristine.origin_hash == dir_hash_md5(pristine.path)
    hub = copies["tools/hubskill"]
    assert hub.origin_hash == hub_content_hash(hub.path)
    assert copies["general/same"].origin_hash is None
    assert copies["general/linked"].origin_hash is None


def test_pikvm_bundled_edited(classic_fleet):
    copy = _by_rel(_scan(classic_fleet, "pikvm"))["bundled/bundled-edited"]
    assert copy.kind == "bundled-edited"
    assert copy.origin_hash is not None
    assert copy.origin_hash != dir_hash_md5(copy.path)


def test_device_has_two_dups(classic_fleet):
    dups = [c for c in _scan(classic_fleet, "device") if c.name == "dup"]
    assert [c.rel for c in dups] == ["a/dup", "b/dup"]
    assert dups[0].tree_hash != dups[1].tree_hash


def test_frontmatter_name_is_read_line_by_line_like_hermes(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/broken", {"SKILL.md": "---\nname: [unclosed\n---\nbody\n"})
    write_files(skills / "general/noname", {"SKILL.md": "---\ndescription: x\n---\nbody\n"})
    write_files(skills / "general/nofm", {"SKILL.md": "just a body\n"})
    write_files(skills / "general/emptyname", {"SKILL.md": "---\nname: ''\n---\n"})
    copies = _by_rel(_scan(classic_fleet, "pocket"))
    assert copies["general/broken"].name == "[unclosed"
    assert copies["general/noname"].name == "noname"
    assert copies["general/nofm"].name == "nofm"
    assert copies["general/emptyname"].name == "emptyname"
    assert copies["general/same"].name == "same"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\nname: [unclosed\n---\n", "[unclosed"),
        ("---\nname: 123\n---\n", "123"),
        ("\n---\nname: after-blank\n---\n", "after-blank"),
        ('---\nname: "double"\n---\n', "double"),
        ("---\nname: 'single'\n---\n", "single"),
        ("---\nname: no-close\nbody\n", "no-close"),
        ("---\nname:\nname: second\n---\n", "second"),
        ("---\ndescription: d\n---\nname: body\n", "fallback"),
        ("\ufeff---\nname: bom\n---\n", "bom"),
        ("---\r\nname: crlf\r\n---\r\n", "crlf"),
        ("no frontmatter\nname: x\n", "fallback"),
    ],
)
def test_read_skill_name_cases(tmp_path, text, expected):
    md = tmp_path / "SKILL.md"
    md.write_text(text, encoding="utf-8")
    assert read_skill_name(md, "fallback") == expected


def test_read_skill_name_missing_file(tmp_path):
    assert read_skill_name(tmp_path / "SKILL.md", "fb") == "fb"


def test_frontmatter_name_wins_over_folder(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/folder", {"SKILL.md": skill_md("other-name", "d", ["x"])})
    assert _by_rel(_scan(classic_fleet, "pocket"))["general/folder"].name == "other-name"


def test_skill_md_in_support_dir_is_not_a_skill(classic_fleet):
    skill = classic_fleet / "skills" / "general" / "same"
    write_files(skill, {"references/inner/SKILL.md": skill_md("inner", "d", ["x"])})
    write_files(skill, {"scripts/SKILL.md": skill_md("inscripts", "d", ["x"])})
    copies = _scan(classic_fleet, "default")
    rels = [c.rel for c in copies]
    assert "general/same/references/inner" not in rels
    assert "general/same/scripts" not in rels
    assert not any(c.name in ("inner", "inscripts") for c in copies)


def test_nested_skill_outside_support_dirs_is_reported(classic_fleet):
    skills = classic_fleet / "skills"
    write_files(skills / "general/same", {"sub/SKILL.md": skill_md("sub", "d", ["x"])})
    write_files(skills / "general/same", {"sub/deeper/SKILL.md": skill_md("deeper", "d", ["x"])})
    write_files(skills / "scripts/top", {"SKILL.md": skill_md("top", "d", ["x"])})
    copies = _by_rel(_scan(classic_fleet, "default"))
    assert copies["general/same"].parent_rel is None
    assert copies["general/same/sub"].parent_rel == "general/same"
    assert copies["general/same/sub"].name == "sub"
    assert copies["general/same/sub/deeper"].parent_rel == "general/same/sub"
    assert copies["scripts/top"].parent_rel is None
    rels = list(_by_rel(_scan(classic_fleet, "default")))
    assert rels == sorted(rels)


def test_only_excluded_dirs_are_skipped(classic_fleet):
    skills = classic_fleet / "skills"
    for d in (".archive", ".hub/quarantine", ".git", "node_modules", ".curator_backups", ".hermes-dist"):
        write_files(skills / d / "old", {"SKILL.md": skill_md("old", "d", ["x"])})
    write_files(skills / ".hidden/thing", {"SKILL.md": skill_md("thing", "d", ["x"])})
    copies = _by_rel(_scan(classic_fleet, "default"))
    assert not any(c.name == "old" for c in copies.values())
    assert copies[".hidden/thing"].kind == "own"


def test_symlinked_category_is_followed(classic_fleet, tmp_path):
    target = tmp_path / "_linkedcat"
    write_files(target / "inside", {"SKILL.md": skill_md("inside", "d", ["x"])})
    (classic_fleet / "skills" / "linkedcat").symlink_to(target, target_is_directory=True)
    copies = _by_rel(_scan(classic_fleet, "default"))
    inside = copies["linkedcat/inside"]
    assert inside.via_symlink is True
    assert inside.kind == "symlink"
    assert inside.symlink_target is None
    assert inside.tree_hash == tree_hash(target / "inside")
    linked = copies["general/linked"]
    assert linked.name == "linked"
    assert linked.via_symlink is True
    assert linked.symlink_target == tmp_path / "_linked" / "linked"
    assert linked.tree_hash == tree_hash(tmp_path / "_linked" / "linked")
    same = copies["general/same"]
    assert same.via_symlink is False
    assert same.symlink_target is None
    assert same.error is None


def test_symlinked_skill_stays_symlink_even_if_bundled_or_hub(classic_fleet):
    skills = classic_fleet / "skills"
    with (skills / ".bundled_manifest").open("a", encoding="utf-8") as fh:
        fh.write("linked:\n")
    assert _by_rel(_scan(classic_fleet, "default"))["general/linked"].kind == "symlink"


def test_symlink_cycles_terminate(classic_fleet):
    skills = classic_fleet / "skills"
    (skills / "general" / "loop").symlink_to(skills / "general", target_is_directory=True)
    (skills / "general" / "same" / "self").symlink_to(skills / "general" / "same", target_is_directory=True)
    (skills / "rootloop").symlink_to(skills, target_is_directory=True)
    rels = [c.rel for c in _scan(classic_fleet, "default")]
    assert not any(r.startswith(("general/loop", "rootloop", "general/same/self")) for r in rels)
    assert "general/same" in rels


def test_edited_hubskill_is_hub_edited(classic_fleet):
    (classic_fleet / "skills" / "tools" / "hubskill" / "extra.md").write_text("local\n", encoding="utf-8")
    assert _by_rel(_scan(classic_fleet, "default"))["tools/hubskill"].kind == "hub-edited"


def test_hub_match_by_install_path(classic_fleet):
    lock = classic_fleet / "skills" / ".hub" / "lock.json"
    lock.write_text(lock.read_text().replace('"hubskill": {', '"renamed": {'), encoding="utf-8")
    assert _by_rel(_scan(classic_fleet, "default"))["tools/hubskill"].kind == "hub-pristine"


def _write_lock(skills, installed):
    lock = skills / ".hub" / "lock.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"version": 1, "installed": installed}), encoding="utf-8")


def test_hub_install_path_mismatch_does_not_fall_back_to_name(classic_fleet):
    skills = classic_fleet / "profiles" / "device" / "skills"
    _write_lock(
        skills, {"dup": {"install_path": "a/dup", "content_hash": hub_content_hash(skills / "a/dup")}}
    )
    copies = _by_rel(_scan(classic_fleet, "device"))
    assert copies["a/dup"].kind == "hub-pristine"
    assert copies["b/dup"].kind == "own"
    assert copies["b/dup"].origin_hash is None


def test_hub_entry_without_install_path_matches_resolved_name(classic_fleet):
    skills = classic_fleet / "profiles" / "device" / "skills"
    write_files(skills / "c/folder-name", {"SKILL.md": skill_md("fm-hub", "d", ["x"])})
    _write_lock(
        skills,
        {
            "dup": {"content_hash": hub_content_hash(skills / "a/dup")},
            "folder-name": {"content_hash": "sha256:0"},
            "blank": {"install_path": "  ", "content_hash": "sha256:0"},
        },
    )
    copies = _by_rel(_scan(classic_fleet, "device"))
    assert copies["a/dup"].kind == "hub-pristine"
    assert copies["b/dup"].kind == "hub-edited"
    assert copies["c/folder-name"].kind == "own"


def test_corrupt_lock_and_manifest_do_not_crash(classic_fleet):
    skills = classic_fleet / "skills"
    (skills / ".hub" / "lock.json").write_text("{not json", encoding="utf-8")
    kinds = {c.rel: c.kind for c in _scan(classic_fleet, "default")}
    assert kinds["tools/hubskill"] == "own"
    (skills / ".hub" / "lock.json").write_text('{"installed": ["wrong", "shape"]}', encoding="utf-8")
    assert _by_rel(_scan(classic_fleet, "default"))["tools/hubskill"].kind == "own"
    (skills / ".bundled_manifest").write_bytes(b"\xff\xfe garbage\n\n:nohash-name\n")
    kinds = {c.rel: c.kind for c in _scan(classic_fleet, "default")}
    assert kinds["bundled/bundled-pristine"] == "own"


def test_blank_lines_and_orphan_ignored(classic_fleet):
    manifest = classic_fleet / "skills" / ".bundled_manifest"
    manifest.write_text("\n   \n:orphan\n" + manifest.read_text(), encoding="utf-8")
    entries = read_manifest(manifest.parent)
    assert set(entries) == {"bundled-pristine", "bundled-edited"}
    kinds = {c.rel: c.kind for c in _scan(classic_fleet, "default")}
    assert kinds["bundled/bundled-pristine"] == "bundled-pristine"


def test_orphan_line_does_not_match_anything(classic_fleet):
    skills = classic_fleet / "skills"
    (skills / ".bundled_manifest").write_text(":orphan\n", encoding="utf-8")
    assert read_manifest(skills) == {}
    kinds = {c.rel: c.kind for c in _scan(classic_fleet, "default")}
    assert kinds["bundled/bundled-pristine"] == "own"
    assert kinds["bundled/bundled-edited"] == "own"


def test_v1_plain_name_line_is_pristine_with_empty_origin(classic_fleet):
    skills = classic_fleet / "profiles" / "pikvm" / "skills"
    (skills / ".bundled_manifest").write_text("  bundled-edited  \n", encoding="utf-8")
    assert read_manifest(skills) == {"bundled-edited": ""}
    copy = _by_rel(_scan(classic_fleet, "pikvm"))["bundled/bundled-edited"]
    assert copy.kind == "bundled-pristine"
    assert copy.origin_hash == ""


def test_v2_line_with_empty_hash_is_pristine_with_empty_origin(classic_fleet):
    skills = classic_fleet / "profiles" / "pikvm" / "skills"
    (skills / ".bundled_manifest").write_text("bundled-edited:\nbundled-pristine :  \n", encoding="utf-8")
    assert read_manifest(skills) == {"bundled-edited": "", "bundled-pristine": ""}
    copies = _by_rel(_scan(classic_fleet, "pikvm"))
    for rel in ("bundled/bundled-edited", "bundled/bundled-pristine"):
        assert copies[rel].kind == "bundled-pristine"
        assert copies[rel].origin_hash == ""


def test_manifest_matched_by_frontmatter_name(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/folder-x", {"SKILL.md": skill_md("fm-name", "d", ["x"])})
    path = skills / "general" / "folder-x"
    origin = dir_hash_md5(path)
    with (skills / ".bundled_manifest").open("a", encoding="utf-8") as fh:
        fh.write(f"fm-name:{origin}\nfolder-x:deadbeef\n")
    copy = _by_rel(_scan(classic_fleet, "pocket"))["general/folder-x"]
    assert copy.name == "fm-name"
    assert copy.kind == "bundled-pristine"
    assert copy.origin_hash == origin


def test_manifest_frontmatter_name_only(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/folder-y", {"SKILL.md": skill_md("fm-only", "d", ["x"])})
    with (skills / ".bundled_manifest").open("a", encoding="utf-8") as fh:
        fh.write("fm-only:deadbeef\n")
    copy = _by_rel(_scan(classic_fleet, "pocket"))["general/folder-y"]
    assert copy.kind == "bundled-edited"
    assert copy.origin_hash == "deadbeef"


def test_manifest_folder_fallback_without_frontmatter_name(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/plainfolder", {"SKILL.md": "---\ndescription: x\n---\nbody\n"})
    path = skills / "general" / "plainfolder"
    origin = dir_hash_md5(path)
    with (skills / ".bundled_manifest").open("a", encoding="utf-8") as fh:
        fh.write(f"plainfolder:{origin}\n")
    copy = _by_rel(_scan(classic_fleet, "pocket"))["general/plainfolder"]
    assert copy.kind == "bundled-pristine"
    assert copy.origin_hash == origin


def test_manifest_not_matched_by_folder_when_frontmatter_name_differs(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/folder-z", {"SKILL.md": skill_md("fm-z", "d", ["x"])})
    origin = dir_hash_md5(skills / "general" / "folder-z")
    with (skills / ".bundled_manifest").open("a", encoding="utf-8") as fh:
        fh.write(f"folder-z:{origin}\n")
    copy = _by_rel(_scan(classic_fleet, "pocket"))["general/folder-z"]
    assert copy.kind == "own"
    assert copy.origin_hash is None


def test_missing_skills_dir(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    profile = discover(tmp_path)[0]
    assert scan_profile(profile, dir_hash_md5, hub_content_hash) == []


def test_tree_hash_across_profiles(classic_fleet):
    default = _by_rel(_scan(classic_fleet, "default"))
    device = _by_rel(_scan(classic_fleet, "device"))
    pocket = _by_rel(_scan(classic_fleet, "pocket"))
    assert default["general/same"].tree_hash == device["general/same"].tree_hash
    assert default["general/same"].tree_hash == pocket["general/same"].tree_hash
    assert default["general/contained"].tree_hash != device["general/contained"].tree_hash
    assert default["general/contained"].tree_hash == pocket["general/contained"].tree_hash


def test_tree_hash_ignores_caches(classic_fleet):
    skill = classic_fleet / "skills" / "general" / "same"
    before = tree_hash(skill)
    write_files(
        skill,
        {
            "__pycache__/x.cpython-311.pyc": b"\x00",
            "scripts/__pycache__/y.pyc": b"\x01",
            "stray.pyc": b"\x02",
            ".git/HEAD": "ref: refs/heads/main\n",
            "node_modules/pkg/index.js": "x\n",
            ".venv/bin/python": "x\n",
            "sub/.ruff_cache/x": "x\n",
            "opt.pyo": b"\x03",
        },
    )
    assert tree_hash(skill) == before
    write_files(skill, {"scripts/run.py": "print(1)\n"})
    assert tree_hash(skill) != before


def test_tree_hash_length_prefix_prevents_collisions(tmp_path):
    write_files(tmp_path / "one", {"a": b"", "b": b"c"})
    write_files(tmp_path / "two", {"a": b"b\0c"})
    assert tree_hash(tmp_path / "one") != tree_hash(tmp_path / "two")


def test_tree_hash_records_inner_symlinks_without_following(tmp_path):
    skill = tmp_path / "skill"
    outside = tmp_path / "outside"
    write_files(skill, {"SKILL.md": "x\n", "real.txt": "same\n"})
    write_files(outside, {"data.txt": "one\n"})
    (skill / "dirlink").symlink_to(outside, target_is_directory=True)
    before = tree_hash(skill)
    (outside / "data.txt").write_text("two\n", encoding="utf-8")
    assert tree_hash(skill) == before
    (skill / "dirlink").unlink()
    (skill / "dirlink").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert tree_hash(skill) != before
    plain = tmp_path / "plain"
    write_files(plain, {"SKILL.md": "x\n", "real.txt": "same\n", "link": "real.txt"})
    linked = tmp_path / "linked"
    write_files(linked, {"SKILL.md": "x\n", "real.txt": "same\n"})
    (linked / "link").symlink_to("real.txt")
    assert tree_hash(plain) != tree_hash(linked)


def _unreadable(path: Path) -> bool:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return False
    try:
        if path.is_dir():
            os.listdir(path)
        else:
            path.read_bytes()
    except PermissionError:
        return True
    return False


@pytest.fixture
def chmod_000():
    touched = []

    def apply(path: Path) -> None:
        touched.append((path, path.stat().st_mode))
        path.chmod(0)
        if not _unreadable(path):
            pytest.skip("chmod 000 has no effect here (root or permissive filesystem)")

    yield apply
    for path, mode in reversed(touched):
        path.chmod(mode)


def test_unreadable_file_sets_error_and_blank_hash(classic_fleet, chmod_000):
    secret = classic_fleet / "skills" / "general" / "same" / "notes.txt"
    chmod_000(secret)
    copies = _by_rel(_scan(classic_fleet, "default"))
    same = copies["general/same"]
    assert same.error
    assert same.tree_hash == ""
    assert copies["general/conflict"].error is None


def test_unreadable_skill_md_sets_error(classic_fleet, chmod_000):
    md = classic_fleet / "skills" / "bundled" / "bundled-pristine" / "SKILL.md"
    chmod_000(md)
    copy = _by_rel(_scan(classic_fleet, "default"))["bundled/bundled-pristine"]
    assert copy.name == "bundled-pristine"
    assert copy.error
    assert copy.tree_hash == ""
    assert copy.kind == "bundled-edited"


def test_unreadable_dirs_do_not_crash(classic_fleet, chmod_000):
    skills = classic_fleet / "skills"
    write_files(skills / "general/same", {"inner/x.txt": "x\n"})
    write_files(skills / "locked/hidden", {"SKILL.md": skill_md("hidden", "d", ["x"])})
    chmod_000(skills / "general" / "same" / "inner")
    chmod_000(skills / "locked")
    copies = _by_rel(_scan(classic_fleet, "default"))
    assert copies["general/same"].error
    assert copies["general/same"].tree_hash == ""
    assert copies["general/conflict"].error is None
    assert "locked/hidden" not in copies
