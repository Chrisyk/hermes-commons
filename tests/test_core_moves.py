"""Tests for commons.core.moves: retire a skill by moving it, never deleting it."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from commons.core import moves
from commons.core.backup import tree_sha256
from commons.core.moves import RetireError, move_noreplace, retire


def _skill(path: Path, body: str = "body\n") -> Path:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {path.name}\n---\n{body}", encoding="utf-8")
    (path / "scripts").mkdir()
    run = path / "scripts" / "run.sh"
    run.write_bytes(b"#!/bin/sh\necho \xe2\x9c\x93\n")
    run.chmod(0o755)
    (path / "scripts" / "rel-link").symlink_to("run.sh")
    return path


def _cross_device(monkeypatch, src: Path, events: list | None = None) -> None:
    """Make the first rename of src fail with EXDEV, as a move to another filesystem would."""
    real = moves._rename_noreplace

    def rename(a, b):
        if Path(a) == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if events is not None:
            events.append(("rename", str(a), str(b)))
        return real(a, b)

    monkeypatch.setattr(moves, "_rename_noreplace", rename)


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
    _cross_device(monkeypatch, src)
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
    _cross_device(monkeypatch, link)
    dest = retire(link, tmp_path / "backup", "p", "linked")
    assert dest.is_symlink() and os.readlink(dest) == str(real)
    assert not link.is_symlink()
    assert (real / "SKILL.md").is_file()


def test_cross_filesystem_verify_failure_keeps_source(tmp_path, monkeypatch):
    src = _skill(tmp_path / "skills" / "one")
    before = tree_sha256(src)

    real_copytree = moves.shutil.copytree

    def bad_copytree(a, b, *args, **kwargs):
        # shutil recurses through the module attribute, so corrupt only the top call.
        real_copytree(a, b, *args, **kwargs)
        if Path(a) == src:
            (Path(b) / "SKILL.md").write_text("corrupt\n", encoding="utf-8")

    _cross_device(monkeypatch, src)
    monkeypatch.setattr(moves.shutil, "copytree", bad_copytree)
    with pytest.raises(OSError):
        retire(src, tmp_path / "backup", "p", "one")
    assert tree_sha256(src) == before
    assert not (tmp_path / "backup" / "retired" / "p" / "one").exists()


def test_empty_dir_at_target_is_not_replaced(tmp_path):
    target = tmp_path / "backup" / "retired" / "p" / "one"
    target.mkdir(parents=True)
    src = _skill(tmp_path / "one")
    before = tree_sha256(src)
    with pytest.raises(FileExistsError):
        retire(src, tmp_path / "backup", "p", "one")
    assert tree_sha256(src) == before
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("native", [True, False])
def test_rename_noreplace_refuses_an_empty_dir(tmp_path, monkeypatch, native):
    # retire() checks the target first; this is the guard for a target that
    # appears after that check (another process or thread racing for it).
    if native and moves._renameat2 is None:
        pytest.skip("libc has no renameat2")
    if not native:
        monkeypatch.setattr(moves, "_renameat2", None)
    src = _skill(tmp_path / "one")
    target = tmp_path / "taken"
    target.mkdir()
    with pytest.raises(FileExistsError):
        moves._rename_noreplace(src, target)
    assert (src / "SKILL.md").is_file()
    assert list(target.iterdir()) == []
    moves._rename_noreplace(src, tmp_path / "free")
    assert (tmp_path / "free" / "SKILL.md").is_file() and not src.exists()


def test_rename_noreplace_falls_back_when_flag_unsupported(tmp_path, monkeypatch):
    def unsupported(a, b):
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(moves, "_renameat2", unsupported)
    src = _skill(tmp_path / "one")
    moves._rename_noreplace(src, tmp_path / "moved")
    assert (tmp_path / "moved" / "SKILL.md").is_file()


def test_retire_plain_file(tmp_path):
    src = tmp_path / "skills" / "loose.md"
    src.parent.mkdir()
    src.write_text("loose\n", encoding="utf-8")
    dest = retire(src, tmp_path / "backup", "p", "loose.md")
    assert dest.read_text(encoding="utf-8") == "loose\n"
    assert not src.exists()


def test_cross_filesystem_plain_file(tmp_path, monkeypatch):
    src = tmp_path / "skills" / "run.sh"
    src.parent.mkdir()
    src.write_bytes(b"#!/bin/sh\necho hi\n")
    src.chmod(0o750)
    _cross_device(monkeypatch, src)
    dest = retire(src, tmp_path / "backup", "p", "run.sh")
    assert dest.read_bytes() == b"#!/bin/sh\necho hi\n"
    assert (dest.stat().st_mode & 0o777) == 0o750
    assert not src.exists()
    assert [p.name for p in dest.parent.iterdir()] == ["run.sh"]


def test_cross_filesystem_fsyncs_copy_before_rename_and_removal(tmp_path, monkeypatch):
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /proc to map fds to paths")
    src = _skill(tmp_path / "skills" / "one")
    backup = tmp_path / "backup"
    events: list[tuple] = []
    real_fsync, real_rmtree = os.fsync, moves.shutil.rmtree

    def spy_fsync(fd):
        events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        return real_fsync(fd)

    def spy_rmtree(path, *args, **kwargs):
        events.append(("remove", str(path)))
        return real_rmtree(path, *args, **kwargs)

    _cross_device(monkeypatch, src, events)
    monkeypatch.setattr(moves.os, "fsync", spy_fsync)
    monkeypatch.setattr(moves.shutil, "rmtree", spy_rmtree)
    dest = retire(src, backup, "p", "one")

    [rename_at] = [i for i, e in enumerate(events) if e[0] == "rename"]
    staging = Path(events[rename_at][1])
    assert events[rename_at][2] == str(dest)
    synced_before = {e[1] for e in events[:rename_at] if e[0] == "fsync"}
    for q in dest.rglob("*"):
        if not q.is_symlink():
            assert str(staging / q.relative_to(dest)) in synced_before, q
    assert str(staging) in synced_before
    assert str(dest.parent) in synced_before
    for d in (backup, backup / "retired", backup / "retired" / "p"):
        assert str(d) in synced_before
    remove_at = events.index(("remove", str(src)))
    assert rename_at < remove_at
    assert ("fsync", str(dest.parent)) in events[rename_at + 1 : remove_at]


def test_partial_source_removal_names_path_and_safe_copy(tmp_path, monkeypatch):
    src = _skill(tmp_path / "skills" / "one")
    before = tree_sha256(src)
    stuck = src / "scripts" / "run.sh"

    def failing_rmtree(path, *args, **kwargs):
        (Path(path) / "SKILL.md").unlink()
        raise PermissionError(errno.EACCES, "Permission denied", str(stuck))

    _cross_device(monkeypatch, src)
    monkeypatch.setattr(moves.shutil, "rmtree", failing_rmtree)
    with pytest.raises(RetireError) as info:
        retire(src, tmp_path / "backup", "p", "one")
    dest = tmp_path / "backup" / "retired" / "p" / "one"
    message = str(info.value)
    assert str(stuck) in message
    assert f"full copy is safe at {dest}" in message
    assert isinstance(info.value, OSError)
    assert tree_sha256(dest) == before


def test_move_noreplace(tmp_path):
    src = _skill(tmp_path / "a" / "s")
    before = tree_sha256(src)
    dest = move_noreplace(src, tmp_path / "b" / "deep" / "s")
    assert tree_sha256(dest) == before and not src.exists()
    other = _skill(tmp_path / "c" / "s")
    with pytest.raises(FileExistsError):
        move_noreplace(other, dest)
    assert other.exists()
    with pytest.raises(FileNotFoundError):
        move_noreplace(tmp_path / "missing", tmp_path / "x")


def test_move_noreplace_across_file_systems(tmp_path, monkeypatch):
    src = _skill(tmp_path / "a" / "s")
    before = tree_sha256(src)
    _cross_device(monkeypatch, src)
    dest = move_noreplace(src, tmp_path / "b" / "s")
    assert tree_sha256(dest) == before and not src.exists()


def test_copy_noreplace_cleans_staging_on_interrupt(tmp_path, monkeypatch):
    src = _skill(tmp_path / "src" / "s")
    dest = tmp_path / "dest" / "s"

    def interrupted(*_args, **_kwargs):
        moves.copy_staging(dest).mkdir(parents=True)
        raise KeyboardInterrupt

    monkeypatch.setattr(moves, "copy_tree", interrupted)
    with pytest.raises(KeyboardInterrupt):
        moves.copy_noreplace(src, dest)
    assert not moves.copy_staging(dest).exists()
    assert not dest.exists()
    assert (src / "SKILL.md").is_file()
