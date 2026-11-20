"""Skill walk and provenance for one profile.

Walks a profile's skills dir, reports every skill folder (a folder holding
SKILL.md) and classifies where it came from: bundled with Hermes, installed
from the hub, a symlink, or the profile's own. Stdlib + PyYAML only; the
Hermes hash functions are injected by the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml

from commons.core.fleet import Profile

SKILL_FILE = "SKILL.md"
MANIFEST_FILE = ".bundled_manifest"
HUB_LOCK = Path(".hub") / "lock.json"

_SKIP_DIRS = frozenset({"__pycache__", ".git"})
_SKIP_SUFFIXES = (".pyc",)


@dataclass(frozen=True)
class SkillCopy:
    profile: str
    name: str
    rel: str
    path: Path
    kind: str
    tree_hash: str
    origin_hash: str | None

    @property
    def id(self) -> str:
        return f"{self.profile}:{self.rel}"


def _hash_files(directory: Path) -> Iterator[tuple[str, Path]]:
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname.endswith(_SKIP_SUFFIXES):
                continue
            fpath = Path(dirpath) / fname
            if fpath.is_file():
                yield fpath.relative_to(directory).as_posix(), fpath


def tree_hash(directory: Path) -> str:
    """sha256 over files sorted by POSIX relpath, skipping __pycache__, .git and *.pyc."""
    directory = Path(directory)
    if directory.is_symlink():
        directory = directory.resolve()
    h = hashlib.sha256()
    for rel, fpath in sorted(_hash_files(directory)):
        try:
            data = fpath.read_bytes()
        except OSError:
            continue
        h.update(rel.encode("utf-8") + b"\0")
        h.update(data)
    return h.hexdigest()


def read_frontmatter_name(skill_md: Path) -> str | None:
    """Return the non-empty string 'name' from a leading '---' block, else None."""
    try:
        text = skill_md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            break
    else:
        return None
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def read_manifest(skills_dir: Path) -> dict[str, str]:
    """Parse .bundled_manifest as Hermes tools/skills_sync.py _read_manifest does.

    Lines are stripped and blank ones skipped. A line with ':' is v2 'name:hash'
    (both parts stripped, hash may be empty); a line without ':' is a v1 plain
    name with hash ''. Only entries whose name is empty (e.g. ':orphan') are
    dropped. A missing or unreadable file yields {}.
    """
    try:
        text = (skills_dir / MANIFEST_FILE).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    entries: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        if name:
            entries[name] = value.strip()
    return entries


def read_hub_lock(skills_dir: Path) -> dict[str, dict]:
    """Return lock.json 'installed' entries that are dicts; {} on any problem."""
    try:
        data = json.loads((skills_dir / HUB_LOCK).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        return {}
    return {k: v for k, v in installed.items() if isinstance(k, str) and isinstance(v, dict)}


def _walk_skill_dirs(skills_dir: Path) -> Iterator[Path]:
    """Yield skill folders (real or symlinked) without following symlinks or entering skills."""
    for dirpath, dirnames, _ in os.walk(skills_dir, followlinks=False):
        current = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if d.startswith("."):
                continue
            child = current / d
            if child.is_symlink():
                if (child / SKILL_FILE).is_file():
                    yield child
                continue
            if (child / SKILL_FILE).is_file():
                yield child
                continue
            keep.append(d)
        dirnames[:] = keep


def _hub_entry(lock: dict[str, dict], folder: str, name: str, rel: str) -> dict | None:
    for entry in lock.values():
        if entry.get("install_path") == rel:
            return entry
    return lock.get(folder) or lock.get(name)


def scan_profile(
    p: Profile,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
) -> list[SkillCopy]:
    """Return every skill copy in p.skills_dir with provenance, sorted by rel."""
    skills_dir = p.skills_dir
    if not skills_dir.is_dir():
        return []
    manifest = read_manifest(skills_dir)
    lock = read_hub_lock(skills_dir)
    copies = []
    for path in _walk_skill_dirs(skills_dir):
        rel = path.relative_to(skills_dir).as_posix()
        folder = path.name
        fm_name = read_frontmatter_name(path / SKILL_FILE)
        name = fm_name or folder
        # Hermes _discover_bundled_skills keys the manifest by the frontmatter
        # name, falling back to the folder name; frontmatter wins.
        key = next((k for k in (fm_name, folder) if k and k in manifest), None)
        origin: str | None = None
        if path.is_symlink():
            kind = "symlink"
        elif key is not None:
            origin = manifest[key]
            # An empty origin is a v1 entry: Hermes _update_existing_skill
            # re-baselines it from the user's copy, so it counts as pristine.
            if origin == "" or md5_dir_hash(path) == origin:
                kind = "bundled-pristine"
            else:
                kind = "bundled-edited"
        elif (entry := _hub_entry(lock, folder, name, rel)) is not None:
            recorded = entry.get("content_hash")
            origin = recorded if isinstance(recorded, str) else None
            kind = "hub-pristine" if origin is not None and hub_hash(path) == origin else "hub-edited"
        else:
            kind = "own"
        copies.append(
            SkillCopy(
                profile=p.name,
                name=name,
                rel=rel,
                path=path,
                kind=kind,
                tree_hash=tree_hash(path),
                origin_hash=origin,
            )
        )
    return sorted(copies, key=lambda c: c.rel)
