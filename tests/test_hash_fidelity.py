"""The fleet hash helpers must reproduce Hermes v0.23.1 byte for byte.

Reference implementations below are copied from Hermes on purpose, so these
tests stay independent of tests/fleet.py.
"""

import hashlib
import json
import re
from contextlib import suppress
from pathlib import Path

import pytest

from tests.fleet import dir_hash_md5, hub_content_hash, make_fleet

_RUNTIME_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _ref_is_runtime_cache(path, skill_dir):
    relative = path.relative_to(skill_dir)
    if any(part in _RUNTIME_CACHE_DIRS for part in relative.parts[:-1]):
        return True
    if path.name in _RUNTIME_CACHE_DIRS and path.is_dir():
        return True
    return path.suffix in {".pyc", ".pyo"} and path.with_suffix(".py").is_file()


def _ref_dir_hash(directory):
    hasher = hashlib.md5()
    with suppress(OSError):
        for fpath in sorted(directory.rglob("*")):
            if not _ref_is_runtime_cache(fpath, directory) and fpath.is_file():
                hasher.update(str(fpath.relative_to(directory)).encode("utf-8"))
                hasher.update(fpath.read_bytes())
    return hasher.hexdigest()


def _posix_ordered_md5(directory):
    hasher = hashlib.md5()
    files = sorted(
        (p.relative_to(directory).as_posix(), p)
        for p in directory.rglob("*")
        if not _ref_is_runtime_cache(p, directory) and p.is_file()
    )
    for rel, p in files:
        hasher.update(rel.encode("utf-8"))
        hasher.update(p.read_bytes())
    return hasher.hexdigest()


def _ref_content_digest(skill_path):
    h = hashlib.sha256()
    for rel, p in sorted(
        (p.relative_to(skill_path).as_posix(), p) for p in skill_path.rglob("*") if p.is_file()
    ):
        h.update(rel.encode("utf-8") + b"\x00")
        h.update(p.read_bytes())
    return h.hexdigest()


def _golden_tree(root: Path) -> Path:
    files = {
        "a/b.md": b"inside a\n",
        "a-b/x": b"inside a-b\n",
        "a.md": b"top level a\n",
        "z/__pycache__/c.pyc": b"cached bytecode",
        "keep.pyc": b"shipped bytecode",
        "mod.py": b"print('mod')\n",
        "mod.pyc": b"stale sibling bytecode",
    }
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def test_dir_hash_md5_matches_reference_path_order(tmp_path):
    tree = _golden_tree(tmp_path / "tree")
    assert dir_hash_md5(tree) == _ref_dir_hash(tree)
    assert _ref_dir_hash(tree) != _posix_ordered_md5(tree)


def test_dir_hash_md5_skips_runtime_cache_only(tmp_path):
    tree = _golden_tree(tmp_path / "tree")
    before = dir_hash_md5(tree)
    (tree / "mod.pyc").write_bytes(b"different stale bytecode")
    (tree / "z" / "__pycache__" / "c.pyc").write_bytes(b"different cache")
    (tree / "z" / "__pycache__" / "new.txt").write_text("cache noise\n", encoding="utf-8")
    assert dir_hash_md5(tree) == before
    (tree / "keep.pyc").write_bytes(b"changed shipped bytecode")
    assert dir_hash_md5(tree) != before


def test_hub_content_hash_matches_reference(tmp_path):
    tree = _golden_tree(tmp_path / "tree")
    value = hub_content_hash(tree)
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", value)
    assert value == "sha256:" + _ref_content_digest(tree)[:16]


def test_hub_lock_uses_hub_content_hash(tmp_path):
    skill = {"SKILL.md": "---\nname: h\ndescription: d\n---\nbody\n", "a-b/x": "1\n", "a/b": "2\n"}
    root = make_fleet(tmp_path, {"default": {"tools/h": skill}, "_hub": {"default": {"h": "tools/h"}}})
    skills = root / "skills"
    lock = json.loads((skills / ".hub" / "lock.json").read_text(encoding="utf-8"))
    expected = "sha256:" + _ref_content_digest(skills / "tools" / "h")[:16]
    assert lock["installed"]["h"]["content_hash"] == expected


def test_bundled_manifest_matches_reference(tmp_path):
    origin = {"SKILL.md": "---\nname: b\ndescription: d\n---\nbody\n", "a-b/x": "1\n", "a/b": "2\n"}
    root = make_fleet(
        tmp_path, {"default": {"bundled/b": dict(origin)}, "_bundled": {"default": {"b": origin}}}
    )
    manifest = (root / "skills" / ".bundled_manifest").read_text(encoding="utf-8")
    assert manifest == f"b:{_ref_dir_hash(root / 'skills' / 'bundled' / 'b')}\n"


def test_bad_string_entry_raises(tmp_path):
    with pytest.raises(ValueError, match="symlink:"):
        make_fleet(tmp_path, {"default": {"general/bad": "not-a-symlink-spec"}})


def test_matches_real_hermes(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path.home() / ".hermes" / "hermes-agent"))
    skills_sync = pytest.importorskip("tools.skills_sync")
    skills_guard = pytest.importorskip("tools.skills_guard")
    tree = _golden_tree(tmp_path / "tree")
    assert dir_hash_md5(tree) == skills_sync.dir_hash(tree)
    assert hub_content_hash(tree) == skills_guard.content_hash(tree)
