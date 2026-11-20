"""Tests for commons.core.moves: retire a skill by moving it, never deleting it."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from commons.core import moves
from commons.core.backup import tree_sha256
from commons.core.moves import retire


def _skill(path: Path, body: str = "body\n") -> Path:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {path.name}\n---\n{body}", encoding="utf-8")
    (path / "scripts").mkdir()
    run = path / "scripts" / "run.sh"
    run.write_bytes(b"#!/bin/sh\necho \xe2\x9c\x93\n")
    run.chmod(0o755)
    (path / "scripts" / "rel-link").symlink_to("run.sh")
    return path


def test_retire_moves_skill_folder(tmp_path):
    src = _skill(tmp_path / "skills" / "general" / "one")
    before = tree_sha256(src)
    backup = tmp_path / "backup"
    dest = retire(src, backup, "device", "general/one")
    assert dest == backup / "retired" / "device" / "general" / "one"
    assert not src.exists() and not src.is_symlink()
    assert tree_sha256(dest) == before
    assert (dest / "scripts" / "rel-link").is_symlink()
    assert os.access(dest / "scripts" / "run.sh", os.X_OK)


def test_retire_twice_to_same_target_raises(tmp_path):
    backup = tmp_path / "backup"
    retire(_skill(tmp_path / "a" / "one", "first\n"), backup, "device", "general/one")
    second = _skill(tmp_path / "b" / "one", "second\n")
    before = tree_sha256(second)
    with pytest.raises(FileExistsError):
        retire(second, backup, "device", "general/one")
    assert tree_sha256(second) == before
    kept = backup / "retired" / "device" / "general" / "one" / "SKILL.md"
    assert kept.read_text(encoding="utf-8").endswith("first\n")


def test_dangling_symlink_at_target_counts_as_existing(tmp_path):
    backup = tmp_path / "backup"
    target = backup / "retired" / "p" / "one"
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "missing")
    src = _skill(tmp_path / "one")
    with pytest.raises(FileExistsError):
        retire(src, backup, "p", "one")
    assert src.is_dir()


def test_symlinked_skill_moves_as_link(tmp_path):
    real = _skill(tmp_path / "library" / "linked")
    before = tree_sha256(real)
    link = tmp_path / "skills" / "general" / "linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)
    dest = retire(link, tmp_path / "backup", "default", "general/linked")
    assert dest.is_symlink()
    assert os.readlink(dest) == str(real)
    assert not link.exists() and not link.is_symlink()
    assert real.is_dir() and tree_sha256(real) == before


def test_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        retire(tmp_path / "nope", tmp_path / "backup", "p", "nope")


@pytest.mark.parametrize(
    "profile,rel", [("p", "../escape"), ("p", "/abs"), ("../x", "one"), ("p", ""), ("", "a")]
)
def test_rel_and_profile_must_stay_inside(tmp_path, profile, rel):
    src = _skill(tmp_path / "one")
    with pytest.raises(ValueError):
        retire(src, tmp_path / "backup", profile, rel)
    assert src.is_dir()


def test_cross_filesystem_copy_path(tmp_path, monkeypatch):
    src = _skill(tmp_path / "skills" / "one")
    before = tree_sha256(src)
    real_rename = os.rename

    def exdev(a, b):
        if Path(a) == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(a, b)

    monkeypatch.setattr(moves.os, "rename", exdev)
    dest = retire(src, tmp_path / "backup", "p", "one")
    assert not src.exists()
    assert tree_sha256(dest) == before
    assert (dest / "scripts" / "rel-link").is_symlink()
    assert [p.name for p in dest.parent.iterdir()] == ["one"]


def test_cross_filesystem_symlink(tmp_path, monkeypatch):
    real = _skill(tmp_path / "library" / "linked")
    link = tmp_path / "skills" / "linked"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)
    real_rename = os.rename

    def exdev(a, b):
        if Path(a) == link:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(a, b)

    monkeypatch.setattr(moves.os, "rename", exdev)
    dest = retire(link, tmp_path / "backup", "p", "linked")
    assert dest.is_symlink() and os.readlink(dest) == str(real)
    assert not link.is_symlink()
    assert (real / "SKILL.md").is_file()


def test_cross_filesystem_verify_failure_keeps_source(tmp_path, monkeypatch):
    src = _skill(tmp_path / "skills" / "one")
    before = tree_sha256(src)

    def exdev(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    real_copytree = moves.shutil.copytree

    def bad_copytree(a, b, *args, **kwargs):
        # shutil recurses through the module attribute, so corrupt only the top call.
        real_copytree(a, b, *args, **kwargs)
        if Path(a) == src:
            (Path(b) / "SKILL.md").write_text("corrupt\n", encoding="utf-8")

    monkeypatch.setattr(moves.os, "rename", exdev)
    monkeypatch.setattr(moves.shutil, "copytree", bad_copytree)
    with pytest.raises(OSError):
        retire(src, tmp_path / "backup", "p", "one")
    assert tree_sha256(src) == before
    assert not (tmp_path / "backup" / "retired" / "p" / "one").exists()
