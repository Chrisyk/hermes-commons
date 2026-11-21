"""The stale-write guard's memory and rules, with no Hermes in it.

The guard remembers, per (session key, real file path), one version of the
file: the hash of what this session last read, or of what its own last
confirmed write produced. ABSENT is a version too (the file did not exist).

  base     the last version this session read or confirmed writing
  pending  what the session's last allowed write will produce, until
           post_tool_call confirms it (then it becomes base) or reports a
           failure (then it is dropped). None in pending means "unknown":
           the next full write must re-read.

The expected version is pending when one is outstanding, else base. Only
one version is kept, never a set: a set would let a session overwrite an
`undo` back to a version it once saw.

A read made in the same model response as the write (equal, non-empty
api_request_id) does not count: the model could not have seen its result
when it wrote the call.

Rules (decide):
  full write      patch with content, edit, write_file, remove_file, delete
                  (delete compares SKILL.md): allowed only when the file on
                  disk equals the expected version. With no record, only a
                  write to a file that does not exist is allowed.
  targeted patch  allowed when old_string occurs exactly in the file;
                  otherwise only when the file equals the expected version
                  (Hermes' fuzzy matcher could otherwise paste a stale patch
                  over another agent's new text).

Stdlib only.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path

ABSENT = "absent"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 4096
UNKNOWN = None


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def read_version(path: Path) -> tuple[str, bytes | None] | None:
    """(hash or ABSENT, bytes) of the file now; None when it is too big or unreadable (the guard steps aside)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return ABSENT, None
    except OSError:
        return None
    if size > MAX_FILE_BYTES or not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ABSENT, None
    except OSError:
        return None
    return digest_bytes(data), data


@dataclass(frozen=True)
class Record:
    version: str
    api_request_id: str = ""


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""


class Memory:
    """Thread-safe base/pending maps, LRU-capped."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._base: OrderedDict[Hashable, Record] = OrderedDict()
        self._pending: OrderedDict[Hashable, str | None] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_entries

    def _trim(self, d: OrderedDict) -> None:
        while len(d) > self._max:
            d.popitem(last=False)

    def saw(self, key: Hashable, version: str, api_request_id: str = "") -> None:
        """A read: it replaces base and clears any pending write."""
        with self._lock:
            self._base[key] = Record(version, api_request_id or "")
            self._base.move_to_end(key)
            self._pending.pop(key, None)
            self._trim(self._base)

    def forget(self, key: Hashable) -> None:
        with self._lock:
            self._base.pop(key, None)
            self._pending.pop(key, None)

    def forget_if(self, key: Hashable, version: str) -> None:
        """Drop base when it still holds *version* (a read whose file changed during the read)."""
        with self._lock:
            rec = self._base.get(key)
            if rec is not None and rec.version == version:
                self._base.pop(key, None)

    def will_write(self, key: Hashable, predicted: str | None) -> None:
        with self._lock:
            self._pending[key] = predicted
            self._pending.move_to_end(key)
            self._trim(self._pending)

    def wrote(self, key: Hashable, ok: bool) -> None:
        """post_tool_call: promote pending to base on success, drop it on failure."""
        with self._lock:
            if key not in self._pending:
                return
            predicted = self._pending.pop(key)
            if not ok:
                return
            if predicted is UNKNOWN:
                self._base.pop(key, None)
            else:
                self._base[key] = Record(predicted)
                self._base.move_to_end(key)
                self._trim(self._base)

    def expected(self, key: Hashable, api_request_id: str = "") -> tuple[bool, str | None]:
        """(known, version): the version this session expects on disk, or known False."""
        with self._lock:
            if key in self._pending:
                predicted = self._pending[key]
                return (predicted is not UNKNOWN), predicted
            rec = self._base.get(key)
        if rec is None:
            return False, None
        if api_request_id and rec.api_request_id and rec.api_request_id == api_request_id:
            return False, None  # read in this same response: the model never saw it
        return True, rec.version


def decide_full_write(memory: Memory, key: Hashable, disk: str, api_request_id: str = "") -> Verdict:
    known, version = memory.expected(key, api_request_id)
    if known:
        if version == disk:
            return Verdict(True)
        if disk == ABSENT:
            return Verdict(False, "it was removed since this session read it")
        return Verdict(False, "it changed since this session last read it")
    if disk == ABSENT:
        return Verdict(True)
    return Verdict(False, "this session has not read it in an earlier step")


def decide_patch(
    memory: Memory, key: Hashable, disk: str, text: str | None, old_string: str, api_request_id: str = ""
) -> Verdict:
    if text is not None and old_string and old_string in text:
        return Verdict(True)
    known, version = memory.expected(key, api_request_id)
    if known and version == disk:
        return Verdict(True)
    if disk == ABSENT:
        return Verdict(False, "it does not exist any more")
    return Verdict(
        False,
        "old_string does not occur exactly and the file changed since this session last read it",
    )


def predicted_patch(text: str | None, old: str, new: str, replace_all: bool) -> str | None:
    """Hash of the file after an exact patch, or None when it cannot be predicted."""
    if text is None or not old or text.count(old) == 0:
        return None
    if not replace_all and text.count(old) != 1:
        return None
    return digest_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))


def block_message(path: Path, reason: str) -> str:
    return (
        f"commons: nothing was written. {path}: {reason}. Another agent may have edited it."
        f" In a separate step, read it again (skill_view with this skill and file, or read_file {path}),"
        " then re-apply your change, preferably as a patch whose old_string is copied exactly from the"
        " new text. Do not resend this call unchanged."
    )
