"""Tests for commons.core.report status report."""

import json
import os
import subprocess

import pytest
import yaml

from commons.core.fleet import Profile, discover
from commons.core.report import (
    clip_description,
    disabled_names,
    environment_active,
    index_lines,
    index_row_chars,
    platform_matches,
    render_status,
    status,
)
from commons.core.resolve import ScanCache, build_index
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
    assert by_name["pikvm"].resolvable == {"project": 0, "local": 2, "shared": 0, "external": 0}
    assert by_name["default"].shadowed_shared == []
    assert by_name["default"].resolvable == {"project": 0, "local": 1, "shared": 1, "external": 0}


def test_render_shows_shadowed_per_profile(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    out = render_status(_status(root))
    assert "shadowed: 1" in _section(out, "pikvm")
    assert "same" in _section(out, "pikvm")
    assert "shadowed: 0" in _section(out, "default")
    assert "shared: 1" in _section(out, "default")
    assert "local: 2" in _section(out, "pikvm")
    assert "ambiguous: 0" in out
    assert "indexed:" in out
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


def test_index_chars_sums_rendered_lines(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    default = {p.profile: p for p in _status(root).profiles}["default"]
    lines = ["  general:", "    - mine-default: Own skill.", "    - same: Shared copy."]
    assert default.indexed == 2
    assert default.index_chars == sum(len(line) + 1 for line in lines)
    out = render_status(_status(root), profile="default")
    assert f"indexed: 2 (~{default.index_chars} chars)" in out
    assert "resolvable: 2 (project: 0, local: 1, shared: 1, external: 0)" in out


def test_no_shared_root(classic_fleet):
    report = _status(classic_fleet)
    assert report.shared_root is None
    assert "shared root: none" in render_status(report)


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
    assert index_row_chars("name", "desc", indent="  ") == len("  - name: desc") + 1


def _md(name, desc, extra=""):
    return f"---\nname: {name}\ndescription: {desc}\n{extra}---\nbody line\n"


def _index_fixture(tmp_path, config_extra=None):
    """A small profile with every filter the index mirrors; returns (Profile, shared, ext)."""
    home = tmp_path / "hermes"
    shared, ext = tmp_path / "shared", tmp_path / "ext"
    local = {
        "general/plain/SKILL.md": _md("plain", "Plain skill."),
        "general/folded/SKILL.md": (
            "---\nname: folded\ndescription: >\n  A folded description that runs well past the sixty\n"
            "  character budget of the index.\n---\n"
        ),
        "general/quoted/SKILL.md": _md("quoted", "'Single quoted.'"),
        "general/nodesc/SKILL.md": "---\nname: nodesc\n---\n",
        "apple/mac-only/SKILL.md": _md("mac-only", "Mac only.", "platforms: [macos]\n"),
        "apple/linux-ok/SKILL.md": _md("linux-ok", "Linux too.", "platforms: [linux, macos]\n"),
        "boards/kb/SKILL.md": _md("kb", "Kanban only.", "environments: [kanban]\n"),
        "boards/anywhere/SKILL.md": _md("anywhere", "Unknown env.", "environments: [nowhere-known]\n"),
        "general/off/SKILL.md": _md("off", "Disabled by name."),
        "general/off-dir/SKILL.md": _md("renamed-off", "Disabled by folder."),
        "general/pd/SKILL.md": _md("pd", "Disabled on cli."),
        "meta/hermes-agent/SKILL.md": _md("hermes-agent", "Essential."),
        "solo/SKILL.md": _md("solo", "Top level skill."),
        "x/dup/SKILL.md": _md("dup", "First dup."),
        "y/dup/SKILL.md": _md("dup", "Second dup."),
        "mine/shadow/SKILL.md": _md("shadow", "Local wins."),
    }
    for rel, text in local.items():
        write_files(home / "skills", {rel: text})
    write_files(shared, {"s/shared-one/SKILL.md": _md("shared-one", "From shared.")})
    write_files(shared, {"s/shadow/SKILL.md": _md("shadow", "Shared loses.")})
    write_files(ext, {"cat/sub/deep/SKILL.md": _md("deep", "Nested category.")})
    skills_cfg = {
        "create_dir": str(shared),
        "external_dirs": [str(ext)],
        "disabled": ["off", "off-dir", "hermes-agent"],
        "platform_disabled": {"cli": ["pd"]},
    }
    config = {"skills": skills_cfg, "plugins": {"enabled": []}, **(config_extra or {})}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return Profile("default", home, config), shared, ext


def _lines(profile, platform=None):
    index = build_index(profile, dir_hash_md5, hub_content_hash)
    return index_lines(index, profile.config, platform=platform, environ={})


def test_index_filters_host_and_config(tmp_path):
    profile, _, _ = _index_fixture(tmp_path)
    lines = _lines(profile)
    text = "\n".join(lines)
    assert "mac-only" not in text
    assert "- linux-ok: Linux too." in text
    assert "- kb:" not in text
    assert "- anywhere: Unknown env." in text
    assert "- off:" not in text and "renamed-off" not in text
    assert "- pd: Disabled on cli." in text
    assert "- hermes-agent: Essential." in text
    assert "    - plain: Plain skill." in lines
    assert "    - quoted: Single quoted." in lines
    assert "    - nodesc" in lines
    assert "  - solo: Top level skill." in lines
    assert "  - deep: Nested category." in lines and "  cat/sub:" not in lines
    assert "  - x/dup: First dup." in lines and "  - y/dup: Second dup." in lines
    assert "  - shadow: Local wins." in lines
    folded = next(line for line in lines if "- folded:" in line)
    assert folded == "    - folded: " + clip_description(
        "A folded description that runs well past the sixty character budget of the index."
    )


def test_index_platform_disabled_and_kanban_opt_in(tmp_path):
    profile, _, _ = _index_fixture(tmp_path, {"toolsets": ["hermes-cli", "kanban"]})
    text = "\n".join(_lines(profile, platform="cli"))
    assert "- pd:" not in text
    assert "- kb: Kanban only." in text


def test_platform_matches():
    assert platform_matches(None, "linux")
    assert platform_matches(["macos"], "darwin")
    assert not platform_matches(["macos"], "linux")
    assert platform_matches("Linux", "linux")
    assert platform_matches(["win"], "win32")


def test_environment_detection():
    assert environment_active("unknown-tag", {}, {})
    assert not environment_active("kanban", {}, {})
    assert environment_active("kanban", {}, {"HERMES_KANBAN_TASK": "t1"})
    assert not environment_active(
        "kanban", {}, {"HERMES_KANBAN_TASK": "t1", "HERMES_DELEGATED_CHILD_CONTEXT": "1"}
    )
    assert environment_active("kanban", {"toolsets": ["kanban"]}, {})
    assert environment_active("kanban", {"platform_toolsets": {"telegram": ["web", "kanban"]}}, {})
    assert not environment_active("kanban", {"platform_toolsets": {"cli": '["kanban"]'}}, {})


def test_disabled_names():
    cfg = {"skills": {"disabled": '["a", "hermes-agent"]', "platform_disabled": {"cli": ["b"]}}}
    assert disabled_names(cfg) == {"a"}
    assert disabled_names(cfg, "cli") == {"a", "b"}
    assert disabled_names({"skills": {"disabled": "single"}}) == {"single"}
    assert disabled_names({}) == set()


def test_status_scans_each_root_once(fleet, tmp_path):
    root, _ = _shared_fleet(fleet, tmp_path)
    cache = ScanCache()
    status(discover(root), dir_hash_md5, hub_content_hash, cache=cache)
    assert cache.scan_calls == len(PROFILES) + 1


_INDEX_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import hermes_bootstrap  # noqa: F401
from agent.prompt_builder import build_skills_system_prompt
text = build_skills_system_prompt()
start = text.index("<available_skills>\n") + len("<available_skills>\n")
end = text.index("\n</available_skills>")
print(json.dumps(text[start:end].split("\n")))
"""


def _hermes_index_lines(tmp_path, home, platform=None):
    python, src = _hermes_python(), _hermes_src()
    if python is None or src is None:
        pytest.skip("no Hermes runtime python or Hermes source checkout found")
    work = tmp_path / "cwd"
    work.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(work), "HERMES_HOME": str(home)}
    if platform:
        env["HERMES_PLATFORM"] = platform
    proc = subprocess.run(
        [str(python), "-I", "-c", _INDEX_PROBE, str(src)],
        check=False,
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("config_extra", "platform"),
    [({}, None), ({"toolsets": ["hermes-cli", "kanban"]}, "cli")],
    ids=["plain", "kanban-cli"],
)
def test_index_matches_real_hermes(tmp_path, config_extra, platform):
    # Hermes wraps the rows in fixed prompt text, so the rows between the
    # <available_skills> tags are compared line by line, and index_chars is
    # checked against their length (one newline per row).
    profile, _, _ = _index_fixture(tmp_path, config_extra)
    ps = status([profile], dir_hash_md5, hub_content_hash, platform=platform).profiles[0]
    ours = _lines(profile, platform=platform)
    real = _hermes_index_lines(tmp_path, profile.home, platform)
    assert ours == real
    assert ps.index_chars == sum(len(line) + 1 for line in real)
    assert ps.indexed == sum(1 for line in real if line.lstrip().startswith("- "))
