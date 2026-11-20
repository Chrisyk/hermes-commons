"""Skill walk and provenance for one profile.

Walks a profile's skills dir the way Hermes discovers skills, reports every
skill folder (a folder holding SKILL.md, nested ones included) and classifies
where it came from: bundled with Hermes, installed from the hub, reached
through a symlink, or the profile's own. Stdlib only; Hermes is never
imported. The Hermes hash functions are injected by the caller and the Hermes
discovery and naming rules are mirrored here (each mirror cites its source).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from commons.core.fleet import Profile

SKILL_FILE = "SKILL.md"
MANIFEST_FILE = ".bundled_manifest"
HUB_LOCK = Path(".hub") / "lock.json"

# Mirror of Hermes agent/skill_utils.py EXCLUDED_SKILL_DIRS and SKILL_SUPPORT_DIRS.
EXCLUDED_SKILL_DIRS = frozenset(
    {
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".curator_backups",
        ".locks",
        ".hermes-dist",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
SKILL_SUPPORT_DIRS = frozenset({"references", "templates", "assets", "scripts"})

_SKIP_SUFFIXES = (".pyc", ".pyo")
_CHUNK = 1 << 20

Kind = Literal["own", "bundled-pristine", "bundled-edited", "hub-pristine", "hub-edited", "symlink"]


@dataclass(frozen=True)
class SkillCopy:
    profile: str
    name: str
    rel: str
    path: Path
    kind: Kind
    tree_hash: str
    origin_hash: str | None
    parent_rel: str | None = None
    via_symlink: bool = False
    symlink_target: Path | None = None
    error: str | None = None

    @property
    def id(self) -> str:
        return f"{self.profile}:{self.rel}"


def _describe(exc: OSError, rel: str) -> str:
    reason = exc.strerror or errno.errorcode.get(exc.errno or 0, "") or exc.__class__.__name__
    return f"unreadable: {rel}: {reason}"


def _hash_entries(directory: Path) -> tuple[list[tuple[str, str, str]], str | None]:
    """List (rel, kind, path) for hashing, kind 'F' file or 'L' symlink; plus the first walk error."""
    entries: list[tuple[str, str, str]] = []
    errors: list[OSError] = []
    top = os.fspath(directory)
    for dirpath, dirnames, filenames in os.walk(top, onerror=errors.append, followlinks=False):
        keep = []
        for d in dirnames:
            if d in EXCLUDED_SKILL_DIRS:
                continue
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                entries.append((os.path.relpath(full, top).replace(os.sep, "/"), "L", full))
            else:
                keep.append(d)
        dirnames[:] = keep
        for fname in filenames:
            if fname.endswith(_SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, fname)
            kind = "L" if os.path.islink(full) else "F"
            entries.append((os.path.relpath(full, top).replace(os.sep, "/"), kind, full))
    if errors:
        exc = errors[0]
        rel = os.path.relpath(exc.filename, top) if exc.filename else "."
        return entries, _describe(exc, rel.replace(os.sep, "/"))
    return entries, None


def _hash_file(h, rel: str, full: str) -> None:
    """Feed one regular file as rel NUL size NUL bytes; skip non-regular files (FIFOs, sockets)."""
    if not stat.S_ISREG(os.lstat(full).st_mode):
        return
    with open(full, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        h.update(rel.encode("utf-8") + b"\0" + str(size).encode() + b"\0")
        seen = 0
        while chunk := fh.read(_CHUNK):
            seen += len(chunk)
            h.update(chunk)
    if seen != size:
        raise OSError(errno.EIO, "changed while hashing", full)


def _tree_hash(directory: Path) -> tuple[str, str | None]:
    """Return (hash, error); hash is '' whenever any part of the tree could not be read."""
    entries, error = _hash_entries(Path(directory))
    if error:
        return "", error
    h = hashlib.sha256()
    for rel, kind, full in sorted(entries):
        try:
            if kind == "L":
                target = os.readlink(full).encode("utf-8", "surrogateescape")
                h.update(rel.encode("utf-8") + b"\0L" + str(len(target)).encode() + b"\0" + target)
            else:
                _hash_file(h, rel, full)
        except OSError as exc:
            return "", _describe(exc, rel)
    return h.hexdigest(), None


def tree_hash(directory: Path) -> str:
    """sha256 identity of a skill tree, '' if any part is unreadable.

    Files are sorted by POSIX relpath and each is framed as relpath NUL size NUL
    bytes, so different trees cannot collide. EXCLUDED_SKILL_DIRS and
    *.pyc/*.pyo are skipped. A symlink inside the tree is recorded as 'L' plus
    its target text and never followed; the top folder itself may be a symlink.
    """
    return _tree_hash(directory)[0]


def read_skill_name(skill_md: Path, fallback: str) -> str:
    """Mirror of Hermes tools/skill_usage.py read_skill_name.

    Line-based, not YAML: the first ``name:`` line with a non-empty value
    between the first ``---`` line and the next one (or the end of the first
    4000 chars), quotes stripped; otherwise *fallback*.
    """
    return read_frontmatter_value(skill_md, "name") or fallback


def read_frontmatter_value(skill_md: Path, key: str) -> str:
    """Line-based frontmatter read behind read_skill_name.

    Same rules as Hermes read_skill_name for ``name``, generalized to any key:
    the first non-empty ``<key>:`` value in the first ``---`` block of the
    first 4000 chars, quotes stripped; '' when missing or unreadable.
    """
    try:
        text = skill_md.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text[:4000].split("\n")]
    if "---" not in lines:
        return ""
    block = lines[lines.index("---") + 1 :]
    block = block[: block.index("---")] if "---" in block else block
    prefix = f"{key}:"
    values = (line.split(":", 1)[1].strip().strip("\"'") for line in block if line.startswith(prefix))
    return next((v for v in values if v), "")


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
        data = json.loads((skills_dir / HUB_LOCK).read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        return {}
    return {k: v for k, v in installed.items() if isinstance(k, str) and isinstance(v, dict)}


@dataclass(frozen=True)
class _Found:
    path: Path
    rel: str
    parent_rel: str | None
    via_symlink: bool


def _walk_skill_dirs(skills_dir: Path) -> Iterator[_Found]:
    """Mirror of Hermes agent/skill_utils.py iter_skill_index_files for SKILL.md.

    os.walk with followlinks=True; prunes EXCLUDED_SKILL_DIRS everywhere and
    SKILL_SUPPORT_DIRS inside a skill folder; nested skills are reported too.
    Unlike Hermes, a directory whose real path is already on the current chain
    (a symlink cycle) is not entered, so the walk always terminates.
    Unreadable directories are skipped (onerror), never raised.
    """
    top = os.fspath(skills_dir)
    # Per pending directory: (realpaths on its chain, nearest enclosing skill rel, reached via symlink).
    state: dict[str, tuple[frozenset[str], str | None, bool]] = {
        top: (frozenset({os.path.realpath(top)}), None, False)
    }
    for root, dirs, files in os.walk(top, onerror=lambda exc: None, followlinks=True):
        chain, enclosing, via_link = state.pop(root, (frozenset(), None, False))
        has_skill_md = SKILL_FILE in files
        rel = "." if root == top else os.path.relpath(root, top).replace(os.sep, "/")
        if has_skill_md:
            yield _Found(Path(root), rel, enclosing, via_link)
        child_enclosing = rel if has_skill_md else enclosing
        keep = []
        for d in dirs:
            if d in EXCLUDED_SKILL_DIRS or (has_skill_md and d in SKILL_SUPPORT_DIRS):
                continue
            full = os.path.join(root, d)
            real = os.path.realpath(full)
            if real in chain:
                continue
            keep.append(d)
            state[full] = (chain | {real}, child_enclosing, via_link or os.path.islink(full))
        dirs[:] = keep


def _hub_entry(lock: dict[str, dict], name: str, rel: str) -> dict | None:
    """Mirror of Hermes tools/skill_usage.py _read_hub_installed_names, per copy.

    An entry with an install_path identifies only the skill at that path; an
    entry without one identifies a skill by its lock key == resolved name.
    """
    by_name = None
    for key, entry in lock.items():
        install_path = entry.get("install_path")
        if isinstance(install_path, str) and install_path.strip():
            if posixpath.normpath(install_path.strip()) == rel:
                return entry
        elif by_name is None and key == name:
            by_name = entry
    return by_name


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _classify(
    found: _Found,
    name: str,
    error: str | None,
    manifest: dict[str, str],
    lock: dict[str, dict],
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
) -> tuple[Kind, str | None]:
    """Return (kind, origin_hash). An unreadable tree can never be proven pristine."""
    if found.via_symlink:
        return "symlink", None
    # Mirror of Hermes tools/skill_usage.py is_bundled: the resolved name only.
    if name in manifest:
        origin = manifest[name]
        # An empty origin is a v1 entry: Hermes _update_existing_skill
        # re-baselines it from the user's copy, so it counts as pristine.
        if error is None and (origin == "" or md5_dir_hash(found.path) == origin):
            return "bundled-pristine", origin
        return "bundled-edited", origin
    entry = _hub_entry(lock, name, found.rel)
    if entry is not None:
        recorded = entry.get("content_hash")
        origin = recorded if isinstance(recorded, str) else None
        if error is None and origin is not None and hub_hash(found.path) == origin:
            return "hub-pristine", origin
        return "hub-edited", origin
    return "own", None


def scan_profile(
    p: Profile,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
) -> list[SkillCopy]:
    """Return every skill copy in p.skills_dir with provenance, sorted by rel."""
    return scan_root(p.skills_dir, p.name, md5_dir_hash, hub_hash)


def scan_root(
    root: Path,
    label: str,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
) -> list[SkillCopy]:
    """Return every skill copy under any skills root (profile, shared, external, project).

    Copies carry *label* as their profile. Provenance reads .bundled_manifest
    and .hub/lock.json at *root* when present; without them every copy not
    reached through a symlink is 'own'. A missing root yields [].
    """
    skills_dir = Path(root)
    if not _is_dir(skills_dir):
        return []
    manifest = read_manifest(skills_dir)
    lock = read_hub_lock(skills_dir)
    copies = []
    for found in _walk_skill_dirs(skills_dir):
        path = found.path
        name = read_skill_name(path / SKILL_FILE, path.name)
        digest, error = _tree_hash(path)
        kind, origin = _classify(found, name, error, manifest, lock, md5_dir_hash, hub_hash)
        target = Path(os.path.realpath(path)) if os.path.islink(path) else None
        copies.append(
            SkillCopy(
                profile=label,
                name=name,
                rel=found.rel,
                path=path,
                kind=kind,
                tree_hash=digest,
                origin_hash=origin,
                parent_rel=found.parent_rel,
                via_symlink=found.via_symlink,
                symlink_target=target,
                error=error,
            )
        )
    return sorted(copies, key=lambda c: c.rel)
