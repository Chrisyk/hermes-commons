"""Move-not-delete: retire a skill folder into a backup dir instead of deleting it.

A symlinked skill is retired as the link itself; its target is never touched.
Within one filesystem this is a single rename that never replaces anything at
the target. Across filesystems the folder (or file) is copied, symlinks kept,
to a staging name next to the target, fsynced, verified by hash, renamed into
place, made durable, and only then is the source removed. Stdlib only.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from commons.core.backup import file_sha256, tree_sha256

RETIRED = "retired"
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
# renameat2 exists but this filesystem or kernel cannot honour the flag.
_NOREPLACE_UNSUPPORTED = {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}


class RetireError(OSError):
    """The copy landed at the target but the source could not be fully removed."""


def _load_renameat2() -> Callable[[Path, Path], None] | None:
    """libc renameat2(RENAME_NOREPLACE) via ctypes on Linux; None elsewhere or if libc lacks it."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int

    def renameat2(src: Path, dest: Path) -> None:
        if fn(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dest), _RENAME_NOREPLACE) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), str(src), None, str(dest))

    return renameat2


_renameat2 = _load_renameat2()


def _rename_noreplace(src: Path, dest: Path) -> None:
    """Rename src to dest, raising FileExistsError if anything (even an empty dir) is at dest.

    On Linux renameat2(RENAME_NOREPLACE) makes the check and the rename one
    atomic step. Elsewhere, or on a filesystem without the flag, it falls back
    to check then os.rename; callers hold the repo lock, so the only window is
    against writers that ignore that lock.
    """
    if _renameat2 is not None:
        try:
            _renameat2(src, dest)
            return
        except OSError as exc:
            if exc.errno not in _NOREPLACE_UNSUPPORTED:
                raise
    if _exists(dest):
        raise FileExistsError(errno.EEXIST, "target already exists; refusing to overwrite", str(dest))
    os.rename(src, dest)


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


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdirs_durable(path: Path) -> None:
    """mkdir -p, then fsync every created dir and the existing parent it was created in."""
    created: list[Path] = []
    p = path
    while not _exists(p):
        created.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True)
    for d in [*created, p]:
        _fsync_path(d)


def _fsync_tree(root: Path) -> None:
    """fsync every regular file under root, then every dir deepest first (symlinks via their dir)."""
    dirs = [root]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in filenames:
            p = here / name
            if p.is_file() and not p.is_symlink():
                _fsync_path(p)
        dirs += [here / d for d in dirnames if not (here / d).is_symlink()]
    for d in sorted(dirs, key=lambda q: len(q.parts), reverse=True):
        _fsync_path(d)


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _copy_then_remove(src: Path, dest: Path) -> None:
    staging = dest.with_name(f".{dest.name}.retiring-{os.getpid()}-{threading.get_ident()}")
    if _exists(staging):
        raise FileExistsError(f"staging path {staging} already exists")
    try:
        if src.is_symlink():
            staging.symlink_to(os.readlink(src))
            _fsync_path(dest.parent)
            ok = os.readlink(staging) == os.readlink(src)
        elif src.is_dir():
            shutil.copytree(src, staging, symlinks=True)
            _fsync_tree(staging)
            _fsync_path(dest.parent)
            ok = tree_sha256(staging) == tree_sha256(src)
        else:
            shutil.copy2(src, staging, follow_symlinks=False)
            _fsync_path(staging)
            _fsync_path(dest.parent)
            ok = file_sha256(staging) == file_sha256(src) and (staging.stat().st_mode == src.stat().st_mode)
        if not ok:
            raise OSError(errno.EIO, f"copy of {src} did not verify; source left in place")
        _rename_noreplace(staging, dest)
    except BaseException:
        # Only our own staging copy is removed; the source is untouched.
        if _exists(staging):
            _remove(staging)
        raise
    _fsync_path(dest.parent)
    # Window: from here until the removal below finishes, the skill exists at
    # both src and dest. A crash leaves a duplicate, never a loss, because the
    # copy was verified and made durable before any byte of src is touched.
    # An edit to src after verification but before removal is lost from the
    # live tree (it was never copied); callers hold the repo lock to keep
    # commons writers out, and Hermes edits during a retire are not expected.
    try:
        _remove(src)
    except OSError as exc:
        where = exc.filename or src
        raise RetireError(
            f"retired {src} but could not remove {where} ({exc.strerror or exc}); "
            f"the full copy is safe at {dest}"
        ) from exc
    _fsync_path(src.parent)


def move_noreplace(src: Path, dest: Path) -> Path:
    """Move src (folder, symlink or file) to dest, never replacing anything; return dest.

    Same guarantees as retire: FileExistsError if dest exists,
    FileNotFoundError if src is missing, a verified copy before any removal
    when the move crosses file systems, RetireError if that removal fails.
    """
    src, dest = Path(src), Path(dest)
    if not _exists(src):
        raise FileNotFoundError(f"nothing to move at {src}")
    if _exists(dest):
        raise FileExistsError(f"{dest} already exists; refusing to overwrite")
    _mkdirs_durable(dest.parent)
    try:
        # A rename moves a symlink itself, never its target.
        _rename_noreplace(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_then_remove(src, dest)
        return dest
    _fsync_path(dest.parent)
    _fsync_path(src.parent)
    return dest


def retire(path: Path, backup_dir: Path, profile: str, rel: str) -> Path:
    """Move path to backup_dir/retired/<profile>/<rel> and return the new location.

    path may be a skill folder, a symlink or a plain file. Raises
    FileExistsError if the target exists (nothing is overwritten, not even an
    empty dir), FileNotFoundError if path is missing, ValueError if profile or
    rel would escape the backup dir, and RetireError if a cross-filesystem
    move copied everything but could not finish removing the source.
    """
    src = Path(path)
    dest = retired_path(Path(backup_dir), profile, rel)
    if not _exists(src):
        raise FileNotFoundError(f"nothing to retire at {src}")
    return move_noreplace(src, dest)
