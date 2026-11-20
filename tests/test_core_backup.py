"""Tests for commons.core.backup: profile snapshots under <shared_root>/.commons/backups."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commons.core.backup import create_backup, list_backups, load_manifest, tree_sha256, verify_backup
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
