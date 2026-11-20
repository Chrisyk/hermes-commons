"""Tests for commons.core.backup: profile snapshots under <shared_root>/.commons/backups."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commons.core import backup
from commons.core.backup import (
    BackupError,
    create_backup,
    list_backups,
    load_manifest,
    tree_sha256,
    verify_backup,
)
from commons.core.fleet import Profile, discover

NOW = datetime(2026, 11, 20, 9, 30, 5, tzinfo=UTC)


def _backups(shared: Path) -> Path:
    return shared / ".commons" / "backups"


def test_classic_fleet_round_trips(classic_fleet, tmp_path):
    shared = tmp_path / "shared"
    profiles = discover(classic_fleet)
    bid = create_backup(shared, profiles, "setup", now=NOW)
    assert bid == "20261120T093005Z-setup"
    manifest = load_manifest(shared, bid)
    assert manifest["id"] == bid
    assert manifest["label"] == "setup"
    assert manifest["created"].startswith("2026-11-20T09:30:05")
    assert sorted(manifest["profiles"]) == ["default", "device", "pikvm", "pocket"]
    for p in profiles:
        copy = _backups(shared) / bid / p.name
        entry = manifest["profiles"][p.name]
        assert entry["home"] == str(p.home)
        assert tree_sha256(copy / "skills") == tree_sha256(p.skills_dir) == entry["skills_tree_sha256"]
        src_config = (p.home / "config.yaml").read_bytes()
        assert (copy / "config.yaml").read_bytes() == src_config
        assert entry["config_sha256"] == hashlib.sha256(src_config).hexdigest()
        assert entry["skills_files"] == sum(
            1 for q in p.skills_dir.rglob("*") if q.is_symlink() or not q.is_dir()
        )
    link = _backups(shared) / bid / "default" / "skills" / "general" / "linked"
    assert link.is_symlink()
    assert os.readlink(link) == os.readlink(classic_fleet / "skills" / "general" / "linked")
    assert verify_backup(shared, bid) == []


def test_symlink_target_contents_not_copied(classic_fleet, tmp_path):
    shared = tmp_path / "shared"
    bid = create_backup(shared, discover(classic_fleet), "setup", now=NOW)
    copied = [p for p in (_backups(shared) / bid).rglob("SKILL.md") if "linked" in p.parts]
    assert all(any(parent.is_symlink() for parent in p.parents) for p in copied)


def test_tree_hash_sensitive_to_content_link_and_exec_bit(tmp_path):
    tree = tmp_path / "t"
    (tree / "a").mkdir(parents=True)
    f = tree / "a" / "run.sh"
    f.write_text("echo hi\n", encoding="utf-8")
    base = tree_sha256(tree)
    assert tree_sha256(tree) == base
    f.chmod(0o755)
    exec_hash = tree_sha256(tree)
    assert exec_hash != base
    f.write_text("echo bye\n", encoding="utf-8")
    assert tree_sha256(tree) != exec_hash
    (tree / "l").symlink_to("a")
    with_link = tree_sha256(tree)
    (tree / "l").unlink()
    (tree / "l").symlink_to("b")
    assert tree_sha256(tree) != with_link
    (tree / "empty").mkdir()
    assert tree_sha256(tree) != with_link


def test_verify_reports_tampering(classic_fleet, tmp_path):
    shared = tmp_path / "shared"
    bid = create_backup(shared, discover(classic_fleet), "setup", now=NOW)
    assert verify_backup(shared, bid) == []
    target = _backups(shared) / bid / "device" / "skills" / "general" / "same" / "notes.txt"
    target.write_text("tampered\n", encoding="utf-8")
    (_backups(shared) / bid / "pikvm" / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    problems = verify_backup(shared, bid)
    assert any("device" in p and "skills" in p for p in problems)
    assert any("pikvm" in p and "config" in p for p in problems)
    assert len(problems) == 2


def test_verify_reports_missing_parts(classic_fleet, tmp_path):
    shared = tmp_path / "shared"
    bid = create_backup(shared, discover(classic_fleet), "setup", now=NOW)
    (_backups(shared) / bid / "pocket" / "config.yaml").unlink()
    problems = verify_backup(shared, bid)
    assert problems and "pocket" in problems[0]
    assert verify_backup(shared, "no-such-backup")


def test_same_second_ids_get_suffix(classic_fleet, tmp_path):
    shared = tmp_path / "shared"
    profiles = discover(classic_fleet)
    ids = [create_backup(shared, profiles, "setup", now=NOW) for _ in range(3)]
    assert ids == ["20261120T093005Z-setup", "20261120T093005Z-setup-2", "20261120T093005Z-setup-3"]
    later = create_backup(shared, profiles, "check", now=datetime(2026, 11, 21, tzinfo=UTC))
    assert list_backups(shared) == sorted(ids + [later])
    for bid in ids:
        assert load_manifest(shared, bid)["id"] == bid


def test_naive_now_is_treated_as_utc(classic_fleet, tmp_path):
    bid = create_backup(tmp_path / "s", discover(classic_fleet), "x", now=datetime(2026, 1, 2, 3, 4, 5))
    assert bid == "20260102T030405Z-x"


def test_missing_config_and_skills_handled(tmp_path):
    home = tmp_path / "bare"
    (home / "skills" / "one").mkdir(parents=True)
    (home / "skills" / "one" / "SKILL.md").write_text("---\nname: one\n---\n", encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()
    shared = tmp_path / "shared"
    bid = create_backup(shared, [Profile("bare", home), Profile("empty", empty)], "manual", now=NOW)
    manifest = load_manifest(shared, bid)
    assert manifest["profiles"]["bare"]["config_sha256"] is None
    assert manifest["profiles"]["bare"]["skills_files"] == 1
    assert manifest["profiles"]["empty"]["skills_tree_sha256"] is None
    assert manifest["profiles"]["empty"]["skills_files"] == 0
    assert not (_backups(shared) / bid / "bare" / "config.yaml").exists()
    assert verify_backup(shared, bid) == []
    json.dumps(manifest)


def test_list_backups_empty(tmp_path):
    assert list_backups(tmp_path / "nothing") == []


@pytest.mark.parametrize("label", ["", "a/b", "..", "has space"])
def test_bad_label_rejected(tmp_path, label):
    with pytest.raises(ValueError):
        create_backup(tmp_path / "s", [], label, now=NOW)


def test_backup_inside_a_skills_dir_does_not_recurse(tmp_path):
    home = tmp_path / "home"
    (home / "skills" / "one").mkdir(parents=True)
    (home / "skills" / "one" / "SKILL.md").write_text("x\n", encoding="utf-8")
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    shared = home / "skills"
    bid = create_backup(shared, [Profile("default", home)], "nested", now=NOW)
    copy = _backups(shared) / bid / "default" / "skills"
    assert (copy / "one" / "SKILL.md").is_file()
    assert not (copy / ".commons").exists()
    assert verify_backup(shared, bid) == []


def _one_profile(tmp_path: Path) -> Profile:
    home = tmp_path / "home"
    (home / "skills" / "one").mkdir(parents=True)
    (home / "skills" / "one" / "SKILL.md").write_text("---\nname: one\n---\n", encoding="utf-8")
    (home / "skills" / "one" / "notes.txt").write_text("notes\n", encoding="utf-8")
    (home / "skills" / "two").mkdir()
    (home / "skills" / "two" / "SKILL.md").write_text("---\nname: two\n---\n", encoding="utf-8")
    (home / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    return Profile("default", home)


def test_plain_profile_records_no_symlinks(tmp_path):
    shared = tmp_path / "shared"
    bid = create_backup(shared, [_one_profile(tmp_path)], "plain", now=NOW)
    entry = load_manifest(shared, bid)["profiles"]["default"]
    assert entry["config_is_symlink"] is False and entry["config_link_target"] is None
    assert entry["skills_is_symlink"] is False and entry["skills_link_target"] is None


def test_symlinked_config_is_recorded_and_copied(tmp_path):
    profile = _one_profile(tmp_path)
    real = tmp_path / "dotfiles" / "config.yaml"
    real.parent.mkdir()
    real.write_text("from: dotfiles\n", encoding="utf-8")
    (profile.home / "config.yaml").unlink()
    (profile.home / "config.yaml").symlink_to(real)
    shared = tmp_path / "shared"
    bid = create_backup(shared, [profile], "linked", now=NOW)
    entry = load_manifest(shared, bid)["profiles"]["default"]
    assert entry["config_is_symlink"] is True
    assert entry["config_link_target"] == str(real)
    copy = _backups(shared) / bid / "default" / "config.yaml"
    assert not copy.is_symlink()
    assert copy.read_text(encoding="utf-8") == "from: dotfiles\n"
    assert entry["skills_is_symlink"] is False
    assert verify_backup(shared, bid) == []


def test_symlinked_skills_dir_is_recorded_and_copied(tmp_path):
    profile = _one_profile(tmp_path)
    real = tmp_path / "library" / "skills"
    real.parent.mkdir()
    (profile.home / "skills").rename(real)
    (profile.home / "skills").symlink_to("../library/skills", target_is_directory=True)
    shared = tmp_path / "shared"
    bid = create_backup(shared, [profile], "linked", now=NOW)
    entry = load_manifest(shared, bid)["profiles"]["default"]
    assert entry["skills_is_symlink"] is True
    assert entry["skills_link_target"] == "../library/skills"
    copy = _backups(shared) / bid / "default" / "skills"
    assert copy.is_dir() and not copy.is_symlink()
    assert (copy / "one" / "SKILL.md").is_file()
    assert entry["skills_tree_sha256"] == tree_sha256(real)
    assert entry["config_is_symlink"] is False
    assert verify_backup(shared, bid) == []


def test_skills_changed_mid_copy_fails_without_manifest(tmp_path, monkeypatch):
    profile = _one_profile(tmp_path)
    victim = profile.skills_dir / "two" / "SKILL.md"
    real_copy = backup._copy_file
    calls = []

    def copy_and_edit(src, dst, *args, **kwargs):
        calls.append(src)
        if len(calls) == 1:
            # A Hermes edit lands while the backup is halfway through.
            victim.write_text("---\nname: two\n---\nedited mid-copy\n", encoding="utf-8")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup, "_copy_file", copy_and_edit)
    shared = tmp_path / "shared"
    with pytest.raises(BackupError, match="changed while copying"):
        create_backup(shared, [profile], "racy", now=NOW)
    assert calls
    assert list_backups(shared) == []
    assert list((_backups(shared)).rglob("manifest.json")) == []
    assert list((_backups(shared)).rglob(".manifest.json.tmp")) == []


def test_skill_deleted_mid_copy_is_a_backup_error(tmp_path, monkeypatch):
    profile = _one_profile(tmp_path)
    real_copy = backup._copy_file

    def copy_then_delete(src, dst, *args, **kwargs):
        (profile.skills_dir / "two" / "SKILL.md").unlink(missing_ok=True)
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup, "_copy_file", copy_then_delete)
    shared = tmp_path / "shared"
    with pytest.raises(BackupError, match="changed while copying"):
        create_backup(shared, [profile], "racy", now=NOW)
    assert list_backups(shared) == []


def test_data_is_fsynced_before_manifest_exists(tmp_path, monkeypatch):
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /proc to map fds to paths")
    profile = _one_profile(tmp_path)
    (profile.skills_dir / "one" / "link").symlink_to("notes.txt")
    synced: list[str] = []
    events: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        path = os.readlink(f"/proc/self/fd/{fd}")
        synced.append(path)
        events.append(("fsync", path))
        return real_fsync(fd)

    def spy_replace(a, b, *args, **kwargs):
        events.append(("replace", str(b)))
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(backup.os, "fsync", spy_fsync)
    monkeypatch.setattr(backup.os, "replace", spy_replace)
    shared = tmp_path / "shared"
    bid = create_backup(shared, [profile], "durable", now=NOW)
    base = _backups(shared) / bid
    manifest = base / "manifest.json"
    replace_at = events.index(("replace", str(manifest)))
    before = {path for kind, path in events[:replace_at] if kind == "fsync"}
    data = [base / "default" / "config.yaml"] + [
        q for q in (base / "default").rglob("*") if not q.is_symlink()
    ]
    dirs = [base, base / "default", _backups(shared), shared / ".commons", shared]
    for q in data + dirs:
        assert str(q) in before, f"{q} not fsynced before the manifest"
    assert str(base / ".manifest.json.tmp") in before
    assert ("fsync", str(base)) in events[replace_at + 1 :]


def test_hashing_reads_in_chunks(tmp_path, monkeypatch):
    tree = tmp_path / "t"
    tree.mkdir()
    big = os.urandom(3 * 1024 * 1024 + 17)
    (tree / "big.bin").write_bytes(big)
    expected = hashlib.sha256()
    expected.update(b"big.bin\x00f\x00-\x00" + str(len(big)).encode() + b"\x00" + big)
    sizes: list[int] = []

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def read(self, n=-1):
            sizes.append(n)
            return self.fh.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

    real_open = open
    monkeypatch.setattr(backup, "open", lambda path, mode="r": Spy(real_open(path, mode)), raising=False)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("whole-file read"))
    assert tree_sha256(tree) == expected.hexdigest()
    assert sizes and all(0 < n <= 1024 * 1024 for n in sizes)
    assert backup._file_sha256(tree / "big.bin") == hashlib.sha256(big).hexdigest()
