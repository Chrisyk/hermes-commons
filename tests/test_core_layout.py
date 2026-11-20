"""Tests for commons.core.layout: commons state lives outside every skills root."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commons.core.backup import create_backup
from commons.core.fleet import Profile, discover
from commons.core.layout import Layout
from tests.fleet import skill_md, write_files
from tests.test_hash_fidelity import _hermes_python, _hermes_src

NOW = datetime(2026, 11, 20, 9, 30, 5, tzinfo=UTC)


def _layout(tmp_path: Path, root: Path) -> Layout:
    return Layout(shared_root=tmp_path / "shared", state_dir=root / "commons")


def test_paths(tmp_path):
    lay = Layout(shared_root=tmp_path / "s", state_dir=tmp_path / "st")
    assert lay.backups_dir == tmp_path / "st" / "backups"
    assert lay.staging_dir == tmp_path / "st" / "staging"
    assert lay.proposals_dir == tmp_path / "st" / "proposals"
    assert lay.journal_path("b1") == tmp_path / "st" / "backups" / "b1" / "journal.json"
    assert lay.commons_dir == tmp_path / "s" / ".commons"
    assert lay.lock_path == tmp_path / "s" / ".commons" / "lock"
    assert lay.config_path == tmp_path / "s" / ".commons" / "config.json"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b"])
def test_journal_path_rejects_bad_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        Layout(tmp_path / "s", tmp_path / "st").journal_path(bad)


def test_check_accepts_default_home_commons(classic_fleet, tmp_path):
    _layout(tmp_path, classic_fleet).check(discover(classic_fleet))


def test_check_rejects_state_inside_shared_root(classic_fleet, tmp_path):
    shared = tmp_path / "shared"
    for state in (shared, shared / ".commons" / "state"):
        with pytest.raises(ValueError, match="shared root"):
            Layout(shared, state).check(discover(classic_fleet))


def test_check_rejects_state_inside_a_skills_dir(classic_fleet, tmp_path):
    profiles = discover(classic_fleet)
    for state in (classic_fleet / "skills", classic_fleet / "profiles" / "device" / "skills" / "x"):
        with pytest.raises(ValueError, match="skills"):
            Layout(tmp_path / "shared", state).check(profiles)


def test_check_rejects_skills_dir_inside_state(classic_fleet, tmp_path):
    with pytest.raises(ValueError):
        Layout(tmp_path / "shared", classic_fleet / "profiles").check(discover(classic_fleet))
    with pytest.raises(ValueError):
        Layout(tmp_path / "state" / "shared", tmp_path / "state").check([])


def test_check_rejects_state_inside_create_dir_or_external(tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    ext, create = tmp_path / "ext", tmp_path / "create"
    profile = Profile("default", home, {"skills": {"create_dir": str(create), "external_dirs": [str(ext)]}})
    Layout(tmp_path / "shared", tmp_path / "state").check([profile])
    with pytest.raises(ValueError, match="create_dir"):
        Layout(tmp_path / "shared", create / "state").check([profile])
    with pytest.raises(ValueError, match="external"):
        Layout(tmp_path / "shared", ext).check([profile])


def test_check_resolves_symlinks(tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(home / "skills", target_is_directory=True)
    with pytest.raises(ValueError):
        Layout(tmp_path / "shared", tmp_path / "alias" / "state").check([Profile("default", home)])


_WALK_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import hermes_bootstrap  # noqa: F401
from pathlib import Path
from agent.skill_utils import iter_skill_index_files
print(json.dumps({d: [str(p) for p in iter_skill_index_files(Path(d), "SKILL.md")] for d in sys.argv[2:]}))
"""


def _hermes_walk(tmp_path: Path, roots: list[Path]) -> dict[str, list[str]]:
    python, src = _hermes_python(), _hermes_src()
    if python is None or src is None:
        pytest.skip("no Hermes runtime python or Hermes source checkout found")
    work = tmp_path / "cwd"
    work.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(work), "HERMES_HOME": str(work / "hermes")}
    proc = subprocess.run(
        [str(python), "-I", "-c", _WALK_PROBE, str(src), *map(str, roots)],
        check=False,
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_real_hermes_never_lists_commons_state(classic_fleet, tmp_path):
    profiles = discover(classic_fleet)
    lay = _layout(tmp_path, classic_fleet)
    lay.check(profiles)
    write_files(lay.shared_root, {"general/same/SKILL.md": skill_md("same", "d", ["S1"])})
    (lay.commons_dir).mkdir(parents=True)
    (lay.commons_dir / "config.json").write_text("{}\n", encoding="utf-8")
    bid = create_backup(lay, profiles, "setup", now=NOW)
    write_files(lay.staging_dir / "conflict", {"SKILL.md": skill_md("conflict", "d", ["merged"])})
    roots = [p.skills_dir for p in profiles] + [lay.shared_root]
    found = _hermes_walk(tmp_path, [*roots, lay.state_dir])
    state = str(lay.state_dir.resolve())
    for root in roots:
        assert found[str(root)], f"Hermes found nothing in {root}"
        assert not [p for p in found[str(root)] if p.startswith(state + os.sep)], root
    # Control: the state dir really holds skill-shaped files Hermes would list.
    listed = found[str(lay.state_dir)]
    assert any(f"{os.sep}backups{os.sep}{bid}{os.sep}" in p for p in listed)
    assert any(f"{os.sep}staging{os.sep}conflict{os.sep}" in p for p in listed)
