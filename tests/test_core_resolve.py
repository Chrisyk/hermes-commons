"""Tests for commons.core.resolve (Hermes skill precedence)."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from commons.core.fleet import Profile
from commons.core.resolve import (
    TIER_CREATE_DIR,
    TIER_EXTERNAL,
    TIER_LOCAL,
    TIER_PROJECT,
    build_index,
    locate,
    resolve,
    search_roots,
)
from commons.core.scan import scan_profile, scan_root
from tests.fleet import dir_hash_md5, hub_content_hash, skill_md, write_files
from tests.test_hash_fidelity import _hermes_python, _hermes_src


def _skill(root: Path, rel: str, name: str, lines: list[str]) -> Path:
    write_files(root / rel, {"SKILL.md": skill_md(name, f"About {name}.", lines)})
    return root / rel


def _profile(tmp_path: Path, create_dir=None, external=None) -> Profile:
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True, exist_ok=True)
    skills: dict = {}
    if create_dir is not None:
        skills["create_dir"] = str(create_dir)
    if external is not None:
        skills["external_dirs"] = [str(e) for e in external]
    return Profile(name="default", home=home, config={"skills": skills})


def _resolve(profile, project_dirs=()):
    return resolve(profile, dir_hash_md5, hub_content_hash, project_dirs=project_dirs)


def test_local_copy_shadows_shared(tmp_path):
    shared = tmp_path / "shared"
    _skill(shared, "general/same", "same", ["shared body"])
    profile = _profile(tmp_path, create_dir=shared)
    _skill(profile.skills_dir, "general/same", "same", ["local body"])
    res = _resolve(profile)["same"]
    assert res.winner_tier == TIER_LOCAL
    assert res.winner is not None and res.winner.path == profile.skills_dir / "general/same"
    assert [(t, c.path) for t, c in res.shadowed] == [(TIER_CREATE_DIR, shared / "general/same")]
    assert res.ambiguous == []


def test_shared_only_resolves_from_shared(tmp_path):
    shared = tmp_path / "shared"
    _skill(shared, "general/only", "only", ["shared body"])
    profile = _profile(tmp_path, create_dir=shared)
    res = _resolve(profile)["only"]
    assert res.winner_tier == TIER_CREATE_DIR
    assert res.winner is not None and res.winner.profile == "shared"
    assert res.shadowed == []


def test_external_loses_to_shared(tmp_path):
    shared, ext = tmp_path / "shared", tmp_path / "ext"
    _skill(shared, "x/tool", "tool", ["shared"])
    _skill(ext, "y/tool", "tool", ["external"])
    profile = _profile(tmp_path, create_dir=shared, external=[ext])
    res = _resolve(profile)["tool"]
    assert res.winner_tier == TIER_CREATE_DIR
    assert [(t, c.path) for t, c in res.shadowed] == [(TIER_EXTERNAL, ext / "y/tool")]


def test_project_beats_local(tmp_path):
    project = tmp_path / "repo" / ".hermes" / "skills"
    _skill(project, "p/tool", "tool", ["project"])
    profile = _profile(tmp_path)
    _skill(profile.skills_dir, "l/tool", "tool", ["local"])
    res = _resolve(profile, project_dirs=[project])["tool"]
    assert res.winner_tier == TIER_PROJECT
    assert [t for t, _ in res.shadowed] == [TIER_LOCAL]


def test_two_different_copies_in_one_tier_are_ambiguous(tmp_path):
    shared = tmp_path / "shared"
    _skill(shared, "s/dup", "dup", ["shared"])
    profile = _profile(tmp_path, create_dir=shared)
    _skill(profile.skills_dir, "a/dup", "dup", ["first"])
    _skill(profile.skills_dir, "b/dup", "dup", ["second"])
    res = _resolve(profile)["dup"]
    assert res.winner is None
    assert res.winner_tier == TIER_LOCAL
    assert sorted(c.rel for c in res.ambiguous) == ["a/dup", "b/dup"]
    assert [(t, c.rel) for t, c in res.shadowed] == [(TIER_CREATE_DIR, "s/dup")]


def test_byte_identical_copies_in_one_root_resolve_to_shallowest(tmp_path):
    profile = _profile(tmp_path)
    _skill(profile.skills_dir, "a/deep/twin", "twin", ["same body"])
    _skill(profile.skills_dir, "b/twin", "twin", ["same body"])
    res = _resolve(profile)["twin"]
    assert res.ambiguous == []
    assert res.winner is not None and res.winner.rel == "b/twin"
    assert [(t, c.rel) for t, c in res.shadowed] == [(TIER_LOCAL, "a/deep/twin")]


def test_identical_copies_at_same_depth_stay_ambiguous(tmp_path):
    profile = _profile(tmp_path)
    _skill(profile.skills_dir, "a/twin", "twin", ["same body"])
    _skill(profile.skills_dir, "b/twin", "twin", ["same body"])
    res = _resolve(profile)["twin"]
    assert res.winner is None
    assert len(res.ambiguous) == 2


def test_identical_copies_in_two_roots_of_one_tier_stay_ambiguous(tmp_path):
    ext1, ext2 = tmp_path / "ext1", tmp_path / "ext2"
    _skill(ext1, "t", "tool", ["same body"])
    _skill(ext2, "deeper/t", "tool", ["same body"])
    profile = _profile(tmp_path, external=[ext1, ext2])
    res = _resolve(profile)["tool"]
    assert res.winner is None
    assert res.winner_tier == TIER_EXTERNAL
    assert len(res.ambiguous) == 2


def test_symlink_view_of_same_skill_md_is_one_candidate(tmp_path):
    shared = tmp_path / "shared"
    target = _skill(shared, "general/viewed", "viewed", ["body"])
    profile = _profile(tmp_path, create_dir=shared)
    (profile.skills_dir / "general").mkdir()
    (profile.skills_dir / "general" / "viewed").symlink_to(target, target_is_directory=True)
    res = _resolve(profile)["viewed"]
    assert res.ambiguous == []
    assert res.winner_tier == TIER_LOCAL
    assert res.shadowed == []


def test_missing_create_dir_is_ignored(tmp_path):
    profile = _profile(tmp_path, create_dir=tmp_path / "nowhere")
    _skill(profile.skills_dir, "g/local", "local", ["x"])
    res = _resolve(profile)
    assert set(res) == {"local"}
    assert res["local"].winner_tier == TIER_LOCAL


def test_external_equal_to_create_dir_is_deduplicated(tmp_path):
    shared = tmp_path / "shared"
    _skill(shared, "g/tool", "tool", ["x"])
    profile = _profile(tmp_path, create_dir=shared, external=[shared, tmp_path / "missing"])
    res = _resolve(profile)["tool"]
    assert res.winner_tier == TIER_CREATE_DIR
    assert res.shadowed == []
    assert res.ambiguous == []


def test_create_dir_equal_to_local_is_deduplicated(tmp_path):
    profile = _profile(tmp_path)
    profile = Profile(profile.name, profile.home, {"skills": {"create_dir": str(profile.skills_dir)}})
    _skill(profile.skills_dir, "g/tool", "tool", ["x"])
    res = _resolve(profile)["tool"]
    assert res.winner_tier == TIER_LOCAL
    assert res.shadowed == []


def test_relative_create_dir_is_anchored_at_home(tmp_path):
    profile = _profile(tmp_path)
    profile = Profile(profile.name, profile.home, {"skills": {"create_dir": "shared"}})
    _skill(profile.home / "shared", "g/tool", "tool", ["x"])
    res = _resolve(profile)["tool"]
    assert res.winner_tier == TIER_CREATE_DIR


def test_scan_root_labels_copies_and_reads_provenance(tmp_path):
    root = tmp_path / "shared"
    _skill(root, "g/plain", "plain", ["x"])
    pristine = _skill(root, "g/bundled", "bundled", ["y"])
    (root / ".bundled_manifest").write_text(f"bundled:{dir_hash_md5(pristine)}\n", encoding="utf-8")
    copies = {c.rel: c for c in scan_root(root, "shared", dir_hash_md5, hub_content_hash)}
    assert copies["g/plain"].profile == "shared"
    assert copies["g/plain"].kind == "own"
    assert copies["g/bundled"].kind == "bundled-pristine"
    assert copies["g/plain"].id == "shared:g/plain"


def test_scan_profile_delegates_to_scan_root(classic_fleet):
    home = classic_fleet / "profiles" / "device"
    profile = Profile("device", home, {})
    assert scan_profile(profile, dir_hash_md5, hub_content_hash) == scan_root(
        home / "skills", "device", dir_hash_md5, hub_content_hash
    )


def test_scan_root_missing_dir(tmp_path):
    assert scan_root(tmp_path / "nope", "x", dir_hash_md5, hub_content_hash) == []


def test_folder_name_match_beats_shared_declared_name(tmp_path):
    shared = tmp_path / "shared"
    _skill(shared, "s/baz", "foo", ["shared foo"])
    profile = _profile(tmp_path, create_dir=shared)
    _skill(profile.skills_dir, "l/foo", "bar", ["local folder foo"])
    res = _resolve(profile)
    assert set(res) == {"bar", "foo"}
    assert res["foo"].winner is not None and res["foo"].winner.rel == "l/foo"
    assert res["foo"].winner_tier == TIER_LOCAL
    assert [(t, c.rel) for t, c in res["foo"].shadowed] == [(TIER_CREATE_DIR, "s/baz")]
    assert res["bar"].winner is not None and res["bar"].winner.rel == "l/foo"


def test_folder_and_declared_name_in_one_tier_are_ambiguous(tmp_path):
    profile = _profile(tmp_path)
    _skill(profile.skills_dir, "alpha", "beta", ["one"])
    _skill(profile.skills_dir, "beta", "gamma", ["two"])
    res = _resolve(profile)["beta"]
    assert res.winner is None
    assert sorted(c.rel for c in res.ambiguous) == ["alpha", "beta"]


def test_non_string_declared_name_matches_nothing(tmp_path):
    profile = _profile(tmp_path)
    _skill(profile.skills_dir, "g/num", "123", ["x"])
    res = _resolve(profile)
    assert set(res) == {"123"}
    assert res["123"].winner is None and res["123"].winner_tier is None
    assert res["123"].winner_path is None


def test_legacy_flat_file_takes_part(tmp_path):
    shared, ext = tmp_path / "shared", tmp_path / "ext"
    write_files(shared, {"notes/legacy.md": "legacy flat skill\n"})
    _skill(ext, "e/legacy", "legacy", ["external"])
    profile = _profile(tmp_path, create_dir=shared, external=[ext])
    res = _resolve(profile)["legacy"]
    assert res.winner is None and res.ambiguous == []
    assert res.winner_tier == TIER_CREATE_DIR
    assert res.winner_path == shared / "notes/legacy.md"
    assert [(t, c.rel) for t, c in res.shadowed] == [(TIER_EXTERNAL, "e/legacy")]


def test_nested_root_owns_its_skills(tmp_path):
    shared = tmp_path / "shared"
    team = shared / "team"
    _skill(team, "n/nest", "nest", ["x"])
    _skill(shared, "both", "both", ["same"])
    _skill(team, "both", "both", ["same"])
    profile = _profile(tmp_path, create_dir=shared, external=[team])
    assert [(r.tier, r.path) for r in search_roots(profile)] == [
        (TIER_LOCAL, profile.skills_dir),
        (TIER_CREATE_DIR, shared.resolve()),
        (TIER_EXTERNAL, team.resolve()),
    ]
    res = _resolve(profile)
    assert res["nest"].winner_tier == TIER_EXTERNAL
    assert res["both"].winner_tier == TIER_CREATE_DIR
    assert res["both"].winner is not None and res["both"].winner.path == shared.resolve() / "both"
    assert [t for t, _ in res["both"].shadowed] == [TIER_EXTERNAL]


def test_roots_dedupe_by_raw_path(tmp_path):
    real = tmp_path / "real-skills"
    _skill(real, "g/tool", "tool", ["x"])
    home = tmp_path / "home"
    home.mkdir()
    (home / "skills").symlink_to(real, target_is_directory=True)
    profile = Profile("default", home, {"skills": {"create_dir": str(real), "external_dirs": [str(real)]}})
    assert [(r.tier, r.path) for r in search_roots(profile)] == [(TIER_LOCAL, home / "skills")]
    assert _resolve(profile)["tool"].winner_tier == TIER_LOCAL


_RESOLVE_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import hermes_bootstrap  # noqa: F401
from tools.skills_tool import _resolve_plugin_skill, locate_skill, skill_lookup_path_error, skill_search_dirs
roots, _ = skill_search_dirs()
out = {}
for name in sys.argv[2:]:
    # The steps skill_view runs before locate_skill.
    if skill_lookup_path_error(name):
        out[name] = None
        continue
    category = None
    if ":" in name:
        served, category = _resolve_plugin_skill(name, None, None, False)
        if served is not None or (category and skill_lookup_path_error(category)):
            out[name] = None
            continue
    err, _sd, smd = locate_skill(name, category, roots)
    out[name] = None if err else str(smd)
print(json.dumps({"roots": [[t, str(d)] for t, d in roots], "found": out}))
"""


def _hermes_locate(tmp_path, home: Path, names: list[str]) -> dict:
    python, src = _hermes_python(), _hermes_src()
    if python is None or src is None:
        pytest.skip("no Hermes runtime python or Hermes source checkout found")
    work = tmp_path / "cwd"
    work.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(work), "HERMES_HOME": str(home)}
    proc = subprocess.run(
        [str(python), "-I", "-c", _RESOLVE_PROBE, str(src), *names],
        check=False,
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_matches_hermes(tmp_path, profile: Profile, extra_names=()) -> dict:
    (profile.home / "config.yaml").write_text(yaml.safe_dump(profile.config), encoding="utf-8")
    ours = _resolve(profile)
    names = sorted({*ours, *extra_names})
    real = _hermes_locate(tmp_path, profile.home, names)
    assert [(t, Path(d)) for t, d in real["roots"]] == [(r.tier, r.path) for r in search_roots(profile)]
    index = build_index(profile, dir_hash_md5, hub_content_hash)
    for name in names:
        res = ours[name] if name in ours else locate(index, name)
        path = None if res.winner_path is None else str(res.winner_path)
        assert path == real["found"][name], name
    return ours


def test_matches_real_hermes_locate_skill(tmp_path):
    shared, ext = tmp_path / "shared", tmp_path / "ext"
    home = tmp_path / "hermes"
    profile = Profile(
        "default", home, {"skills": {"create_dir": str(shared), "external_dirs": [str(ext), str(shared)]}}
    )
    local = profile.skills_dir
    _skill(local, "l/tool", "tool", ["local"])
    _skill(shared, "s/tool", "tool", ["shared"])
    _skill(ext, "e/tool", "tool", ["external"])
    _skill(shared, "s/onlyshared", "onlyshared", ["x"])
    _skill(ext, "e/onlyshared", "onlyshared", ["y"])
    _skill(local, "a/dup", "dup", ["one"])
    _skill(local, "b/dup", "dup", ["two"])
    _skill(local, "a/deep/twin", "twin", ["same"])
    _skill(local, "b/twin", "twin", ["same"])
    _skill(ext, "x/flat", "flat", ["same"])
    _skill(shared, "y/flat", "flat", ["different"])
    _assert_matches_hermes(tmp_path, profile)


def test_matches_real_hermes_folder_vs_declared_name(tmp_path):
    shared = tmp_path / "shared"
    home = tmp_path / "hermes"
    profile = Profile("default", home, {"skills": {"create_dir": str(shared)}})
    local = profile.skills_dir
    _skill(local, "l/foo", "bar", ["local folder foo"])
    _skill(shared, "s/baz", "foo", ["shared declares foo"])
    _skill(local, "alpha", "beta", ["one"])
    _skill(local, "beta", "gamma", ["two"])
    _skill(local, "g/num", "123", ["x"])
    write_files(shared, {"notes/legacy.md": "legacy flat skill\n"})
    _skill(local, "e/legacy-dir", "legacy", ["local declares legacy"])
    ours = _assert_matches_hermes(
        tmp_path,
        profile,
        extra_names=["baz", "l/foo", "l:foo", "plugin:l:foo", "notes/legacy", "../x", "/abs"],
    )
    assert ours["foo"].winner is not None and ours["foo"].winner.rel == "l/foo"


def test_matches_real_hermes_nested_roots(tmp_path):
    shared = tmp_path / "shared"
    team = shared / "team"
    hidden = shared / ".archive" / "ext"
    home = tmp_path / "hermes"
    profile = Profile(
        "default", home, {"skills": {"create_dir": str(shared), "external_dirs": [str(team), str(hidden)]}}
    )
    _skill(team, "n/nest", "nest", ["x"])
    _skill(shared, "both", "both", ["same"])
    _skill(team, "both", "both", ["same"])
    _skill(team, "a/two", "two", ["one"])
    _skill(team, "b/two", "two", ["other"])
    _skill(hidden, "z", "zed", ["same"])
    _skill(hidden, "deep/z", "zed", ["same"])
    _skill(profile.skills_dir, "l/local", "local", ["x"])
    _assert_matches_hermes(tmp_path, profile)
