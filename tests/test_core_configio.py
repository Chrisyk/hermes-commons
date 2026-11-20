"""Tests for commons.core.configio: per-profile config.yaml reads and writes."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from commons.core import configio
from commons.core.configio import ConfigWriter, YamlFileWriter, hermes_config_set_argv, yaml_dump_flow
from commons.core.fleet import Profile
from tests.test_hash_fidelity import _hermes_python, _hermes_src


def _profile(tmp_path: Path, config: dict | None = None, name: str = "device") -> Profile:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return Profile(name=name, home=home)


def _read(profile: Profile) -> dict:
    return yaml.safe_load((profile.home / "config.yaml").read_text(encoding="utf-8"))


def test_yaml_writer_satisfies_protocol():
    assert isinstance(YamlFileWriter(), ConfigWriter)


def test_set_nested_key_and_get_it_back(tmp_path):
    p = _profile(tmp_path, {"model": {"name": "x"}, "plugins": {"enabled": ["a"]}, "top": 1})
    w = YamlFileWriter()
    w.set(p, "skills.create_dir", "~/shared")
    assert w.get(p, "skills.create_dir") == "~/shared"
    data = _read(p)
    assert data["skills"] == {"create_dir": "~/shared"}
    assert data["model"] == {"name": "x"}
    assert data["plugins"] == {"enabled": ["a"]}
    assert data["top"] == 1
    assert list(data) == ["model", "plugins", "top", "skills"]


def test_get_missing_returns_none(tmp_path):
    p = _profile(tmp_path, {"a": {"b": 1}, "s": "text"})
    w = YamlFileWriter()
    assert w.get(p, "a.b") == 1
    assert w.get(p, "a.c") is None
    assert w.get(p, "s.deeper") is None
    assert w.get(p, "nope.at.all") is None


def test_missing_config_starts_empty(tmp_path):
    p = _profile(tmp_path)
    w = YamlFileWriter()
    assert w.get(p, "skills.create_dir") is None
    w.set(p, "skills.create_dir", "/x")
    assert _read(p) == {"skills": {"create_dir": "/x"}}


def test_set_through_non_dict_raises(tmp_path):
    p = _profile(tmp_path, {"skills": "oops"})
    with pytest.raises(ValueError):
        YamlFileWriter().set(p, "skills.create_dir", "/x")
    assert _read(p) == {"skills": "oops"}


@pytest.mark.parametrize("key", ["", ".a", "a.", "a..b"])
def test_bad_keys_rejected(tmp_path, key):
    with pytest.raises(ValueError):
        YamlFileWriter().set(_profile(tmp_path, {}), key, 1)


def test_add_to_list_is_idempotent(tmp_path):
    p = _profile(tmp_path, {"plugins": {"enabled": ["a"]}})
    w = YamlFileWriter()
    w.add_to_list(p, "plugins.enabled", "commons")
    w.add_to_list(p, "plugins.enabled", "commons")
    w.add_to_list(p, "skills.external_dirs", "/lib")
    assert _read(p)["plugins"]["enabled"] == ["a", "commons"]
    assert _read(p)["skills"]["external_dirs"] == ["/lib"]


def test_add_to_list_rejects_non_list(tmp_path):
    p = _profile(tmp_path, {"plugins": {"enabled": "a"}})
    with pytest.raises(ValueError):
        YamlFileWriter().add_to_list(p, "plugins.enabled", "commons")


def test_remove_from_list(tmp_path):
    p = _profile(tmp_path, {"plugins": {"enabled": ["a", "commons", "b", "commons"]}})
    w = YamlFileWriter()
    w.remove_from_list(p, "plugins.enabled", "commons")
    assert _read(p)["plugins"]["enabled"] == ["a", "b"]
    before = (p.home / "config.yaml").read_bytes()
    w.remove_from_list(p, "plugins.enabled", "commons")
    w.remove_from_list(p, "plugins.missing", "commons")
    assert (p.home / "config.yaml").read_bytes() == before


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    p = _profile(tmp_path, {"a": 1})
    w = YamlFileWriter()
    for i in range(5):
        w.set(p, f"k{i}.v", i)
    w.add_to_list(p, "l", 1)
    assert sorted(x.name for x in p.home.iterdir()) == ["config.yaml"]


def test_failed_dump_keeps_original(tmp_path, monkeypatch):
    p = _profile(tmp_path, {"a": 1})
    before = (p.home / "config.yaml").read_bytes()

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("commons.core.configio.yaml.safe_dump", boom)
    with pytest.raises(RuntimeError):
        YamlFileWriter().set(p, "b", 2)
    assert (p.home / "config.yaml").read_bytes() == before
    assert sorted(x.name for x in p.home.iterdir()) == ["config.yaml"]


def test_symlinked_config_is_written_through(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text("a: 1\n", encoding="utf-8")
    p = _profile(tmp_path)
    (p.home / "config.yaml").symlink_to(real)
    YamlFileWriter().set(p, "b", 2)
    assert (p.home / "config.yaml").is_symlink()
    assert yaml.safe_load(real.read_text(encoding="utf-8")) == {"a": 1, "b": 2}


def test_yaml_dump_flow():
    assert yaml_dump_flow("~/shared") == "~/shared"
    assert yaml_dump_flow(True) == "true"
    assert yaml_dump_flow(3) == "3"
    assert yaml_dump_flow(None) == "null"
    assert yaml_dump_flow(["a", "commons"]) == "[a, commons]"
    assert yaml_dump_flow({"k": [1, 2]}) == "{k: [1, 2]}"
    assert yaml_dump_flow("yes") == "'yes'"
    for value in ("~/shared", True, ["a b", "c: d"], {"k": "v"}, "x" * 200):
        assert yaml.safe_load(yaml_dump_flow(value)) == value
        assert "\n" not in yaml_dump_flow(value)


def test_argv_default_profile():
    assert hermes_config_set_argv("default", "skills.create_dir", "~/shared") == [
        "hermes",
        "config",
        "set",
        "--",
        "skills.create_dir",
        "~/shared",
    ]


def test_argv_named_profile():
    assert hermes_config_set_argv("device", "plugins.enabled", ["a", "commons"]) == [
        "hermes",
        "-p",
        "device",
        "config",
        "set",
        "--",
        "plugins.enabled",
        "[a, commons]",
    ]


@pytest.mark.parametrize("text", ["a: [unclosed\n", "- just\n- a list\n"])
def test_unreadable_config_is_never_overwritten(tmp_path, text):
    p = _profile(tmp_path)
    (p.home / "config.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        YamlFileWriter().set(p, "b", 2)
    assert (p.home / "config.yaml").read_text(encoding="utf-8") == text


def test_argv_option_like_value_follows_double_dash():
    argv = hermes_config_set_argv("default", "display.prefix", "-x")
    assert argv == ["hermes", "config", "set", "--", "display.prefix", "-x"]
    assert argv.index("--") < argv.index("-x")


def test_new_config_is_private(tmp_path):
    p = _profile(tmp_path)
    YamlFileWriter().set(p, "a", 1)
    assert stat.S_IMODE((p.home / "config.yaml").stat().st_mode) == 0o600


def test_existing_config_keeps_its_mode(tmp_path):
    p = _profile(tmp_path, {"a": 1})
    (p.home / "config.yaml").chmod(0o640)
    YamlFileWriter().set(p, "b", 2)
    assert stat.S_IMODE((p.home / "config.yaml").stat().st_mode) == 0o640


def test_parent_dir_fsynced_after_replace(tmp_path, monkeypatch):
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /proc to map fds to paths")
    p = _profile(tmp_path, {"a": 1})
    events: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        return real_fsync(fd)

    def spy_replace(a, b, *args, **kwargs):
        events.append(("replace", str(b)))
        return real_replace(a, b, *args, **kwargs)

    monkeypatch.setattr(configio.os, "fsync", spy_fsync)
    monkeypatch.setattr(configio.os, "replace", spy_replace)
    YamlFileWriter().set(p, "b", 2)
    target = str((p.home / "config.yaml").resolve())
    at = events.index(("replace", target))
    assert any(
        kind == "fsync" and path.startswith(str(p.home.resolve()) + "/.config.yaml.")
        for kind, path in events[:at]
    )
    assert ("fsync", str(p.home.resolve())) in events[at + 1 :]


def test_double_dash_is_accepted_by_real_hermes(tmp_path):
    """`hermes config set` is argparse with positionals key and value; '--' ends options."""
    python, src = _hermes_python(), _hermes_src()
    if python is None or src is None:
        pytest.skip("no Hermes runtime python or Hermes source checkout found")
    probe = (
        "import argparse, json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from hermes_cli.subcommands.config import build_config_parser\n"
        "p = argparse.ArgumentParser(); sp = p.add_subparsers(dest='command')\n"
        "build_config_parser(sp, cmd_config=lambda a: None)\n"
        "a = p.parse_args(json.loads(sys.argv[2]))\n"
        "print(json.dumps([a.config_command, a.key, a.value]))\n"
    )
    argv = hermes_config_set_argv("default", "display.prefix", "-x")[1:]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "h")}
    proc = subprocess.run(
        [str(python), "-I", "-c", probe, str(src), json.dumps(argv)],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == ["set", "display.prefix", "-x"]
