"""Tests for commons.core.report status report."""

import json
import os
import subprocess

import pytest
import yaml

from commons.core.fleet import discover
from commons.core.report import clip_description, index_row_chars, read_description, render_status, status
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files
from tests.test_hash_fidelity import _hermes_python, _hermes_src

PROFILES = ("default", "device", "pikvm", "pocket")


def _shared_fleet(fleet, tmp_path):
    spec = {p: {"general/mine": {"SKILL.md": skill_md(f"mine-{p}", "Own skill.", ["x"])}} for p in PROFILES}
    spec["pikvm"]["general/same"] = {"SKILL.md": skill_md("same", "Local copy.", ["local body"])}
    root = fleet(spec)
    shared = tmp_path / "shared"
    write_files(shared / "general/same", {"SKILL.md": skill_md("same", "Shared copy.", ["shared body"])})
    for p in PROFILES:
        cfg = {"skills": {"create_dir": str(shared)}, "plugins": {"enabled": []}}
        (profile_home(root, p) / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return root, shared


def _status(root):
    return status(discover(root), dir_hash_md5, hub_content_hash)


def _section(out, profile):
    lines = out.splitlines()
    start = lines.index(f"profile {profile}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("profile ")), len(lines))
    return "\n".join(lines[start:end])


def test_status_counts_and_shadow(fleet, tmp_path):
    root, shared = _shared_fleet(fleet, tmp_path)
    report = _status(root)
    assert report.shared_root == shared
    by_name = {p.profile: p for p in report.profiles}
    assert [p.profile for p in report.profiles] == list(PROFILES)
    assert by_name["pikvm"].shadowed_shared == ["same"]
    assert by_name["pikvm"].counts == {"project": 0, "local": 2, "shared": 0, "external": 0}
    assert by_name["default"].shadowed_shared == []
    assert by_name["default"].counts == {"project": 0, "local": 1, "shared": 1, "external": 0}


def test_render_shows_shadowed_per_profile(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    out = render_status(_status(root))
    assert "shadowed: 1" in _section(out, "pikvm")
    assert "same" in _section(out, "pikvm")
    assert "shadowed: 0" in _section(out, "default")
    assert "shared: 1" in _section(out, "default")
    assert "local: 2" in _section(out, "pikvm")
    assert "ambiguous: 0" in out
    assert "index:" in out
    assert "\x1b" not in out


def test_render_profile_filter(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    out = render_status(_status(root), profile="pikvm")
    assert "profile pikvm" in out
    assert "profile default" not in out


def test_render_skill_filter_shows_winning_path(fleet, tmp_path):
    root, shared = _shared_fleet(fleet, tmp_path)
    out = render_status(_status(root), skill="same")
    pikvm_local = profile_home(root, "pikvm") / "skills" / "general" / "same"
    assert f"same: local {pikvm_local}" in _section(out, "pikvm")
    assert f"shadows shared {shared / 'general' / 'same'}" in _section(out, "pikvm")
    assert f"same: shared {shared / 'general' / 'same'}" in _section(out, "default")
    assert "shadows" not in _section(out, "default")


def test_render_skill_filter_missing_and_ambiguous(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    skills = profile_home(root, "pocket") / "skills"
    write_files(skills / "a/dup", {"SKILL.md": skill_md("dup", "d", ["one"])})
    write_files(skills / "b/dup", {"SKILL.md": skill_md("dup", "d", ["two"])})
    report = _status(root)
    pocket = {p.profile: p for p in report.profiles}["pocket"]
    assert pocket.ambiguous == ["dup"]
    out = render_status(report, skill="dup")
    assert "dup: not found" in _section(out, "default")
    assert "dup: ambiguous in local" in _section(out, "pocket")
    assert str(skills / "a" / "dup") in _section(out, "pocket")
    assert "ambiguous: 1 (dup)" in render_status(report, profile="pocket")


def test_render_skill_filter_uses_given_resolutions(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    report = _status(root)
    out = render_status(report, skill="same", resolutions={p.profile: {} for p in report.profiles})
    assert "same: not found" in _section(out, "pikvm")


def test_index_chars_sums_winner_rows(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    default = {p.profile: p for p in _status(root).profiles}["default"]
    expected = len("    - mine-default: Own skill.") + 1 + len("    - same: Shared copy.") + 1
    assert default.index_chars == expected
    assert "index: ~" + str(expected) + " chars" in render_status(_status(root), profile="default")


def test_no_shared_root(classic_fleet):
    report = _status(classic_fleet)
    assert report.shared_root is None
    assert "shared root: none" in render_status(report)


def test_read_description(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_text('---\nname: x\ndescription: "Quoted text."\n---\nbody\n', encoding="utf-8")
    assert read_description(md) == "Quoted text."
    md.write_text("---\nname: x\n---\ndescription: in body\n", encoding="utf-8")
    assert read_description(md) == ""
    assert read_description(tmp_path / "missing.md") == ""


def test_clip_description_mirrors_hermes():
    assert clip_description("short") == "short"
    exact = "x" * 60
    assert clip_description(exact) == exact
    for text, expected in _CLIP_CASES.items():
        assert clip_description(text) == expected, text


_CLIP_CASES = {
    "Use when doing a very long thing with many words, more and more words here": (
        "Use when doing a very long thing with many words, more..."
    ),
    "Use when x, y and z happen together in one big long sentence; really long": (
        "Use when x, y and z happen together in one big long..."
    ),
    "y" * 80: "y" * 57 + "...",
    "a " + "b" * 70: "a...",
    "x" * 61: "x" * 57 + "...",
}

_CLIP_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import hermes_bootstrap  # noqa: F401
from agent.skill_utils import clip_skill_description
print(json.dumps({t: clip_skill_description(t) for t in json.loads(sys.argv[2])}))
"""


def test_clip_matches_real_hermes(tmp_path):
    python, src = _hermes_python(), _hermes_src()
    if python is None or src is None:
        pytest.skip("no Hermes runtime python or Hermes source checkout found")
    texts = [*_CLIP_CASES, "short", "z" * 60, "tab\tseparated " * 6]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "h")}
    proc = subprocess.run(
        [str(python), "-I", "-c", _CLIP_PROBE, str(src), json.dumps(texts)],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    real = json.loads(proc.stdout.strip().splitlines()[-1])
    assert {t: clip_description(t) for t in texts} == real


def test_index_row_without_description():
    assert index_row_chars("name", "") == len("    - name") + 1
    assert index_row_chars("name", "desc") == len("    - name: desc") + 1
