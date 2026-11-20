"""Reading and writing one key of a profile's config.yaml.

ConfigWriter is the interface setup and restore use. The real fleet goes
through `hermes config set` (HermesCliWriter, wired in the adapter phase);
this module only defines that command's argv as a pure function and never
runs Hermes. YamlFileWriter edits the file directly and is what tests (and
a fallback) use.

`hermes config set` keeps comments, key order and quoting (a ruamel
round-trip, utils.atomic_roundtrip_yaml_update), and YamlFileWriter matches
that for the keys commons writes: it edits only the lines of the key with
the line editor in yamledit.py, checks that the edited file parses to
exactly the intended data, and only otherwise (flow-style parent, unusual
layout, a quoted or spaced spelling of the key) falls back to a full
yaml.safe_dump, which drops comments. Every parse, the check of the edited
lines included, refuses duplicate mapping keys at any level (as Hermes'
ruamel round-trip does): an edit that would create one falls back, and a
config.yaml that already has one is refused with ConfigError, never
rewritten. Each
write returns 'edited', 'rewritten' (the fallback) or 'unchanged', and the
writer lists its fallbacks in .fallbacks so a caller can warn.
Stdlib + PyYAML.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from yaml.constructor import ConstructorError

from commons.core.fleet import CONFIG_FILE, DEFAULT_PROFILE, Profile
from commons.core.frontmatter import UniqueKeyLoader
from commons.core.yamledit import Lines, Unsupported


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


def lookup_key(data: dict, key: str) -> Any:
    """The value at dotted *key* in a parsed config, or None."""
    return _lookup(data, _parts(key))


def has_key(data: dict, key: str) -> bool:
    """True when dotted *key* is present in a parsed config (even with a null value)."""
    node: Any = data
    for part in _parts(key):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def set_key(data: dict, key: str, value: Any) -> None:
    """Set dotted *key* in a parsed config the way YamlFileWriter.set does."""
    _assign(data, key, value)


def unset_key(data: dict, key: str) -> bool:
    """Remove dotted *key* the way YamlFileWriter.unset does; True if it existed."""
    return _remove(data, _parts(key))


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


Outcome = Literal["edited", "rewritten", "unchanged"]


class YamlFileWriter:
    """Edit <profile.home>/config.yaml in place, keeping comments and order (see module docstring)."""

    def __init__(self) -> None:
        self.fallbacks: list[tuple[Path, str, str]] = []  # (config path, key, why) of full rewrites

    def _path(self, profile: Profile) -> Path:
        return profile.home / CONFIG_FILE

    def _text(self, profile: Profile) -> str | None:
        """The file as text, line endings kept (no universal-newline translation), BOM dropped."""
        try:
            return self._path(profile).read_bytes().decode("utf-8-sig")
        except FileNotFoundError:
            return None

    def _load(self, profile: Profile) -> dict:
        text = self._text(profile)
        return {} if text is None else self._parse(self._path(profile), text)

    def _parse(self, path: Path, text: str) -> dict:
        try:
            data = yaml.load(text, Loader=UniqueKeyLoader)
        except ConstructorError as exc:
            if "duplicate key" in str(exc):
                raise ConfigError(
                    f"{path} has a duplicate key ({exc.problem}); refusing to rewrite it"
                ) from exc
            raise ConfigError(f"{path} is not valid YAML; refusing to rewrite it") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML; refusing to rewrite it") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path} is not a mapping; refusing to rewrite it")
        return data

    def _edit(
        self, profile: Profile, key: str, data: dict, edit: Callable[[Lines, str, str], None]
    ) -> Outcome:
        """Write *data*, the intended config: by edit() on the lines when that yields exactly it, else a dump."""
        path = self._path(profile)
        text = self._text(profile)
        bom = text is not None and path.read_bytes().startswith(b"\xef\xbb\xbf")
        try:
            parts = _parts(key)
            if len(parts) != 2:
                raise Unsupported("only two-level keys are edited in place")
            lines = Lines(text or "")
            edit(lines, *parts)
            new = lines.text()
            try:
                same = self._parse(path, new) == data
            except ConfigError:
                same = False
            if not same:
                raise Unsupported("the edited lines would not hold the intended value")
        except Unsupported as exc:
            self.fallbacks.append((path, key, str(exc)))
            self._save(profile, data)
            return "rewritten"
        self._write(profile, ("\ufeff" if bom else "") + new)
        return "edited"

    def _save(self, profile: Profile, data: dict) -> None:
        self._write(
            profile, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
        )

    def _write(self, profile: Profile, content: str) -> None:
        # Write through a symlinked config.yaml so the link itself survives.
        path = self._path(profile).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600  # a new config may hold API keys: owner only
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
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

    def set(self, profile: Profile, key: str, value: Any) -> Outcome:
        data = self._load(profile)
        if has_key(data, key) and _lookup(data, _parts(key)) == value:
            return "unchanged"
        _assign(data, key, value)
        if isinstance(value, list):
            return self._edit(profile, key, data, lambda lines, top, sub: lines.set_list(top, sub, value))
        return self._edit(profile, key, data, lambda lines, top, sub: lines.set_scalar(top, sub, value))

    def _list(self, data: dict, key: str) -> list | None:
        current = _lookup(data, _parts(key))
        if current is not None and not isinstance(current, list):
            raise ConfigError(f"config key {key!r} is not a list")
        return current

    def add_to_list(self, profile: Profile, key: str, item: Any) -> Outcome:
        data = self._load(profile)
        current = self._list(data, key)
        if current is not None and item in current:
            return "unchanged"
        items = [*(current or []), item]
        _assign(data, key, items)
        return self._edit(profile, key, data, lambda lines, top, sub: lines.set_list(top, sub, items))

    def remove_from_list(self, profile: Profile, key: str, item: Any) -> Outcome:
        data = self._load(profile)
        current = self._list(data, key)
        if not current or item not in current:
            return "unchanged"
        items = [x for x in current if x != item]
        _assign(data, key, items)
        return self._edit(profile, key, data, lambda lines, top, sub: lines.set_list(top, sub, items))

    def unset(self, profile: Profile, key: str) -> Outcome:
        data = self._load(profile)
        if not _remove(data, _parts(key)):
            return "unchanged"
        return self._edit(profile, key, data, lambda lines, top, sub: lines.unset(top, sub))
