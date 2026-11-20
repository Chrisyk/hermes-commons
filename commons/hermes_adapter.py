"""The one module that imports Hermes. Everything else in commons is plain Python.

Every Hermes import sits inside a function and names a symbol from the public
plugin API (website/docs/developer-guide/plugins/public-api.md in Hermes,
also hermes_cli/data/plugin_api.json, which `hermes plugins validate` reads):

- hermes_constants (whole module): get_default_hermes_root, get_hermes_home,
  named_profile_is_deleted, profile_name_for_home, get_bundled_skills_dir
- hermes_cli.config (whole module): get_project_root
- hermes_cli.chat_launch.hermes_command: the argv that runs this Hermes
- tools.skills_tool: skill_lookup_path_error, skill_search_dirs, locate_skill
- tools.skills_sync.dir_hash: the .bundled_manifest hash
- tools.skill_usage.read_skill_name

A missing module or name raises HermesApiChanged, which names the symbol, so
a Hermes release that moves one fails loudly instead of half working.

Two things have no public API and are mirrored here instead (keep in step
with Hermes v0.23.1):

- hub_hash mirrors tools/skills_guard.py content_hash, the hash Hermes writes
  to <skills>/.hub/lock.json.
- bundled_index mirrors tools/skills_sync.py _discover_bundled_skills: the
  SKILL.md files under the bundled skills dir (get_bundled_skills_dir with
  <project root>/skills as the source-checkout default, the same call
  _get_bundled_dir makes), skipping agent/skill_utils.py EXCLUDED_SKILL_DIRS
  and support dirs (commons.core.scan keeps both lists), each named by its
  frontmatter name and hashed with dir_hash, the value the sync stores as a
  skill's origin hash. The first folder found for a name wins.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path, PurePath
from typing import Any

from commons.core.configio import ConfigError, Outcome, YamlFileWriter, has_key, yaml_dump_flow
from commons.core.fleet import CONFIG_FILE, DEFAULT_PROFILE, Profile, discover, load_config
from commons.core.layout import Layout
from commons.core.report import shared_root_of
from commons.core.scan import EXCLUDED_SKILL_DIRS, SKILL_FILE, SKILL_SUPPORT_DIRS

STATE_DIR_NAME = "commons"
SHARED_DIR_NAME = "shared-skills"
DEFAULT_CLI_TIMEOUT = 60.0
# Mirror of hermes_constants.PROFILE_ID_RE (the -p grammar).
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class HermesApiChanged(RuntimeError):
    """A Hermes symbol commons relies on is gone or moved."""


@contextlib.contextmanager
def _api(symbol: str) -> Iterator[None]:
    try:
        yield
    except ImportError as exc:
        raise HermesApiChanged(
            f"Hermes no longer provides {symbol}; this commons release does not match the installed Hermes"
        ) from exc


# ------------------------------------------------------------------ paths and profiles


def default_root() -> Path:
    """The Hermes root: the default profile's home, parent of profiles/."""
    with _api("hermes_constants.get_default_hermes_root"):
        from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def state_dir() -> Path:
    """<default HERMES_HOME>/commons: backups, staging, proposals, cache. Outside every skills root."""
    return default_root() / STATE_DIR_NAME


def active_profile_home() -> Path:
    with _api("hermes_constants.get_hermes_home"):
        from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def active_profile_name() -> str:
    with _api("hermes_constants.profile_name_for_home"):
        from hermes_constants import profile_name_for_home
    return profile_name_for_home(active_profile_home()) or DEFAULT_PROFILE


def is_deleted(path: Path) -> bool:
    with _api("hermes_constants.named_profile_is_deleted"):
        from hermes_constants import named_profile_is_deleted
    return bool(named_profile_is_deleted(path))


def fleet() -> list[Profile]:
    """Every live profile under the Hermes root (commons.core.fleet.discover)."""
    return discover(default_root(), is_deleted)


def layout(profiles: Sequence[Profile], shared_root: Path | None = None) -> Layout:
    """The commons Layout: *shared_root*, else the create_dir most profiles use, else <root>/shared-skills."""
    root = shared_root or shared_root_of(profiles) or default_root() / SHARED_DIR_NAME
    return Layout(shared_root=Path(root).expanduser(), state_dir=state_dir())


# ------------------------------------------------------------------ hashes


def md5_dir_hash(directory: Path) -> str:
    with _api("tools.skills_sync.dir_hash"):
        from tools.skills_sync import dir_hash
    return dir_hash(Path(directory))


def _content_digest(skill_path: Path) -> str:
    if not skill_path.is_dir():
        return hashlib.sha256(skill_path.read_bytes()).hexdigest()
    h = hashlib.sha256()
    for rel, p in sorted(
        (p.relative_to(skill_path).as_posix(), p) for p in skill_path.rglob("*") if p.is_file()
    ):
        h.update(rel.encode("utf-8") + b"\x00")
        h.update(p.read_bytes())
    return h.hexdigest()


def hub_hash(path: Path) -> str:
    """Mirror of Hermes tools/skills_guard.py content_hash (no public API for it)."""
    return f"sha256:{_content_digest(Path(path))[:16]}"


def _bundled_dir() -> Path:
    with _api("hermes_cli.config.get_project_root"):
        from hermes_cli.config import get_project_root
    with _api("hermes_constants.get_bundled_skills_dir"):
        from hermes_constants import get_bundled_skills_dir
    return Path(get_bundled_skills_dir(Path(get_project_root()) / "skills"))


def _excluded(rel: PurePath, root: Path) -> bool:
    parts = rel.parts
    if any(part in EXCLUDED_SKILL_DIRS for part in parts):
        return True
    return any(
        part in SKILL_SUPPORT_DIRS and (root / Path(*parts[:idx]) / SKILL_FILE).exists()
        for idx, part in enumerate(parts[:-1])
        if idx > 0
    )


def bundled_index() -> Callable[[str], str | None]:
    """name -> dir_hash of the bundled skill Hermes ships now (None if it ships none), for check."""
    with _api("tools.skill_usage.read_skill_name"):
        from tools.skill_usage import read_skill_name
    root = _bundled_dir()
    folders: dict[str, Path] = {}
    if root.is_dir():
        for md in sorted(root.rglob(SKILL_FILE)):
            if _excluded(md.relative_to(root), root):
                continue
            folders.setdefault(read_skill_name(md, md.parent.name), md.parent)
    hashes: dict[str, str] = {}

    def lookup(name: str) -> str | None:
        folder = folders.get(name)
        if folder is None:
            return None
        if name not in hashes:
            hashes[name] = md5_dir_hash(folder)
        return hashes[name]

    return lookup


# ------------------------------------------------------------------ skill lookup


def locate(name: str) -> Path | None:
    """The folder (or legacy flat .md) skill_view(name) would load in the active profile, else None."""
    with _api("tools.skills_tool.skill_lookup_path_error"):
        from tools.skills_tool import skill_lookup_path_error
    with _api("tools.skills_tool.skill_search_dirs"):
        from tools.skills_tool import skill_search_dirs
    with _api("tools.skills_tool.locate_skill"):
        from tools.skills_tool import locate_skill
    if skill_lookup_path_error(name):
        return None
    roots, _active = skill_search_dirs()
    error, skill_dir, skill_md = locate_skill(name, None, roots)
    if error is not None:
        return None
    found = skill_dir if skill_dir is not None else skill_md
    return Path(found) if found is not None else None


# ------------------------------------------------------------------ config writes


def hermes_command(args: Sequence[str]) -> list[str]:
    with _api("hermes_cli.chat_launch.hermes_command"):
        from hermes_cli.chat_launch import hermes_command as command
    return list(command(list(args)))


class HermesCliWriter:
    """ConfigWriter that changes config.yaml only through `hermes -p <name> config set|unset`.

    Verified against Hermes v0.23.1 (hermes_cli/subcommands/config.py and
    hermes_cli/config.py set_config_value):
    - `config set -- KEY VALUE` and `config unset -- KEY` take positionals;
      '--' ends option parsing, and Hermes' -p pre-scan stops there too.
    - `config unset` of an absent key exits 1 ("Config key not set"), so an
      absent key is skipped here.
    - There is no list append: set writes a whole list given as a YAML/JSON
      flow literal, so add/remove read the list, compute the new one and set it.
    - A string is passed verbatim (a string-typed setting is stored verbatim,
      others are coerced), every other value as one-line YAML.
    - `-p <name>` is always given, `default` included: without it Hermes
      would follow the sticky active_profile file. HERMES_HOME is the root.
    Every write is read back from config.yaml; a value that did not land
    (coerced, managed, wrong file) raises ConfigError. Reads parse the file.
    """

    def __init__(
        self,
        root: Path | None = None,
        timeout: float = DEFAULT_CLI_TIMEOUT,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        command: Callable[[Sequence[str]], list[str]] = hermes_command,
    ) -> None:
        self._root = Path(root) if root is not None else None
        self.timeout = timeout
        self.runner = runner
        self.command = command
        self._reader = YamlFileWriter()

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = default_root()
        return self._root

    def _check_home(self, profile: Profile) -> None:
        if not _PROFILE_ID.fullmatch(profile.name):
            # Hermes casefolds -p and would pick another profile, or refuse the name.
            raise ConfigError(
                f"profile name {profile.name!r} is not one `hermes -p` accepts; refusing to write"
            )
        expected = self.root if profile.name == DEFAULT_PROFILE else self.root / "profiles" / profile.name
        if Path(profile.home).absolute() != expected.absolute():
            raise ConfigError(
                f"profile {profile.name} lives at {profile.home}, not {expected}; refusing to write"
            )

    def _run(self, profile: Profile, args: list[str]) -> None:
        self._check_home(profile)
        argv = self.command(["-p", profile.name, "config", *args])
        env = {**os.environ, "HERMES_HOME": str(self.root)}
        shown = " ".join(argv)
        try:
            proc = self.runner(
                argv, env=env, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise ConfigError(f"`{shown}` timed out after {self.timeout:g} s") from exc
        except OSError as exc:
            raise ConfigError(f"could not run `{shown}`: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ConfigError(f"`{shown}` failed (exit {proc.returncode}): {detail}")

    def _data(self, profile: Profile) -> dict:
        return load_config(Path(profile.home) / CONFIG_FILE)

    def get(self, profile: Profile, key: str) -> Any:
        return self._reader.get(profile, key)

    def _set(self, profile: Profile, key: str, value: Any) -> Outcome:
        raw = value if isinstance(value, str) else yaml_dump_flow(value)
        self._run(profile, ["set", "--", key, raw])
        now = self.get(profile, key)
        if now != value:
            raise ConfigError(
                f"hermes config set {key} did not take in {profile.name}: wanted {value!r}, got {now!r}"
            )
        return "edited"

    def set(self, profile: Profile, key: str, value: Any) -> Outcome:
        if has_key(self._data(profile), key) and self.get(profile, key) == value:
            return "unchanged"
        return self._set(profile, key, value)

    def _list(self, profile: Profile, key: str) -> list:
        current = self.get(profile, key)
        if current is not None and not isinstance(current, list):
            raise ConfigError(f"config key {key!r} of {profile.name} is not a list")
        return list(current or [])

    def add_to_list(self, profile: Profile, key: str, item: Any) -> Outcome:
        current = self._list(profile, key)
        if item in current:
            return "unchanged"
        return self._set(profile, key, [*current, item])

    def remove_from_list(self, profile: Profile, key: str, item: Any) -> Outcome:
        current = self._list(profile, key)
        if item not in current:
            return "unchanged"
        return self._set(profile, key, [x for x in current if x != item])

    def unset(self, profile: Profile, key: str) -> Outcome:
        if not has_key(self._data(profile), key):
            return "unchanged"
        self._run(profile, ["unset", "--", key])
        if has_key(self._data(profile), key):
            raise ConfigError(f"hermes config unset {key} did not take in {profile.name}")
        return "edited"
