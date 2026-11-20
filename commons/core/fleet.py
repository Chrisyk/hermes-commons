"""Fleet discovery: find every Hermes profile under a Hermes root.

Profile "default" is the root itself; every other profile lives at
root/profiles/<name>/. A directory counts as a profile only if it has a
config.yaml. Stdlib + PyYAML only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_FILE = "config.yaml"
DEFAULT_PROFILE = "default"


def _section(config: dict, key: str) -> dict:
    value = config.get(key) if isinstance(config, dict) else None
    return value if isinstance(value, dict) else {}


def _expand(value: str) -> Path:
    return Path(value).expanduser()


@dataclass(frozen=True)
class Profile:
    name: str
    home: Path
    config: dict = field(default_factory=dict, hash=False)

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    def create_dir(self) -> Path | None:
        value = _section(self.config, "skills").get("create_dir")
        if not isinstance(value, str) or not value.strip():
            return None
        return _expand(value)

    def external_dirs(self) -> list[Path]:
        value = _section(self.config, "skills").get("external_dirs")
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [_expand(v) for v in value if isinstance(v, str) and v.strip()]

    def plugins_enabled(self) -> list[str]:
        value = _section(self.config, "plugins").get("enabled")
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str)]


def load_config(path: Path) -> dict:
    """Parse a config.yaml; return {} if missing, unreadable, malformed or not a mapping."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _profile(name: str, home: Path) -> Profile:
    return Profile(name=name, home=home, config=load_config(home / CONFIG_FILE))


def discover(root: Path, is_deleted: Callable[[Path], bool] = lambda p: False) -> list[Profile]:
    """Return the default profile (if root has a config.yaml) then named profiles sorted by name."""
    root = Path(root)
    found: list[Profile] = []
    if (root / CONFIG_FILE).is_file() and not is_deleted(root):
        found.append(_profile(DEFAULT_PROFILE, root))
    profiles_dir = root / "profiles"
    try:
        entries = sorted(profiles_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        entries = []
    for home in entries:
        if not home.is_dir() or not (home / CONFIG_FILE).is_file() or is_deleted(home):
            continue
        found.append(_profile(home.name, home))
    return found
