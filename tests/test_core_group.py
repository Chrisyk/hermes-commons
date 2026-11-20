"""Tests for commons.core.group grouping and containment."""

import os
from collections import Counter
from dataclasses import replace

import pytest

from commons.core.fleet import discover
from commons.core.group import (
    compare_variants,
    file_lines,
    fingerprint,
    group,
    is_text,
    is_text_file,
    same_content,
)
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
    assert g.unique_files == {"default:t/bin": ["assets/logo.png"], "device:t/bin": []}


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
    a = "---\r\nname: ws\r\n---\r\n  some spaced line   \r\n"
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


def test_file_lines_keeps_everything_but_skill_md_version_lines(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_bytes(
        b"---\r\nname: x\nversion: 3\nupdated: today\nlast_updated: now\n---\n"
        b"```python\n```\n\n  indented   line  \nab\nab\nversion: body keeps\n"
    )
    assert file_lines(md) == Counter(
        [
            "---",
            "name: x",
            "---",
            "```python",
            "```",
            "",
            "  indented   line",
            "ab",
            "ab",
            "version: body keeps",
        ]
    )
    other = tmp_path / "notes.md"
    other.write_text("---\nversion: 3\n---\n", encoding="utf-8")
    assert file_lines(other) == Counter(["---", "version: 3", "---"])
    binary = tmp_path / "d.bin"
    binary.write_bytes(b"\x00zz")
    with pytest.raises(ValueError):
        file_lines(binary)


def test_is_text():
    assert is_text(b"hello\n")
    assert is_text("caf\u00e9\n".encode())
    assert not is_text(b"a\x00b")
    assert not is_text(b"\xff\xfe broken")
    assert not is_text(b"x" * 9000 + b"\xff")
    assert not is_text(b"x" * 9000 + b"\x00")
    assert is_text(b"")


def test_is_text_file_streams_and_sees_late_nul(tmp_path):
    late = tmp_path / "late.txt"
    late.write_bytes(b"line\n" * 700_000 + b"\x00")
    assert not is_text_file(late)
    # A multibyte char split across the 1 MiB read boundary is still text.
    split = tmp_path / "split.txt"
    split.write_bytes(b"a" * (1024 * 1024 - 1) + "\u00e9".encode() + b"\n")
    assert is_text_file(split)
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"a" * (2 * 1024 * 1024) + b"\xc3")
    assert not is_text_file(bad)


def test_nul_after_first_8k_makes_the_file_binary(fleet):
    body = "line of text\n" * 1000
    a = {"SKILL.md": _MD, "data.txt": body + "\x00"}
    b = {"SKILL.md": _MD_PLUS, "data.txt": body + "more\n\x00"}
    _, g = _pair(fleet, a, b)
    assert g.status == "conflict"


# Containment is per file: every file of the smaller copy must exist in the bigger
# one at the same path, with its lines present there in the same order.

_BASE = ["step one do this", "step two do that"]
_MD = skill_md("x", "d", _BASE)
_MD_PLUS = skill_md("x", "d", [*_BASE, "extra line"])


def _pair(fleet, a, b):
    """Group of skill x with copy *a* in default and copy *b* in device."""
    root = fleet({"default": {"t/x": a}, "device": {"t/x": b}})
    return root, _groups(root)["x"]


_PROBE_CASES = {
    "file only in A, lines elsewhere in B": (
        {"SKILL.md": _MD, "scripts/run.sh": "step one do this\n"},
        {"SKILL.md": _MD_PLUS},
    ),
    "empty file only in A": ({"SKILL.md": _MD, "scripts/__init__.py": ""}, {"SKILL.md": _MD_PLUS}),
    "short-line script only in A": (
        {"SKILL.md": _MD, "scripts/a.sh": "ls\ncd\nfi\n"},
        {"SKILL.md": _MD_PLUS},
    ),
    "reordered": ({"SKILL.md": skill_md("x", "d", _BASE[::-1])}, {"SKILL.md": _MD_PLUS}),
    "indent change python": (
        {"SKILL.md": _MD, "s.py": "if True:\n    do_thing()\n"},
        {"SKILL.md": skill_md("x", "d", [*_BASE, "extra"]), "s.py": "if True:\ndo_thing()\n"},
    ),
    "dup line removed": (
        {"SKILL.md": skill_md("x", "d", [*_BASE, "retry the command", "retry the command"])},
        {"SKILL.md": skill_md("x", "d", [*_BASE, "retry the command", "extra"])},
    ),
    "renamed file": (
        {"SKILL.md": _MD, "references/a.md": "ref content line\n"},
        {"SKILL.md": _MD + "more\n", "references/b.md": "ref content line\n"},
    ),
    "code fence lang": (
        {"SKILL.md": _MD + "```python\ncode line here\n```\n"},
        {"SKILL.md": _MD + "```\ncode line here\n```\nextra line\n"},
    ),
}


@pytest.mark.parametrize("case", list(_PROBE_CASES))
def test_lossy_copies_are_conflicts(fleet, case):
    a, b = _PROBE_CASES[case]
    _, g = _pair(fleet, a, b)
    assert g.status == "conflict", case
    assert g.keep is None


def test_file_only_in_one_copy_is_listed(fleet):
    a, b = _PROBE_CASES["renamed file"]
    _, g = _pair(fleet, a, b)
    assert g.unique_files == {"default:t/x": ["references/a.md"], "device:t/x": ["references/b.md"]}
    assert g.unique_lines == {"default:t/x": 1, "device:t/x": 2}


def test_duplicate_lines_count_as_unique(fleet):
    a, b = _PROBE_CASES["dup line removed"]
    _, g = _pair(fleet, a, b)
    assert g.unique_lines == {"default:t/x": 1, "device:t/x": 1}
    assert g.unique_files == {"default:t/x": [], "device:t/x": []}


def test_superset_in_every_file_is_contained(fleet):
    a = {"SKILL.md": _MD, "references/r.md": "ref one\nref two\n", "assets/i.bin": b"\x00img"}
    b = {
        "SKILL.md": skill_md("x", "d", ["intro line", _BASE[0], "middle", _BASE[1], "outro"]),
        "references/r.md": "ref one\nnew ref\nref two\n",
        "references/new.md": "only in b\n",
        "assets/i.bin": b"\x00img",
    }
    _, g = _pair(fleet, a, b)
    assert g.status == "contained"
    assert g.keep is not None and g.keep.id == "device:t/x"


def test_version_bump_only_is_contained(fleet):
    a = {"SKILL.md": "---\nname: x\nversion: 1.0.0\nupdated: 2026-01-01\n---\nbody line\n"}
    b = {"SKILL.md": "---\nname: x\nversion: 1.1.0\nlast_updated: 2026-02-02\n---\nbody line\n"}
    _, g = _pair(fleet, a, b)
    assert g.status == "contained"
    assert g.keep is not None and g.keep.id == "default:t/x"


def test_crlf_only_difference_is_contained(fleet):
    a = {"SKILL.md": _MD.replace("\n", "\r\n"), "s.sh": "echo a\r\n"}
    b = {"SKILL.md": _MD, "s.sh": "echo a\n"}
    _, g = _pair(fleet, a, b)
    assert g.status == "contained"


def test_trailing_whitespace_only_difference_is_contained(fleet):
    a = {"SKILL.md": _MD.replace("do this", "do this  \t"), "s.py": "x = 1   \n"}
    b = {"SKILL.md": _MD, "s.py": "x = 1\n"}
    _, g = _pair(fleet, a, b)
    assert g.status == "contained"


@pytest.mark.parametrize("which", ["smaller lacks it", "bigger lacks it"])
def test_final_newline_difference_is_not_contained(fleet, which):
    a, b = _MD, _MD_PLUS
    if which == "smaller lacks it":
        a = a.rstrip("\n")
    else:
        b = b.rstrip("\n")
    _, g = _pair(fleet, {"SKILL.md": a}, {"SKILL.md": b})
    assert g.status == "conflict"
    assert g.keep is None


def test_final_newline_only_difference_is_not_identical(fleet):
    _, g = _pair(fleet, {"SKILL.md": _MD, "n.txt": "x\n"}, {"SKILL.md": _MD, "n.txt": "x"})
    assert g.status == "conflict"


def test_crlf_final_newline_counts_as_a_final_newline(fleet):
    _, g = _pair(fleet, {"SKILL.md": _MD, "n.txt": "x\r\n"}, {"SKILL.md": _MD_PLUS, "n.txt": "x\n"})
    assert g.status == "contained"


def test_empty_dir_only_in_smaller_copy_is_conflict(fleet):
    root, _ = _pair(fleet, {"SKILL.md": _MD}, {"SKILL.md": _MD_PLUS})
    (root / "skills/t/x/references/empty").mkdir(parents=True)
    g = _groups(root)["x"]
    assert g.status == "conflict"
    assert g.unique_files["default:t/x"] == ["references/empty/"]


def test_empty_dir_only_in_bigger_copy_is_contained(fleet):
    root, _ = _pair(fleet, {"SKILL.md": _MD}, {"SKILL.md": _MD_PLUS})
    (root / "profiles/device/skills/t/x/empty").mkdir()
    g = _groups(root)["x"]
    assert g.status == "contained"
    assert g.keep is not None and g.keep.id == "device:t/x"


def test_empty_dir_breaks_identical(fleet):
    files = {"SKILL.md": _MD}
    root, g = _pair(fleet, dict(files), dict(files))
    assert g.status == "identical"
    (root / "skills/t/x/empty").mkdir()
    g = _groups(root)["x"]
    assert g.status == "contained"
    assert g.keep is not None and g.keep.id == "default:t/x"


def test_empty_runtime_cache_dir_is_ignored(fleet):
    files = {"SKILL.md": _MD}
    root, _ = _pair(fleet, dict(files), dict(files))
    (root / "skills/t/x/__pycache__").mkdir()
    assert _groups(root)["x"].status == "identical"


def test_undecodable_filename_in_own_copies(fleet):
    bad = os.fsdecode(b"bad\xff.txt")
    root, _ = _pair(fleet, {"SKILL.md": _MD}, {"SKILL.md": _MD})
    for home in (root, root / "profiles" / "device"):
        (home / "skills" / "t" / "x" / bad).write_bytes(b"x\n")
    assert _groups(root)["x"].status == "identical"


def test_executable_bit_only_in_smaller_copy_is_conflict(fleet):
    a = {"SKILL.md": _MD, "scripts/run.sh": "echo run\n"}
    b = {"SKILL.md": _MD_PLUS, "scripts/run.sh": "echo run\n"}
    root, _ = _pair(fleet, a, b)
    os.chmod(root / "skills/t/x/scripts/run.sh", 0o755)
    assert _groups(root)["x"].status == "conflict"


def test_executable_bit_difference_breaks_identical(fleet):
    files = {"SKILL.md": _MD, "scripts/run.sh": "echo run\n"}
    root, g = _pair(fleet, dict(files), dict(files))
    assert g.status == "identical"
    os.chmod(root / "profiles/device/skills/t/x/scripts/run.sh", 0o755)
    assert _groups(root)["x"].status == "conflict"


def test_inner_symlink_targets_must_match(fleet):
    root, _ = _pair(fleet, {"SKILL.md": _MD}, {"SKILL.md": _MD_PLUS})
    (root / "skills/t/x/link").symlink_to("SKILL.md")
    (root / "profiles/device/skills/t/x/link").symlink_to("other.md")
    assert _groups(root)["x"].status == "conflict"
    (root / "profiles/device/skills/t/x/link").unlink()
    (root / "profiles/device/skills/t/x/link").symlink_to("SKILL.md")
    assert _groups(root)["x"].status == "contained"


def test_frontmatter_exemption_only_in_leading_block(fleet):
    a = {"SKILL.md": _MD + "version: 2 in the body\n"}
    b = {"SKILL.md": _MD_PLUS}
    _, g = _pair(fleet, a, b)
    assert g.status == "conflict"


# Hermes skips EXCLUDED_SKILL_DIRS when hashing, but setup retires a contained
# copy with everything in it, so grouping counts those dirs byte for byte.
# Only regenerable runtime caches are ignored.

_EXCLUDED_ONLY_IN_A = {
    ".git": {".git/HEAD": "ref: refs/heads/main\n"},
    ".github": {".github/workflows/ci.yml": "on: push\n"},
    "node_modules": {"node_modules/x/index.js": "module.exports=1\n"},
}


@pytest.mark.parametrize("case", list(_EXCLUDED_ONLY_IN_A))
def test_excluded_dir_only_in_smaller_copy_is_conflict(fleet, case):
    _, g = _pair(fleet, {"SKILL.md": _MD, **_EXCLUDED_ONLY_IN_A[case]}, {"SKILL.md": _MD_PLUS})
    assert g.status == "conflict", case
    assert g.keep is None
    assert g.unique_files["default:t/x"] == sorted(_EXCLUDED_ONLY_IN_A[case])


def test_same_git_dir_in_both_is_contained(fleet):
    git = {".git/HEAD": "ref: refs/heads/main\n", ".git/objects/ab/cd": b"\x00blob"}
    _, g = _pair(fleet, {"SKILL.md": _MD, **git}, {"SKILL.md": _MD_PLUS, **git})
    assert g.status == "contained"
    assert g.keep is not None and g.keep.id == "device:t/x"


def test_excluded_dir_text_is_compared_byte_for_byte(fleet):
    a = {"SKILL.md": _MD, ".git/config": "[core]\n"}
    b = {"SKILL.md": _MD_PLUS, ".git/config": "[core]\nbare = false\n"}
    _, g = _pair(fleet, a, b)
    assert g.status == "conflict"


def test_runtime_cache_only_in_smaller_copy_is_contained(fleet):
    a = {
        "SKILL.md": _MD,
        "scripts/run.py": "print(1)\n",
        "scripts/run.pyc": b"\x00bytecode",
        "__pycache__/run.cpython-314.pyc": b"\x00bytecode",
        ".pytest_cache/v/cache/nodeids": "[]\n",
        "scripts/.ruff_cache/x": "cache\n",
    }
    b = {"SKILL.md": _MD_PLUS, "scripts/run.py": "print(1)\n"}
    _, g = _pair(fleet, a, b)
    assert g.status == "contained"
    assert g.keep is not None and g.keep.id == "device:t/x"


def test_sourceless_pyc_counts_as_content(fleet):
    a = {"SKILL.md": _MD, "scripts/tool.pyc": b"\x00shipped"}
    _, g = _pair(fleet, a, {"SKILL.md": _MD_PLUS})
    assert g.status == "conflict"
    assert g.unique_files["default:t/x"] == ["scripts/tool.pyc"]


def test_copies_differing_only_in_git_are_not_identical(fleet):
    a = {"SKILL.md": _MD, ".git/HEAD": "ref: refs/heads/main\n"}
    b = {"SKILL.md": _MD, ".git/HEAD": "ref: refs/heads/dev\n"}
    _, g = _pair(fleet, a, b)
    copies = {c.id: c for c in g.copies}
    assert copies["default:t/x"].tree_hash == copies["device:t/x"].tree_hash
    assert g.status == "conflict"
    assert g.keep is None


def test_copies_differing_only_in_runtime_cache_are_identical(fleet):
    a = {"SKILL.md": _MD, "__pycache__/m.cpython-314.pyc": b"\x00one"}
    b = {"SKILL.md": _MD, "__pycache__/m.cpython-314.pyc": b"\x00two"}
    _, g = _pair(fleet, a, b)
    assert g.status == "identical"
    assert g.keep is not None and g.keep.id == "default:t/x"


def test_excluded_dir_difference_in_one_profile_is_dup(fleet):
    a = {"SKILL.md": _MD, ".git/HEAD": "ref: refs/heads/main\n"}
    b = {"SKILL.md": _MD}
    root = fleet({"default": {"a/x": a, "b/x": b}})
    assert _groups(root)["x"].status == "in-profile-dup"


def test_fingerprint_and_same_content(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_files(a, {"SKILL.md": _MD, "n.txt": "x\n"})
    write_files(b, {"SKILL.md": _MD, "n.txt": "x\n"})
    assert fingerprint(a) == fingerprint(b) and same_content(a, b)
    (b / "empty").mkdir()
    assert fingerprint(a) != fingerprint(b) and not same_content(a, b)
    plain = fingerprint(a)
    os.chmod(a / "n.txt", 0o755)
    assert fingerprint(a) != plain


def test_compare_variants(classic_fleet):
    g = _groups(classic_fleet)["conflict"]
    stats = compare_variants(g.copies)
    assert {cid: n for cid, (n, _) in stats.items()} == g.unique_lines
