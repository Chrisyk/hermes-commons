"""The consistency settings, and the switch that shares Hermes' per-skill lock across profiles.

Two settings, both stored in <state dir>/consistency.json, outside the
shared root so neither an agent editing skills nor a git checkout there can
change them. Only `hermes commons config set` writes the file.

  lock   own | shared            missing = own
  guard  off | warn | strict     missing = off
  guard_overrides {profile: level}   the guard level for one profile

A missing file means lock own and guard off: exactly what commons 0.1 did,
so an upgrade changes nothing.

lock. Hermes serializes every skill_manage write with an flock on
<profile>/skills/.locks/<sha256(basename)>.lock (tools/skill_manager_tool.py
_skill_lock_path, v0.23.1), held for the whole read-modify-write. The
directory is the ACTIVE profile's, not the folder the skill lives in, so two
profiles writing the same shared skill take different locks and the later
rename can drop the earlier edit. With lock shared, every profile's .locks is
a symlink to <state dir>/skill-locks/shared, so Hermes' own lock covers all
of them; nothing here holds a lock during a turn. With lock own after an
opt-in, .locks links to <state dir>/skill-locks/<profile>. Once a profile's
.locks is a symlink, every later switch is one atomic rename of a new link
over the old one, so Hermes (which mkdirs .locks on demand) never sees it
missing. The first switch of a real directory has a gap; it is retried if
Hermes recreates the directory inside it.

A switch first tries a non-blocking flock on every lock file the profiles
use now and aborts if any is held. A writer that opened its lock file before
the switch keeps that inode, so one write in flight can still overlap one
write that starts on the new link; the docs say so. Moved directories go
into <state dir>/backups/consistency-<stamp>/ (nothing is deleted).

Stdlib only. Linux and macOS (fcntl).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commons.core.fleet import Profile

SETTINGS_FILE = "consistency.json"
LOCKS_DIR = "skill-locks"
SHARED_LOCKS = "shared"
HERMES_LOCKS = ".locks"
LOCK_LEVELS = ("own", "shared")
GUARD_LEVELS = ("off", "warn", "strict")
DEFAULT_LOCK = "own"
DEFAULT_GUARD = "off"
SWITCH_RETRIES = 5


class ConsistencyError(RuntimeError):
    """A setting or a lock switch was refused; nothing was changed."""


@dataclass(frozen=True)
class Settings:
    lock: str = DEFAULT_LOCK
    guard: str = DEFAULT_GUARD
    guard_overrides: Mapping[str, str] = field(default_factory=dict)
    exists: bool = False
    problems: tuple[str, ...] = ()

    def guard_for(self, profile: str) -> str:
        return self.guard_overrides.get(profile, self.guard)

    def guard_anywhere(self) -> bool:
        """True when some profile runs the guard (warn or strict), so its tool hooks must be registered."""
        return self.guard != "off" or any(v != "off" for v in self.guard_overrides.values())

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"lock": self.lock, "guard": self.guard}
        if self.guard_overrides:
            data["guard_overrides"] = dict(sorted(self.guard_overrides.items()))
        return data


def settings_path(state_dir: Path) -> Path:
    return Path(state_dir) / SETTINGS_FILE


def parse_settings(data: Any) -> Settings:
    """Settings from decoded JSON; an unknown value falls back to its default and is named in problems."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return Settings(exists=True, problems=("consistency.json is not a JSON object",))
    lock = data.get("lock", DEFAULT_LOCK)
    if lock not in LOCK_LEVELS:
        problems.append(f"lock {lock!r} is not one of {', '.join(LOCK_LEVELS)}; using {DEFAULT_LOCK}")
        lock = DEFAULT_LOCK
    guard = data.get("guard", DEFAULT_GUARD)
    if guard not in GUARD_LEVELS:
        problems.append(f"guard {guard!r} is not one of {', '.join(GUARD_LEVELS)}; using {DEFAULT_GUARD}")
        guard = DEFAULT_GUARD
    overrides: dict[str, str] = {}
    raw = data.get("guard_overrides", {})
    if not isinstance(raw, dict):
        problems.append("guard_overrides is not an object; ignored")
        raw = {}
    for profile, level in raw.items():
        if level in GUARD_LEVELS and isinstance(profile, str):
            overrides[profile] = level
        else:
            problems.append(f"guard override {profile!r}: {level!r} is not one of {', '.join(GUARD_LEVELS)}")
    return Settings(lock, guard, overrides, exists=True, problems=tuple(problems))


def read_settings(state_dir: Path) -> Settings:
    """The settings; defaults (lock own, guard off) when the file is missing.

    An unreadable or invalid file also gives the defaults, with the reason in
    problems (check reports it). Callers that must keep the last good value
    on a read error (the guard) use read_settings_strict.
    """
    try:
        return read_settings_strict(state_dir)
    except (OSError, ValueError) as exc:
        return Settings(exists=True, problems=(f"cannot read {settings_path(state_dir)}: {exc}",))


def read_settings_strict(state_dir: Path) -> Settings:
    """Like read_settings, but raises OSError or ValueError when the file exists and cannot be read."""
    path = settings_path(state_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    return parse_settings(json.loads(text))


def write_settings(state_dir: Path, settings: Settings) -> Path:
    """Write the settings atomically (temp file, fsync, rename)."""
    path = settings_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{SETTINGS_FILE}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(settings.to_json(), indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def file_signature(path: Path) -> tuple[int, int, int, int] | None:
    """(mtime_ns, size, inode, ctime_ns), as Hermes utils.file_signature; None when missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


# ------------------------------------------------------------------ lock links


def locks_root(state_dir: Path) -> Path:
    return Path(state_dir) / LOCKS_DIR


def lock_target(state_dir: Path, profile: str, lock: str) -> Path:
    """Where profile's .locks should point for this lock level."""
    return locks_root(state_dir) / (SHARED_LOCKS if lock == "shared" else profile)


def hermes_locks(profile: Profile) -> Path:
    return Path(profile.home) / "skills" / HERMES_LOCKS


@dataclass(frozen=True)
class LinkState:
    profile: str
    path: Path
    kind: str  # "missing" | "dir" | "link" | "other"
    target: Path | None = None  # the resolved link target for kind "link"

    def points_to(self, want: Path) -> bool:
        return self.kind == "link" and self.target is not None and _same(self.target, want)


def _same(a: Path, b: Path) -> bool:
    with contextlib.suppress(OSError):
        return Path(a).resolve() == Path(b).resolve()
    return Path(a).absolute() == Path(b).absolute()


def link_state(profile: Profile) -> LinkState:
    path = hermes_locks(profile)
    if path.is_symlink():
        raw = Path(os.readlink(path))
        target = raw if raw.is_absolute() else path.parent / raw
        return LinkState(profile.name, path, "link", target)
    if not path.exists():
        return LinkState(profile.name, path, "missing")
    if path.is_dir():
        return LinkState(profile.name, path, "dir")
    return LinkState(profile.name, path, "other")


def expected_kind_ok(state: LinkState, state_dir: Path, lock: str) -> bool:
    """True when this profile's .locks matches the lock level.

    lock own is also satisfied by a real directory or no .locks at all: that
    is Hermes' own default, what a profile that never opted in has.
    """
    want = lock_target(state_dir, state.profile, lock)
    if lock == "shared":
        return state.points_to(want)
    if state.kind in ("dir", "missing"):
        return True
    return state.points_to(want)


def busy_locks(dirs: Iterable[Path]) -> list[Path]:
    """Lock files in these folders that some process holds right now (non-blocking flock probe)."""
    held: list[Path] = []
    for d in dirs:
        with contextlib.suppress(OSError):
            for entry in sorted(Path(d).iterdir()):
                if not entry.is_file() or entry.is_symlink():
                    continue
                try:
                    fd = os.open(entry, os.O_RDONLY)
                except OSError:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(entry)
                except OSError:
                    pass
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
    return held


def _stamp(now: datetime | None) -> str:
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _free(path: Path) -> Path:
    if not os.path.lexists(path):
        return path
    n = 2
    while os.path.lexists(path.with_name(f"{path.name}-{n}")):
        n += 1
    return path.with_name(f"{path.name}-{n}")


def _set_aside(path: Path, backup: Path, profile: str) -> Path:
    """Move a real .locks directory (or stray file) into the backup; never deletes."""
    backup.mkdir(parents=True, exist_ok=True)
    dst = _free(backup / f"{profile}{HERMES_LOCKS}")
    shutil.move(str(path), str(dst))
    return dst


def _relink(path: Path, target: Path) -> None:
    """Point *path* (a symlink, or nothing) at *target* with one atomic rename."""
    tmp = path.with_name(f"{HERMES_LOCKS}.commons-new")
    with contextlib.suppress(FileNotFoundError):
        if tmp.is_symlink():
            tmp.unlink()  # our own leftover temp link from an interrupted switch, never a user file
    os.symlink(target, tmp, target_is_directory=True)
    os.replace(tmp, path)


@dataclass
class SwitchResult:
    linked: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    set_aside: list[Path] = field(default_factory=list)
    backup: Path | None = None


def switch_locks(
    profiles: Iterable[Profile],
    state_dir: Path,
    lock: str,
    now: datetime | None = None,
    after_set_aside=None,
) -> SwitchResult:
    """Point every profile's skills/.locks at the folder for *lock* (see module docstring).

    Raises ConsistencyError, with nothing changed, when a lock file in use
    now is held. *after_set_aside(path)* is a test seam called in the gap
    between moving a real directory away and linking.
    """
    if lock not in LOCK_LEVELS:
        raise ConsistencyError(f"lock must be one of {', '.join(LOCK_LEVELS)}, not {lock!r}")
    profiles = list(profiles)
    state_dir = Path(state_dir)
    result = SwitchResult()
    states = [link_state(p) for p in profiles]
    todo = [s for s in states if not s.points_to(lock_target(state_dir, s.profile, lock))]
    if lock == "own":
        # A real dir or no .locks already is Hermes' private lock: leave it, do not opt it in.
        todo = [s for s in todo if s.kind == "link"]
    for s in states:
        if s not in todo:
            result.unchanged.append(s.profile)
    if not todo:
        return result
    current = [s.target if s.kind == "link" else s.path for s in todo if s.kind != "missing"]
    if lock == "shared":
        current.append(lock_target(state_dir, "", "shared"))
    held = busy_locks(d for d in current if d is not None)
    if held:
        names = ", ".join(str(h) for h in held[:5])
        raise ConsistencyError(
            f"a skill write is in progress (lock held: {names}); nothing was changed, try again in a moment"
        )
    for s in todo:
        if s.kind == "other":
            raise ConsistencyError(f"{s.path} is neither a folder nor a symlink; move it away first")
    backup = Path(state_dir) / "backups" / f"consistency-{_stamp(now)}"
    for s in todo:
        target = lock_target(state_dir, s.profile, lock)
        target.mkdir(parents=True, exist_ok=True)
        path = s.path
        for _attempt in range(SWITCH_RETRIES):
            if path.is_symlink() or not os.path.lexists(path):
                try:
                    _relink(path, target)
                except IsADirectoryError:
                    continue  # Hermes recreated a real .locks in the gap: set it aside and retry
                except OSError as exc:
                    if not path.is_dir() or path.is_symlink():
                        raise ConsistencyError(f"could not link {path} to {target}: {exc}") from exc
                    continue
                break
            result.set_aside.append(_set_aside(path, backup, s.profile))
            result.backup = backup
            if after_set_aside is not None:
                after_set_aside(path)
        else:
            raise ConsistencyError(
                f"{path} kept being recreated while it was switched; run the command again"
                f" (the old folders are in {backup})"
            )
        result.linked.append(s.profile)
    return result


SETUP_RECORD = "consistency-setup.json"


def record_setup(state_dir: Path, backup_id: str, locks_backup: Path | None) -> None:
    """Remember that setup *backup_id* set the lock, so restoring that setup can undo it."""
    data = {"setup": backup_id, "locks_backup": str(locks_backup) if locks_backup else None}
    (Path(state_dir) / SETUP_RECORD).write_text(json.dumps(data) + "\n", encoding="utf-8")


def setup_record(state_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(state_dir) / SETUP_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def undo_setup_lock(profiles: Iterable[Profile], state_dir: Path, now: datetime | None = None) -> list[str]:
    """Put every profile's .locks back as it was before the setup that set the lock.

    A .locks link into <state dir>/skill-locks/ is removed (it is commons'
    own link) and the folder the setup moved away, if any, is moved back.
    consistency.json and the setup record move into a backup folder, so the
    settings return to the defaults. Raises ConsistencyError, changing
    nothing, when a lock file is held. Returns the profiles put back.
    """
    state_dir = Path(state_dir)
    record = setup_record(state_dir)
    if record is None:
        return []
    saved = Path(record["locks_backup"]) if record.get("locks_backup") else None
    states = [link_state(p) for p in profiles]
    ours = [
        s
        for s in states
        if s.kind == "link" and s.target is not None and _inside(s.target, locks_root(state_dir))
    ]
    held = busy_locks(s.target for s in ours if s.target is not None)
    if held:
        raise ConsistencyError(f"a skill write is in progress (lock held: {held[0]}); try the restore again")
    done = []
    for s in ours:
        os.unlink(s.path)  # commons' own symlink, never a user folder
        original = saved / f"{s.profile}{HERMES_LOCKS}" if saved else None
        if original is not None and original.is_dir():
            shutil.move(str(original), str(s.path))
        done.append(s.profile)
    backup = state_dir / "backups" / f"consistency-restored-{_stamp(now)}"
    backup.mkdir(parents=True, exist_ok=True)
    for name in (SETTINGS_FILE, SETUP_RECORD):
        src = state_dir / name
        if src.exists():
            shutil.move(str(src), str(backup / name))
    return done


def _inside(path: Path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except OSError:
        return False


def lock_findings(
    profiles: Iterable[Profile], state_dir: Path, settings: Settings
) -> list[tuple[str, str, str]]:
    """(code, profile, message) for every profile whose .locks does not match the lock setting."""
    out: list[tuple[str, str, str]] = []
    for p in profiles:
        s = link_state(p)
        if expected_kind_ok(s, state_dir, settings.lock):
            continue
        want = lock_target(state_dir, p.name, settings.lock)
        have = {"missing": "missing", "dir": "a private folder", "other": "not a folder"}.get(
            s.kind, f"a link to {s.target}"
        )
        if settings.lock == "shared":
            out.append(
                (
                    "locks-unshared",
                    p.name,
                    (
                        f"lock is shared but {s.path} is {have}, not a link to {want}; this profile's"
                        " skill writes do not wait for the others (run `hermes commons config set lock shared`)"
                    ),
                )
            )
        else:
            out.append(
                (
                    "locks-mismatch",
                    p.name,
                    (
                        f"lock is own but {s.path} is {have}, not a link to {want}"
                        " (run `hermes commons config set lock own`)"
                    ),
                )
            )
    return out
