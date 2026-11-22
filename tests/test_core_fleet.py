import sys

import pytest

"""Tests for commons.core.fleet discovery."""

from pathlib import Path

import yaml

from commons.core.fleet import Profile, discover


def _write_config(home: Path, config) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_classic_fleet_order(classic_fleet):
    profiles = discover(classic_fleet)
    assert [p.name for p in profiles] == ["default", "device", "pikvm", "pocket"]
    assert profiles[0].home == classic_fleet
    assert profiles[1].home == classic_fleet / "profiles" / "device"
    assert profiles[1].skills_dir == classic_fleet / "profiles" / "device" / "skills"


def test_profile_without_config_is_skipped(classic_fleet):
    (classic_fleet / "profiles" / "noconfig" / "skills").mkdir(parents=True)
    names = [p.name for p in discover(classic_fleet)]
    assert "noconfig" not in names


def test_root_without_config_has_no_default(tmp_path):
    _write_config(tmp_path / "profiles" / "alpha", {})
    assert [p.name for p in discover(tmp_path)] == ["alpha"]


def test_is_deleted_skips_profile(classic_fleet):
    pikvm = classic_fleet / "profiles" / "pikvm"
    names = [p.name for p in discover(classic_fleet, is_deleted=lambda home: home == pikvm)]
    assert names == ["default", "device", "pocket"]


@pytest.mark.skipif(sys.platform == "win32", reason="expanduser reads USERPROFILE on Windows, not HOME")
def test_create_dir_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    p = Profile("x", tmp_path, {"skills": {"create_dir": "~/shared/skills"}})
    assert p.create_dir() == tmp_path / "home" / "shared" / "skills"


def test_create_dir_none_when_unset_or_empty(tmp_path):
    assert Profile("x", tmp_path, {}).create_dir() is None
    assert Profile("x", tmp_path, {"skills": {"create_dir": ""}}).create_dir() is None
    assert Profile("x", tmp_path, {"skills": None}).create_dir() is None
    assert Profile("x", tmp_path, {"skills": "oops"}).create_dir() is None


@pytest.mark.skipif(sys.platform == "win32", reason="expanduser reads USERPROFILE on Windows, not HOME")
def test_external_dirs_accepts_str_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    one = Profile("x", tmp_path, {"skills": {"external_dirs": "~/a"}})
    assert one.external_dirs() == [tmp_path / "home" / "a"]
    many = Profile("x", tmp_path, {"skills": {"external_dirs": ["~/a", "/abs/b"]}})
    assert many.external_dirs() == [tmp_path / "home" / "a", Path("/abs/b")]
    assert Profile("x", tmp_path, {}).external_dirs() == []
    assert Profile("x", tmp_path, {"skills": {"external_dirs": None}}).external_dirs() == []


def test_plugins_enabled(tmp_path):
    assert Profile("x", tmp_path, {"plugins": {"enabled": ["commons", "other"]}}).plugins_enabled() == [
        "commons",
        "other",
    ]
    assert Profile("x", tmp_path, {}).plugins_enabled() == []
    assert Profile("x", tmp_path, {"plugins": None}).plugins_enabled() == []
    assert Profile("x", tmp_path, {"plugins": {"enabled": None}}).plugins_enabled() == []


def test_malformed_config_yields_empty_dict(classic_fleet):
    (classic_fleet / "profiles" / "device" / "config.yaml").write_text("key: [unclosed\n", encoding="utf-8")
    (classic_fleet / "profiles" / "pikvm" / "config.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    by_name = {p.name: p for p in discover(classic_fleet)}
    assert by_name["device"].config == {}
    assert by_name["pikvm"].config == {}
    assert by_name["device"].create_dir() is None


def test_named_profile_called_default_is_skipped(classic_fleet):
    _write_config(classic_fleet / "profiles" / "default", {})
    profiles = discover(classic_fleet)
    assert [p.name for p in profiles].count("default") == 1
    assert profiles[0].home == classic_fleet
