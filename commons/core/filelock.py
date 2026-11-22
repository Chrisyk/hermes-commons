"""One place for cross-process file locks: fcntl.flock on POSIX, msvcrt.locking on Windows.

Only the non-blocking try is exposed; callers poll against their own deadline.

Windows differences handled here:
- msvcrt.locking locks a byte range from the current file position and is mandatory
  (other handles cannot read or write the locked bytes). So every call seeks to *offset*
  first, and the file position is restored afterwards.
- A busy lock raises PermissionError (not BlockingIOError); try_lock maps it to False.
- There is no shared mode; commons only ever takes exclusive locks.

Interop: Hermes' own cron lock (cron/jobs.py) locks 1 byte at offset 0 of <cron dir>/.jobs.lock
on Windows, so commons must use offset 0 there. Files that carry readable text (the repo lock's
holder note) lock a far-away byte instead, so waiters can still read the text.
"""

from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"

# Byte used for locks on files whose first bytes must stay readable.
HIGH_OFFSET = 0x40000000

# O_BINARY exists only on Windows; without it os.write would translate "\n".
O_BINARY = getattr(os, "O_BINARY", 0)

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def try_lock(fd: int, offset: int = 0) -> bool:
    """Take an exclusive lock without waiting. True if taken, False if someone else holds it."""
    if IS_WINDOWS:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except PermissionError:
                return False
            except OSError as exc:
                if exc.errno in (13, 36):  # EACCES, EDEADLOCK: held by another handle
                    return False
                raise
            return True
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def unlock(fd: int, offset: int = 0) -> None:
    """Release a lock taken by try_lock with the same offset."""
    if IS_WINDOWS:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def write_at_start(fd: int, data: bytes) -> None:
    """Replace the file's contents with *data* (portable stand-in for ftruncate + pwrite)."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)


def read_at_start(fd: int, n: int = 4096) -> bytes:
    """Read up to *n* bytes from the start of the file (portable stand-in for pread)."""
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, n)
