"""Tests for commons.core.group grouping and containment."""

from dataclasses import replace

from commons.core.fleet import discover
from commons.core.group import group, is_text, normalized_lines
from commons.core.scan import scan_profile
from tests.fleet import dir_hash_md5, hub_content_hash, skill_md, write_files


def _scan_all(root):
    copies = []
    for profile in discover(root):
        copies += scan_profile(profile, dir_hash_md5, hub_content_hash)
    return copies


def _groups(root):
    return {g.name: g for g in group(_scan_all(root))}


def _home(root, profile):
    return root if profile == "default" else root / "profiles" / profile


def test_groups_sorted_by_name(classic_fleet):
    names = [g.name for g in group(_scan_all(classic_fleet))]
    assert names == sorted(names)
    assert set(names) == {
        "bundled-edited",
        "bundled-pristine",
        "conflict",
        "contained",
        "dup",
        "hubskill",
        "linked",
        "same",
    }


def test_classic_statuses(classic_fleet):
    statuses = {name: g.status for name, g in _groups(classic_fleet).items()}
    assert statuses == {
        "bundled-edited": "conflict",
        "bundled-pristine": "stays-local",
        "conflict": "conflict",
        "contained": "contained",
        "dup": "in-profile-dup",
        "hubskill": "stays-local",
        "linked": "stays-local",
        "same": "identical",
    }


def test_identical_keeps_default_copy(classic_fleet):
    same = _groups(classic_fleet)["same"]
    assert same.keep is not None
    assert same.keep.id == "default:general/same"
    assert len(same.copies) == 4
    assert same.unique_lines == {}
    assert same.taken_over is False


def test_contained_keeps_device_copy(classic_fleet):
    contained = _groups(classic_fleet)["contained"]
    assert contained.keep is not None
    assert contained.keep.id == "device:general/contained"
    assert contained.unique_lines == {}


def test_conflict_counts_unique_lines(classic_fleet):
    conflict = _groups(classic_fleet)["conflict"]
    assert conflict.keep is None
    assert conflict.unique_lines == {"default:general/conflict": 1, "device:general/conflict": 1}


def test_bundled_edited_is_taken_over_conflict(classic_fleet):
    edited = _groups(classic_fleet)["bundled-edited"]
    assert edited.status == "conflict"
    assert edited.taken_over is True
    assert edited.unique_lines == {
        "pikvm:bundled/bundled-edited": 1,
        "pocket:bundled/bundled-edited": 1,
    }
    # The pristine default/device copies stay in the group but do not participate.
    assert {c.profile for c in edited.copies} == {"default", "device", "pikvm", "pocket"}


def test_stays_local_groups_have_no_keep(classic_fleet):
    groups = _groups(classic_fleet)
    for name in ("bundled-pristine", "hubskill", "linked"):
        assert groups[name].keep is None
        assert groups[name].taken_over is False


def test_copies_in_profile_order(classic_fleet):
    same = _groups(classic_fleet)["same"]
    assert [c.profile for c in same.copies] == ["default", "device", "pikvm", "pocket"]


def test_changed_version_line_is_trivial(fleet):
    base = ["---", "name: tool", "version: 1.0.0", "description: d", "---", "first body line"]
    newer = ["---", "name: tool", "version: 2.0.0", "description: d", "---", "first body line"]
    root = fleet(
        {
            "default": {"t/tool": {"SKILL.md": "\n".join(base) + "\n"}},
            "device": {"t/tool": {"SKILL.md": "\n".join(newer) + "\n"}},
        }
    )
    tool = _groups(root)["tool"]
    assert tool.status == "contained"
    assert tool.keep is not None and tool.keep.profile == "default"


def test_version_line_does_not_block_strict_containment(fleet):
    small = "---\nname: tool\nversion: 1\n---\nshared line here\n"
    big = "---\nname: tool\nversion: 9\n---\nshared line here\nextra line here\n"
    root = fleet({"default": {"t/tool": {"SKILL.md": small}}, "pocket": {"t/tool": {"SKILL.md": big}}})
    tool = _groups(root)["tool"]
    assert tool.status == "contained"
    assert tool.keep is not None and tool.keep.profile == "pocket"


def test_binary_only_in_smaller_copy_is_conflict(fleet):
    small = {"SKILL.md": skill_md("bin", "d", ["shared line here"]), "assets/logo.png": b"\x89PNG\x00\x01"}
    big = {"SKILL.md": skill_md("bin", "d", ["shared line here", "extra line here"])}
    root = fleet({"default": {"t/bin": small}, "device": {"t/bin": big}})
    g = _groups(root)["bin"]
    assert g.status == "conflict"
    assert g.unique_lines == {"default:t/bin": 0, "device:t/bin": 1}


def test_binary_with_same_bytes_is_contained(fleet):
    blob = b"\x89PNG\x00\x01"
    small = {"SKILL.md": skill_md("bin", "d", ["shared line here"]), "assets/logo.png": blob}
    big = {"SKILL.md": skill_md("bin", "d", ["shared line here", "more"]), "assets/logo.png": blob}
    root = fleet({"default": {"t/bin": small}, "device": {"t/bin": big}})
    g = _groups(root)["bin"]
    assert g.status == "contained"
    assert g.keep is not None and g.keep.profile == "device"


def test_binary_with_different_bytes_is_conflict(fleet):
    small = {"SKILL.md": skill_md("bin", "d", ["shared line here"]), "assets/logo.png": b"\x00one"}
    big = {"SKILL.md": skill_md("bin", "d", ["shared line here", "more"]), "assets/logo.png": b"\x00two"}
    root = fleet({"default": {"t/bin": small}, "device": {"t/bin": big}})
    assert _groups(root)["bin"].status == "conflict"


def test_equal_line_sets_keep_earlier_profile(fleet):
    a = "---\nname: ws\n---\nsome   spaced    line\n# \n"
    b = "---\nname: ws\n---\n  some spaced line\n"
    root = fleet({"pocket": {"t/ws": {"SKILL.md": a}}, "device": {"t/ws": {"SKILL.md": b}}})
    g = _groups(root)["ws"]
    assert g.status == "contained"
    assert g.keep is not None and g.keep.profile == "device"


def test_three_way_contained_chain(fleet):
    lines = [f"chain line {i}" for i in range(5)]
    root = fleet(
        {
            "default": {"t/c": {"SKILL.md": skill_md("c", "d", lines[:2])}},
            "device": {"t/c": {"SKILL.md": skill_md("c", "d", lines)}},
            "pocket": {"t/c": {"SKILL.md": skill_md("c", "d", lines[:4])}},
        }
    )
    g = _groups(root)["c"]
    assert g.status == "contained"
    assert g.keep is not None and g.keep.profile == "device"


def test_conflict_unique_lines_only_count_survivors(fleet):
    root = fleet(
        {
            "default": {"t/c": {"SKILL.md": skill_md("c", "d", ["common line", "alpha only"])}},
            "device": {"t/c": {"SKILL.md": skill_md("c", "d", ["common line", "beta only", "beta two"])}},
            "pocket": {"t/c": {"SKILL.md": skill_md("c", "d", ["common line"])}},
        }
    )
    g = _groups(root)["c"]
    assert g.status == "conflict"
    assert g.unique_lines == {"default:t/c": 1, "device:t/c": 2}


def test_pinned_copies_do_not_participate(classic_fleet):
    # A hub-pristine copy elsewhere must not turn an identical group into a conflict.
    skills = _home(classic_fleet, "pocket") / "skills"
    write_files(skills / "tools/hubskill", {"SKILL.md": skill_md("hubskill", "Edited.", ["mine only"])})
    hub = _groups(classic_fleet)["hubskill"]
    assert hub.status == "identical"
    assert hub.keep is not None and hub.keep.id == "pocket:tools/hubskill"
    assert len(hub.copies) == 2


def test_error_copy_is_unreadable(classic_fleet):
    copies = _scan_all(classic_fleet)
    copies = [
        replace(c, error="unreadable: x", tree_hash="") if c.id == "pikvm:general/same" else c for c in copies
    ]
    g = {x.name: x for x in group(copies)}["same"]
    assert g.status == "unreadable"
    assert g.keep is None


def test_nested_copy_is_nested(classic_fleet):
    write_files(
        _home(classic_fleet, "device") / "skills" / "general" / "same",
        {"sub/SKILL.md": skill_md("inner", "d", ["inner line"])},
    )
    write_files(
        _home(classic_fleet, "pocket") / "skills" / "x/inner", {"SKILL.md": skill_md("inner", "d", ["o"])}
    )
    g = _groups(classic_fleet)["inner"]
    assert g.status == "nested"
    assert g.keep is None


def test_same_hash_twice_in_one_profile_is_not_dup(fleet):
    body = {"SKILL.md": skill_md("twin", "d", ["twin line"])}
    root = fleet({"default": {"a/twin": dict(body), "b/twin": dict(body)}})
    g = _groups(root)["twin"]
    assert g.status == "identical"
    assert g.keep is not None and g.keep.rel == "a/twin"


def test_symlinked_copies_never_move(fleet):
    root = fleet({"default": {"t/link": "symlink:_target/link"}, "device": {}})
    g = _groups(root)["link"]
    assert g.status == "stays-local"


def test_hub_edited_is_taken_over(classic_fleet):
    (classic_fleet / "skills" / "tools" / "hubskill" / "extra.md").write_text(
        "local edit\n", encoding="utf-8"
    )
    g = _groups(classic_fleet)["hubskill"]
    assert g.status == "identical"
    assert g.taken_over is True


def test_normalized_lines_skips_trivial(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_text(
        "---\nname: x\nversion: 3\nupdated: today\nlast_updated: now\n```python\n```\n##  \n- \n> \n| \n"
        "abc\n  real    content   line  \n",
        encoding="utf-8",
    )
    assert normalized_lines(md) == {"name: x", "real content line"}
    write_files(
        tmp_path / "tree", {"a.md": "line one here\n", "b/c.txt": "line two here\n", "d.bin": b"\x00zz"}
    )
    assert normalized_lines(tmp_path / "tree") == {"line one here", "line two here"}


def test_is_text():
    assert is_text(b"hello\n")
    assert is_text("caf\u00e9\n".encode())
    assert not is_text(b"a\x00b")
    assert not is_text(b"\xff\xfe broken")
    assert not is_text(b"x" * 9000 + b"\xff")
    assert is_text(b"")
