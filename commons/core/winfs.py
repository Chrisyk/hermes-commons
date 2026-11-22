"""Filesystem and process helpers that behave the same on POSIX and Windows.

- fsync_path: Windows cannot open a folder for fsync and cannot fsync a read-only handle.
- is_link / read_link: a Windows directory junction is not is_symlink(), so test the reparse tag too.
- make_dir_link: a real symlink on POSIX; a junction on Windows (no admin or Developer Mode needed).
- kill_tree: kill a process and its children (killpg on POSIX, taskkill /T /F on Windows).
- relative_anchor: a path made relative to its root, also for drive paths like C:\\x\\y.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
O_BINARY = getattr(os, "O_BINARY", 0)
_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def fsync_path(path: Path) -> None:
    """fsync a file, or a directory where the OS allows it (a no-op for directories on Windows)."""
    if IS_WINDOWS:
        if os.path.isdir(path):
            return
        fd = os.open(path, os.O_RDWR | O_BINARY)
    else:
        fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def is_junction(path: Path) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) == _MOUNT_POINT


def is_link(path: Path) -> bool:
    """True for a symlink, and on Windows also for a directory junction."""
    return Path(path).is_symlink() or is_junction(path)


def clean_link_text(text: str) -> str:
    """Strip the \\\\?\\ (or \\??\\) prefix Windows puts in front of an absolute link target."""
    if IS_WINDOWS:
        for prefix in ("\\\\?\\UNC\\", "\\\\?\\", "\\??\\"):
            if text.startswith(prefix):
                return ("\\\\" + text[len(prefix) :]) if prefix.endswith("UNC\\") else text[len(prefix) :]
    return text


def read_link(path: Path) -> str:
    return clean_link_text(os.readlink(path))


def make_dir_link(link: Path, target: Path) -> None:
    """Create *link* pointing at the directory *target* (symlink, or junction on Windows)."""
    if not IS_WINDOWS:
        os.symlink(target, link, target_is_directory=True)
        return
    try:
        import _winapi

        _winapi.CreateJunction(str(Path(target).absolute()), str(link))
    except (ImportError, AttributeError, OSError):
        os.symlink(target, link, target_is_directory=True)


def remove_link(path: Path) -> None:
    """Remove a symlink or junction itself, never its target."""
    if IS_WINDOWS and is_junction(path):
        os.rmdir(path)
    else:
        os.unlink(path)


def replace_retry(src: Path, dst: Path, attempts: int = 20, delay: float = 0.05) -> None:
    """os.replace, retried while another process holds either file open (Windows WinError 5/32)."""
    for n in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS or n == attempts - 1:
                raise
            time.sleep(delay)


def popen_group_kwargs() -> dict:
    """Popen arguments that put the child in its own process group / session."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill *proc* and everything it started."""
    if IS_WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        with contextlib.suppress(OSError):
            proc.kill()
        return
    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def relative_anchor(path: Path) -> Path:
    """*path* without its root, so it can be nested under another folder: /a/b -> a/b, C:\\a\\b -> C/a/b."""
    p = Path(path)
    if p.drive:
        drive = p.drive.replace(":", "").replace("\\", "_").replace("/", "_").strip("_") or "drive"
        return Path(drive, *p.parts[1:])
    return Path(*p.parts[1:]) if p.is_absolute() else p
