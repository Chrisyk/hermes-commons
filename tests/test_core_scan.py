"""Tests for commons.core.scan skill walk and provenance."""

from commons.core.fleet import discover
from commons.core.scan import read_manifest, scan_profile, tree_hash
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


def test_broken_frontmatter_falls_back_to_folder(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/broken", {"SKILL.md": "---\nname: [unclosed\n---\nbody\n"})
    write_files(skills / "general/noname", {"SKILL.md": "---\ndescription: x\n---\nbody\n"})
    write_files(skills / "general/nofm", {"SKILL.md": "just a body\n"})
    write_files(skills / "general/emptyname", {"SKILL.md": "---\nname: ''\n---\n"})
    copies = _by_rel(_scan(classic_fleet, "pocket"))
    assert copies["general/broken"].name == "broken"
    assert copies["general/noname"].name == "noname"
    assert copies["general/nofm"].name == "nofm"
    assert copies["general/emptyname"].name == "emptyname"
    assert copies["general/same"].name == "same"


def test_frontmatter_name_wins_over_folder(classic_fleet):
    skills = classic_fleet / "profiles" / "pocket" / "skills"
    write_files(skills / "general/folder", {"SKILL.md": skill_md("other-name", "d", ["x"])})
    assert _by_rel(_scan(classic_fleet, "pocket"))["general/folder"].name == "other-name"


def test_nested_skill_md_is_part_of_parent(classic_fleet):
    skill = classic_fleet / "skills" / "general" / "same"
    write_files(skill, {"references/inner/SKILL.md": skill_md("inner", "d", ["x"])})
    copies = _scan(classic_fleet, "default")
    rels = [c.rel for c in copies]
    assert "general/same/references/inner" not in rels
    assert not any(c.name == "inner" for c in copies)


def test_dot_directories_are_skipped(classic_fleet):
    skills = classic_fleet / "skills"
    write_files(skills / ".archive/old", {"SKILL.md": skill_md("old", "d", ["x"])})
    write_files(skills / ".hidden/thing", {"SKILL.md": skill_md("thing", "d", ["x"])})
    write_files(skills / ".hub/quarantine/q", {"SKILL.md": skill_md("q", "d", ["x"])})
    rels = [c.rel for c in _scan(classic_fleet, "default")]
    assert not any(r.startswith(".") for r in rels)


def test_symlink_not_descended(classic_fleet, tmp_path):
    target = tmp_path / "_linkedcat"
    write_files(target / "inside", {"SKILL.md": skill_md("inside", "d", ["x"])})
    (classic_fleet / "skills" / "linkedcat").symlink_to(target, target_is_directory=True)
    copies = _by_rel(_scan(classic_fleet, "default"))
    assert "linkedcat/inside" not in copies
    linked = copies["general/linked"]
    assert linked.name == "linked"
    assert linked.tree_hash == tree_hash(tmp_path / "_linked" / "linked")


def test_edited_hubskill_is_hub_edited(classic_fleet):
    (classic_fleet / "skills" / "tools" / "hubskill" / "extra.md").write_text("local\n", encoding="utf-8")
    assert _by_rel(_scan(classic_fleet, "default"))["tools/hubskill"].kind == "hub-edited"


def test_hub_match_by_install_path(classic_fleet):
    lock = classic_fleet / "skills" / ".hub" / "lock.json"
    lock.write_text(lock.read_text().replace('"hubskill": {', '"renamed": {'), encoding="utf-8")
    assert _by_rel(_scan(classic_fleet, "default"))["tools/hubskill"].kind == "hub-pristine"


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
        },
    )
    assert tree_hash(skill) == before
    write_files(skill, {"scripts/run.py": "print(1)\n"})
    assert tree_hash(skill) != before
