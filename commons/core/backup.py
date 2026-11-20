"""Profile backups under <shared_root>/.commons/backups/<id>/<profile>/.

A backup holds each profile's config.yaml (if present) and its whole skills/
dir, copied with symlinks kept as symlinks (never followed out of the tree),
plus manifest.json with hashes so a backup can be verified before restore.
A symlinked config.yaml or skills/ dir is copied by content and the link is
recorded in the manifest so restore can put the link back.

Durability: every copied file and every created directory is fsynced before
manifest.json is written (tmp + fsync + rename + fsync of the backup dir), so a
manifest only exists once the data under it is on disk. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from commons.core.fleet import CONFIG_FILE, Profile

STATE_DIR = ".commons"
BACKUPS_DIR = "backups"
MANIFEST = "manifest.json"
SKILLS = "skills"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    pass


def backups_root(shared_root: Path) -> Path:
    return Path(shared_root) / STATE_DIR / BACKUPS_DIR


def _is_excluded(path: Path, exclude: Iterable[Path]) -> bool:
    return any(path == e for e in exclude)


def _walk(root: Path, exclude: tuple[Path, ...] = ()) -> list[tuple[str, Path]]:
    """Every entry under root as (posix relpath, path), sorted, never following symlinks."""
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_excluded((here / d).resolve(), exclude)]
        for name in dirnames + filenames:
            p = here / name
            found.append((p.relative_to(root).as_posix(), p))
    found.sort(key=lambda item: item[0])
    return found


def tree_sha256(root: Path, exclude: tuple[Path, ...] = ()) -> str | None:
    """Deterministic sha256 over (relpath, type, bytes or link target, exec bit); None if root is absent.

    Directories are included so empty folders count. Symlinks hash their target
    text and are never followed. exclude lists resolved directory paths to skip.
    """
    root = Path(root)
    if not root.is_dir():
        return None
    h = hashlib.sha256()
    for rel, p in _walk(root, exclude):
        st = p.lstat()
        h.update(rel.encode("utf-8") + b"\x00")
        if stat.S_ISLNK(st.st_mode):
            h.update(b"l\x00" + os.fsencode(os.readlink(p)) + b"\x00")
        elif stat.S_ISDIR(st.st_mode):
            h.update(b"d\x00")
        elif stat.S_ISREG(st.st_mode):
            exec_bit = b"x" if st.st_mode & stat.S_IXUSR else b"-"
            h.update(b"f\x00" + exec_bit + b"\x00" + str(st.st_size).encode() + b"\x00")
            if _hash_into(h, p) != st.st_size:
                # Size changed between lstat and read: never equal to a stable copy.
                h.update(b"\x00size-changed")
        else:
            h.update(b"o\x00")
    return h.hexdigest()


def count_files(root: Path, exclude: tuple[Path, ...] = ()) -> int:
    """Non-directory entries (files and symlinks, a symlinked folder counts once)."""
    root = Path(root)
    if not root.is_dir():
        return 0
    return sum(1 for _, p in _walk(root, exclude) if p.is_symlink() or not p.is_dir())


def _hash_into(h, path: Path) -> int:
    """Feed path's bytes to h in 1 MiB chunks; return the byte count."""
    total = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            total += len(chunk)
    return total


def _file_sha256(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        _hash_into(h, path)
    except FileNotFoundError:
        return None
    return h.hexdigest()


def _fsync_path(path: Path) -> None:
    """fsync a file or directory by path (directories make their entries durable)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdirs_durable(path: Path) -> None:
    """mkdir -p, then fsync every created dir and the existing parent it was created in."""
    created: list[Path] = []
    p = path
    while not p.exists():
        created.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for d in [*created, p]:
        _fsync_path(d)


def _copy_file(src: Path, dst: Path) -> None:
    """Copy one regular file (data, mode, times) and fsync the copy."""
    shutil.copy2(src, dst, follow_symlinks=False)
    _fsync_path(dst)


def _copy_tree(src: Path, dst: Path, exclude: tuple[Path, ...]) -> None:
    """Copy src to a new dir dst, symlinks kept as links; fsync every file, then every dir."""
    dst.mkdir()
    dirs = [dst]
    for rel, p in _walk(src, exclude):
        target = dst / rel
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode):
            target.symlink_to(os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            target.mkdir()
            dirs.append(target)
        elif stat.S_ISREG(st.st_mode):
            _copy_file(p, target)
        else:
            # A fifo or socket would block or fail a copy; refuse instead.
            raise BackupError(f"cannot back up {p}: not a file, folder or symlink")
    # Deepest first, so each dir's entries are durable before its parent's.
    # Like copytree, dir mode and times are copied once their contents are in.
    for d in sorted(dirs, key=lambda q: len(q.parts), reverse=True):
        shutil.copystat(src / d.relative_to(dst), d)
        _fsync_path(d)


def _new_backup_dir(root: Path, stamp: str, label: str) -> tuple[str, Path]:
    _mkdirs_durable(root)
    n = 1
    while True:
        bid = f"{stamp}-{label}" if n == 1 else f"{stamp}-{label}-{n}"
        target = root / bid
        try:
            target.mkdir()
        except FileExistsError:
            n += 1
            continue
        _fsync_path(root)
        return bid, target


def _write_json_atomic(path: Path, data: dict) -> None:
    """tmp + fsync + os.replace + fsync of the parent dir."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_path(path.parent)


def _link_target(path: Path) -> str | None:
    return os.readlink(path) if path.is_symlink() else None


def create_backup(shared_root: Path, profiles: list[Profile], label: str, now: datetime | None = None) -> str:
    """Snapshot every profile and return the backup id '<UTC %Y%m%dT%H%M%SZ>-<label>[-N]'.

    The copy is hashed against the source; any mismatch (for example a skill
    edited mid-copy) raises BackupError and leaves the partial backup without
    a manifest, so list_backups ignores it.
    """
    if not _LABEL.fullmatch(label):
        raise ValueError(f"bad backup label {label!r}: use letters, digits, '.', '_' or '-'")
    names = [p.name for p in profiles]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate profile names in {names}")
    now = now or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    root = backups_root(shared_root)
    bid, target = _new_backup_dir(root, now.strftime("%Y%m%dT%H%M%SZ"), label)
    # If the shared root sits inside a skills dir, never copy its state folder.
    exclude = ((Path(shared_root) / STATE_DIR).resolve(),)

    entries: dict[str, dict] = {}
    for profile in profiles:
        dest = target / profile.name
        dest.mkdir()
        config = profile.home / CONFIG_FILE
        if config.is_file():
            shutil.copy2(config, dest / CONFIG_FILE, follow_symlinks=True)
            _fsync_path(dest / CONFIG_FILE)
        skills = profile.skills_dir
        source_hash = tree_sha256(skills, exclude)
        if skills.is_dir():
            # A symlinked skills dir is followed at the top only (its contents are copied).
            try:
                _copy_tree(skills, dest / SKILLS, exclude)
            except FileNotFoundError as exc:
                raise BackupError(
                    f"backup {bid}: {profile.name} skills changed while copying ({exc})"
                ) from exc
        _fsync_path(dest)
        copy_hash = tree_sha256(dest / SKILLS)
        if copy_hash != source_hash:
            raise BackupError(f"backup {bid}: {profile.name} skills changed while copying")
        config_hash = _file_sha256(config) if config.is_file() else None
        if config_hash != _file_sha256(dest / CONFIG_FILE):
            raise BackupError(f"backup {bid}: {profile.name} config.yaml changed while copying")
        config_link = _link_target(config)
        skills_link = _link_target(skills)
        entries[profile.name] = {
            "home": str(profile.home),
            "config_sha256": config_hash,
            "config_is_symlink": config_link is not None,
            "config_link_target": config_link,
            "skills_files": count_files(dest / SKILLS),
            "skills_tree_sha256": source_hash,
            "skills_is_symlink": skills_link is not None,
            "skills_link_target": skills_link,
        }

    _fsync_path(target)
    manifest = {"id": bid, "created": now.isoformat(), "label": label, "profiles": entries}
    _write_json_atomic(target / MANIFEST, manifest)
    return bid


def list_backups(shared_root: Path) -> list[str]:
    """Ids of complete backups (those with a manifest), sorted oldest first."""
    root = backups_root(shared_root)
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and (d / MANIFEST).is_file()]
    except FileNotFoundError:
        return []
    return sorted(d.name for d in dirs)


def load_manifest(shared_root: Path, backup_id: str) -> dict:
    if "/" in backup_id or backup_id in ("", ".", ".."):
        raise ValueError(f"bad backup id {backup_id!r}")
    return json.loads((backups_root(shared_root) / backup_id / MANIFEST).read_text(encoding="utf-8"))


def verify_backup(shared_root: Path, backup_id: str) -> list[str]:
    """Recompute the backup copy's hashes against its manifest; return problems (empty = clean)."""
    try:
        manifest = load_manifest(shared_root, backup_id)
    except (OSError, ValueError) as exc:
        return [f"{backup_id}: cannot read manifest ({exc})"]
    base = backups_root(shared_root) / backup_id
    problems: list[str] = []
    for name, entry in sorted(manifest.get("profiles", {}).items()):
        dest = base / name
        if not dest.is_dir():
            problems.append(f"{name}: profile folder missing from backup")
            continue
        config = dest / CONFIG_FILE
        actual_config = _file_sha256(config) if config.is_file() else None
        if actual_config != entry.get("config_sha256"):
            problems.append(f"{name}: config.yaml does not match manifest")
        if tree_sha256(dest / SKILLS) != entry.get("skills_tree_sha256"):
            problems.append(f"{name}: skills tree does not match manifest")
        elif count_files(dest / SKILLS) != entry.get("skills_files"):
            problems.append(f"{name}: skills file count does not match manifest")
    return problems
