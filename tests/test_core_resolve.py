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


_RESOLVE_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import hermes_bootstrap  # noqa: F401
from tools.skills_tool import locate_skill, skill_search_dirs
roots, _ = skill_search_dirs()
out = {}
for name in sys.argv[2:]:
    err, _sd, smd = locate_skill(name, None, roots)
    out[name] = None if err else str(smd.parent)
print(json.dumps({"roots": [[t, str(d)] for t, d in roots], "found": out}))
"""


def test_matches_real_hermes_locate_skill(tmp_path):
    python, src = _hermes_python(), _hermes_src()
    if python is None or src is None:
        pytest.skip("no Hermes runtime python or Hermes source checkout found")
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
    (home / "config.yaml").write_text(yaml.safe_dump(profile.config), encoding="utf-8")
    names = ["tool", "onlyshared", "dup", "twin", "flat"]
    work = tmp_path / "cwd"
    work.mkdir()
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
    real = json.loads(proc.stdout.strip().splitlines()[-1])
    ours = _resolve(profile)
    assert [(t, Path(d)) for t, d in real["roots"]] == [(r.tier, r.path) for r in search_roots(profile)]
    for name in names:
        winner = ours[name].winner
        assert (None if winner is None else str(winner.path)) == real["found"][name], name
