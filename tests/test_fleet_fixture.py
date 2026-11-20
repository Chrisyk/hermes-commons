"""Checks that the synthetic classic fleet has the layout later tasks rely on."""

from tests.fleet import dir_hash_md5


def _manifest(skills_dir):
    entries = {}
    for line in (skills_dir / ".bundled_manifest").read_text().splitlines():
        name, _, digest = line.partition(":")
        entries[name] = digest
    return entries


def test_classic_fleet_has_four_configs(classic_fleet):
    configs = sorted(p.relative_to(classic_fleet).as_posix() for p in classic_fleet.rglob("config.yaml"))
    assert configs == [
        "config.yaml",
        "profiles/device/config.yaml",
        "profiles/pikvm/config.yaml",
        "profiles/pocket/config.yaml",
    ]


def test_device_has_duplicate_skill(classic_fleet):
    device = classic_fleet / "profiles" / "device" / "skills"
    assert (device / "a" / "dup" / "SKILL.md").is_file()
    assert (device / "b" / "dup" / "SKILL.md").is_file()
    assert (device / "a" / "dup" / "SKILL.md").read_text() != (device / "b" / "dup" / "SKILL.md").read_text()


def test_pristine_bundled_matches_manifest(classic_fleet):
    skills = classic_fleet / "skills"
    manifest = _manifest(skills)
    assert manifest["bundled-pristine"] == dir_hash_md5(skills / "bundled" / "bundled-pristine")


def test_edited_bundled_differs_from_manifest(classic_fleet):
    skills = classic_fleet / "profiles" / "pikvm" / "skills"
    manifest = _manifest(skills)
    assert manifest["bundled-edited"] != dir_hash_md5(skills / "bundled" / "bundled-edited")


def test_default_bundled_edited_matches_origin(classic_fleet):
    skills = classic_fleet / "skills"
    assert _manifest(skills)["bundled-edited"] == dir_hash_md5(skills / "bundled" / "bundled-edited")


def test_hub_lock_records_hubskill(classic_fleet):
    import json

    lock = json.loads((classic_fleet / "skills" / ".hub" / "lock.json").read_text())
    entry = lock["installed"]["hubskill"]
    assert entry["install_path"] == "tools/hubskill"
    assert entry["content_hash"].startswith("sha256:")
    assert (classic_fleet / "skills" / "tools" / "hubskill" / "SKILL.md").is_file()


def test_linked_is_symlink(classic_fleet):
    link = classic_fleet / "skills" / "general" / "linked"
    assert link.is_symlink()
    assert (link / "SKILL.md").is_file()


def test_config_starts_empty(classic_fleet):
    import yaml

    cfg = yaml.safe_load((classic_fleet / "profiles" / "pocket" / "config.yaml").read_text())
    assert cfg == {"skills": {}, "plugins": {"enabled": []}}
