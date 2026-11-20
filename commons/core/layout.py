"""Where commons keeps its state, and the rule that keeps it out of every skills root.

Hermes' skill walk (agent/skill_utils.py iter_skill_index_files) prunes only
EXCLUDED_SKILL_DIRS, not dot-dirs, so a SKILL.md anywhere under a skills root
becomes a live skill: a backup, a retired copy or a merge staging folder
inside the shared root or a profile skills dir would be indexed, and could
make a name ambiguous. So all of that lives in a separate state dir chosen
by the caller (the Hermes adapter passes <default HERMES_HOME>/commons), and
Layout.check refuses any layout where that dir and a skills root overlap.

<shared_root>/.commons/ keeps only the git lock and config.json, never a
SKILL.md. Stdlib only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from commons.core.fleet import Profile
from commons.core.resolve import config_path

COMMONS_DIR = ".commons"
LOCK_FILE = "lock"
CONFIG_JSON = "config.json"
JOURNAL = "journal.json"


def check_id(backup_id: str) -> str:
    """Return *backup_id* if it is a single safe path component, else raise ValueError."""
    if not isinstance(backup_id, str) or "/" in backup_id or backup_id in ("", ".", ".."):
        raise ValueError(f"bad backup id {backup_id!r}")
    return backup_id


def _real(path: Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser().absolute()


def skill_roots(profiles: Iterable[Profile]) -> list[tuple[str, Path]]:
    """(label, resolved path) of every skills root the profiles read: skills dir, create_dir, external_dirs."""
    roots: list[tuple[str, Path]] = []
    for p in profiles:
        roots.append((f"the skills dir of profile {p.name}", _real(p.skills_dir)))
        create = p.create_dir()
        if create is not None:
            roots.append((f"skills.create_dir of profile {p.name}", config_path(p.home, create)))
        for ext in p.external_dirs():
            roots.append((f"a skills.external_dirs entry of profile {p.name}", config_path(p.home, ext)))
    return roots


def check_outside(path: Path, what: str, roots: Iterable[tuple[str, Path]]) -> None:
    """Raise ValueError if *path* is inside (or equal to) any root, or any root is inside *path*."""
    target = _real(path)
    for label, root in roots:
        if target == root or target.is_relative_to(root):
            raise ValueError(
                f"{what} {target} is inside {label} ({root}); Hermes would load any SKILL.md there as a skill"
            )
        if root.is_relative_to(target):
            raise ValueError(f"{label} ({root}) is inside {what} {target}; keep them apart")


@dataclass(frozen=True)
class Layout:
    shared_root: Path
    state_dir: Path

    @property
    def commons_dir(self) -> Path:
        return Path(self.shared_root) / COMMONS_DIR

    @property
    def lock_path(self) -> Path:
        return self.commons_dir / LOCK_FILE

    @property
    def config_path(self) -> Path:
        return self.commons_dir / CONFIG_JSON

    @property
    def backups_dir(self) -> Path:
        return Path(self.state_dir) / "backups"

    @property
    def staging_dir(self) -> Path:
        return Path(self.state_dir) / "staging"

    @property
    def proposals_dir(self) -> Path:
        return Path(self.state_dir) / "proposals"

    def backup_dir(self, backup_id: str) -> Path:
        return self.backups_dir / check_id(backup_id)

    def journal_path(self, backup_id: str) -> Path:
        return self.backup_dir(backup_id) / JOURNAL

    def check(self, profiles: Iterable[Profile]) -> None:
        """Raise ValueError unless state_dir is apart from the shared root and every skills root."""
        roots = [("the shared root", _real(self.shared_root)), *skill_roots(profiles)]
        check_outside(self.state_dir, "the commons state dir", roots)
