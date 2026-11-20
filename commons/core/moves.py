"""Move-not-delete: retire a skill folder into a backup dir instead of deleting it.

A symlinked skill is retired as the link itself; its target is never touched.
Within one filesystem this is a single os.rename. Across filesystems the
folder is copied (symlinks kept) to a staging name next to the target, the
copy is verified by tree hash, renamed into place, and only then is the
source removed. Stdlib only.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path, PurePosixPath

from commons.core.backup import tree_sha256

RETIRED = "retired"


def _safe_parts(value: str, what: str) -> tuple[str, ...]:
    pure = PurePosixPath(value)
    parts = pure.parts
    if not value or pure.is_absolute() or not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"bad {what} {value!r}: must be a relative path inside the backup")
    return parts


def retired_path(backup_dir: Path, profile: str, rel: str) -> Path:
    if len(_safe_parts(profile, "profile")) != 1:
        raise ValueError(f"bad profile {profile!r}")
    return Path(backup_dir, RETIRED, profile, *_safe_parts(rel, "rel"))


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _copy_then_remove(src: Path, dest: Path) -> None:
    staging = dest.with_name(f".{dest.name}.retiring-{os.getpid()}")
    if _exists(staging):
        raise FileExistsError(f"staging path {staging} already exists")
    try:
        if src.is_symlink():
            staging.symlink_to(os.readlink(src))
            ok = os.readlink(staging) == os.readlink(src)
        else:
            shutil.copytree(src, staging, symlinks=True)
            ok = tree_sha256(staging) == tree_sha256(src)
        if not ok:
            raise OSError(errno.EIO, f"copy of {src} did not verify; source left in place")
        if _exists(dest):
            raise FileExistsError(f"{dest} appeared while retiring {src}")
        os.rename(staging, dest)
    except BaseException:
        # Only our own staging copy is removed; the source is untouched.
        if staging.is_symlink() or staging.is_file():
            staging.unlink()
        elif staging.is_dir():
            shutil.rmtree(staging)
        raise
    if src.is_symlink():
        src.unlink()
    else:
        shutil.rmtree(src)


def retire(path: Path, backup_dir: Path, profile: str, rel: str) -> Path:
    """Move path to backup_dir/retired/<profile>/<rel> and return the new location.

    Raises FileExistsError if the target exists (nothing is overwritten),
    FileNotFoundError if path is missing, ValueError if profile or rel would
    escape the backup dir.
    """
    src = Path(path)
    dest = retired_path(Path(backup_dir), profile, rel)
    if not _exists(src):
        raise FileNotFoundError(f"nothing to retire at {src}")
    if _exists(dest):
        raise FileExistsError(f"{dest} already exists; refusing to overwrite")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        # os.rename on a symlink renames the link, never its target. It would
        # silently replace an empty dir or a file at dest, hence the check above
        # (callers hold the repo lock, so nothing else races for the target).
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_then_remove(src, dest)
    return dest
