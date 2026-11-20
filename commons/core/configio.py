"""Reading and writing one key of a profile's config.yaml.

ConfigWriter is the interface setup and restore use. YamlFileWriter edits the
file directly (load, change a dotted key, atomic dump) and is what tests use.
The real fleet goes through `hermes config set` (HermesCliWriter, wired in the
adapter phase) because it keeps comments and formatting; this module only
defines that command's argv as a pure function and never runs Hermes.
Stdlib + PyYAML.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from commons.core.fleet import CONFIG_FILE, DEFAULT_PROFILE, Profile


class ConfigError(ValueError):
    """A config file or key that commons refuses to change."""


@runtime_checkable
class ConfigWriter(Protocol):
    def get(self, profile: Profile, key: str) -> Any: ...

    def set(self, profile: Profile, key: str, value: Any) -> None: ...

    def add_to_list(self, profile: Profile, key: str, item: Any) -> None: ...

    def remove_from_list(self, profile: Profile, key: str, item: Any) -> None: ...

    def unset(self, profile: Profile, key: str) -> None: ...


def _parts(key: str) -> list[str]:
    parts = key.split(".")
    if not key or any(not part for part in parts):
        raise ConfigError(f"bad config key {key!r}")
    return parts


def _lookup(data: dict, parts: list[str]) -> Any:
    node: Any = data
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _assign(data: dict, key: str, value: Any) -> None:
    parts = _parts(key)
    node = data
    for i, part in enumerate(parts[:-1]):
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        elif not isinstance(child, dict):
            where = ".".join(parts[: i + 1])
            raise ConfigError(f"cannot set {key!r}: {where!r} is not a mapping")
        node = child
    node[parts[-1]] = value


def _remove(data: dict, parts: list[str]) -> bool:
    """Delete a dotted key; drop dict containers it leaves empty. True if it existed.

    Mirrors Hermes hermes_cli/config.py _unset_nested, which `hermes config
    unset` uses.
    """
    chain: list[tuple[dict, str]] = []
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or not isinstance(node.get(part), dict):
            return False
        chain.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, part in reversed(chain):
        if parent[part] != {}:
            break
        del parent[part]
    return True


def yaml_dump_flow(value: Any) -> str:
    """One-line YAML for a value (flow style), as `hermes config set` expects."""
    text = yaml.safe_dump(
        value, default_flow_style=True, width=2**31 - 1, allow_unicode=True, sort_keys=False
    )
    text = text.removesuffix("\n...\n")
    return text.strip()


def hermes_config_set_argv(profile_name: str, key: str, value: Any) -> list[str]:
    """argv for `hermes [-p <name>] config set -- <key> <yaml value>`; the default profile takes no -p.

    `hermes config set` is a plain argparse subparser with two positionals
    (key, value; hermes_cli/subcommands/config.py), so '--' ends option
    parsing and a value such as '-x' is taken as the value, not an option.
    Hermes' own pre-argparse -p scan (hermes_cli/_parser.py scan_profile_flag)
    also stops at '--', so a value of '-p' cannot select a profile either.
    tests/test_core_configio.py checks this against the real Hermes parser.
    """
    _parts(key)
    argv = ["hermes", "config", "set", "--", key, yaml_dump_flow(value)]
    if profile_name != DEFAULT_PROFILE:
        argv[1:1] = ["-p", profile_name]
    return argv


def hermes_config_unset_argv(profile_name: str, key: str) -> list[str]:
    """argv for `hermes [-p <name>] config unset -- <key>` (one positional, hermes_cli/subcommands/config.py)."""
    _parts(key)
    argv = ["hermes", "config", "unset", "--", key]
    if profile_name != DEFAULT_PROFILE:
        argv[1:1] = ["-p", profile_name]
    return argv


class YamlFileWriter:
    """Edit <profile.home>/config.yaml in place. Comments are not preserved."""

    def _path(self, profile: Profile) -> Path:
        return profile.home / CONFIG_FILE

    def _load(self, profile: Profile) -> dict:
        path = self._path(profile)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML; refusing to rewrite it") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path} is not a mapping; refusing to rewrite it")
        return data

    def _save(self, profile: Profile, data: dict) -> None:
        # Write through a symlinked config.yaml so the link itself survives.
        path = self._path(profile).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600  # a new config may hold API keys: owner only
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        # Make the rename itself durable.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def get(self, profile: Profile, key: str) -> Any:
        return _lookup(self._load(profile), _parts(key))

    def set(self, profile: Profile, key: str, value: Any) -> None:
        data = self._load(profile)
        _assign(data, key, value)
        self._save(profile, data)

    def _list(self, data: dict, key: str) -> list | None:
        current = _lookup(data, _parts(key))
        if current is not None and not isinstance(current, list):
            raise ConfigError(f"config key {key!r} is not a list")
        return current

    def add_to_list(self, profile: Profile, key: str, item: Any) -> None:
        data = self._load(profile)
        current = self._list(data, key)
        if current is not None and item in current:
            return
        _assign(data, key, [*(current or []), item])
        self._save(profile, data)

    def remove_from_list(self, profile: Profile, key: str, item: Any) -> None:
        data = self._load(profile)
        current = self._list(data, key)
        if not current or item not in current:
            return
        _assign(data, key, [x for x in current if x != item])
        self._save(profile, data)

    def unset(self, profile: Profile, key: str) -> None:
        data = self._load(profile)
        if _remove(data, _parts(key)):
            self._save(profile, data)
