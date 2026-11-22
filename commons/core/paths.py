"""Find and rewrite stale skill paths after skills move into the shared root.

A cron job prompt or a skill's markdown may name a skill file by an absolute
path (/home/u/.hermes/skills/general/x/SKILL.md) or a ~ path
(~/.hermes/skills/general/x/SKILL.md). Once setup retires that copy the path
is dead; find_stale_paths lists every such mention with the matching path in
the shared root, and rewrite() replaces only those whose new file exists.

Cron store: Hermes keeps one jobs.json per profile at <HERMES_HOME>/cron/
jobs.json (cron/jobs.py JOBS_FILE and _CronStorePaths.for_dir, ~line 95 and
~131), written as {"jobs": [...], "updated_at": iso} with json.dump(indent=2,
ensure_ascii=False) (cron/jobs.py _stage_jobs_payload ~1556). load_jobs
(~1377) also accepts a bare list. Each job is a dict whose only free text
read by the agent is "prompt" (create_job ~1899; cron/scheduler_prompt.py
builds the run prompt from job["prompt"] ~324). "skills"/"skill" hold skill
names, not paths, and "script" is relative to <HERMES_HOME>/scripts, so
neither can point at a skill folder. Writers hold an flock on
<cron dir>/.jobs.lock (cron/jobs.py _jobs_lock ~294); rewrite() takes the
same lock. Stdlib only.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from commons.core import filelock, winfs
from commons.core.fleet import Profile

PROMPT_FIELDS = ("prompt",)
CRON_DIR = "cron"
JOBS_FILE = "jobs.json"
JOBS_LOCK = ".jobs.lock"
REWRITES = "rewrites"
_LOCK_TIMEOUT = 30.0

# A path component character; a match must not touch one on either side.
_PATH_CHAR = r"[\w.~/-]"
_TERM = r"""[\s'"`<>()\[\]{}|,;]"""
_SUFFIX = r"""(?P<suffix>/[^\s'"`<>()\[\]{}|,;]*)?"""
# After a path: optional sentence punctuation, then a terminator or the end.
_END = rf"(?=[.:!?]*(?:$|{_TERM}))"
_TRAIL = ".:!?"

Kind = Literal["cron", "skill-file"]


class CronStoreError(ValueError):
    """A jobs.json commons refuses to rewrite."""


@dataclass(frozen=True)
class Finding:
    kind: Kind
    location: str
    old: str
    new: str
    new_exists: bool
    profile: str | None = None  # cron only
    job_id: str | None = None  # cron only
    field: str | None = None  # cron only
    path: Path | None = None  # skill-file only: the markdown file


@dataclass(frozen=True)
class PlannedWrite:
    """One file rewrite(), told to a WriteHook before and after it lands."""

    kind: Kind
    path: Path
    backup: Path  # the original, kept before the write
    sha_before: str | None
    sha_after: str
    profile: str | None = None  # cron only


class WriteHook(Protocol):
    def before(self, write: PlannedWrite) -> None: ...

    def after(self, write: PlannedWrite) -> None: ...


@dataclass(frozen=True)
class _Form:
    text: str  # the old path as it may appear in text
    old: Path
    new: Path
    tilde: bool


def _tilde(path: Path, home: Path) -> str | None:
    try:
        rel = path.relative_to(home)
    except ValueError:
        return None
    return "~/" + rel.as_posix() if rel.parts else "~"


def _forms(moved: Mapping[Path, Path], home: Path) -> list[_Form]:
    forms: dict[str, _Form] = {}
    for old, new in moved.items():
        old, new = Path(old), Path(new)
        variants = {old}
        with contextlib.suppress(OSError):
            variants.add(old.resolve())
        for v in variants:
            forms.setdefault(str(v), _Form(str(v), old, new, False))
            t = _tilde(v, home)
            if t is not None:
                forms.setdefault(t, _Form(t, old, new, True))
    # Longest first, so a moved folder inside another moved folder wins.
    return sorted(forms.values(), key=lambda f: len(f.text), reverse=True)


def _display(path: Path, tilde: bool, home: Path) -> str:
    if tilde:
        t = _tilde(path, home)
        if t is not None:
            return t
    return str(path)


def _matches(text: str, forms: list[_Form], home: Path) -> Iterator[tuple[str, str, Path]]:
    """(old text, new text, new absolute path) for each stale path in *text*, in order."""
    if not forms:
        return
    alts = "|".join(re.escape(f.text) for f in forms)
    by_text = {f.text: f for f in forms}
    pattern = re.compile(rf"(?<!{_PATH_CHAR})(?P<base>{alts}){_SUFFIX}{_END}")
    for m in pattern.finditer(text):
        form = by_text[m.group("base")]
        suffix = (m.group("suffix") or "").rstrip(_TRAIL)
        new_abs = Path(str(form.new) + suffix)
        yield form.text + suffix, _display(form.new, form.tilde, home) + suffix, new_abs


def _exact(old: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!{_PATH_CHAR}){re.escape(old)}{_END}")


def _iter_markdown(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda exc: None, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", ".commons"))
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield Path(dirpath) / name


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def find_stale_paths(
    profiles: Iterable[Profile],
    shared_root: Path,
    moved: Mapping[Path, Path],
    cron_jobs: Mapping[str, list[dict]],
    home: Path | None = None,
    exists: Callable[[Path], bool] | None = None,
    include_files: bool = True,
) -> list[Finding]:
    """Every mention of a moved path in cron prompts and in *.md files of the shared root.

    *moved* maps each old skill folder to its new folder. Mentions match on
    path component boundaries (/a/skills/foo never matches inside
    /a/skills/foobar), in absolute or ~ form (relative to *home*, default
    the user's home); the new path keeps the old one's form. *exists* decides
    new_exists (default Path.exists); a proposal passes one that looks in
    the source copy, since the target does not exist yet. Only cron jobs of
    the given *profiles* are read. .git and .commons are skipped;
    include_files=False reads cron prompts only.
    """
    home = Path(home) if home is not None else Path.home()
    exists = exists or (lambda p: p.exists())
    forms = _forms(moved, home)
    names = {p.name for p in profiles}
    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(finding: Finding) -> None:
        key = (finding.location, finding.old)
        if key not in seen:
            seen.add(key)
            out.append(finding)

    for profile in sorted(cron_jobs):
        if profile not in names:
            continue
        keys = job_keys(cron_jobs[profile])
        for index, job in enumerate(cron_jobs[profile]):
            if not isinstance(job, dict):
                continue
            job_id = keys[index]
            for field in PROMPT_FIELDS:
                value = job.get(field)
                if not isinstance(value, str):
                    continue
                for old, new, new_abs in _matches(value, forms, home):
                    location = f"{profile} cron job {job_id} {field}"
                    add(Finding("cron", location, old, new, exists(new_abs), profile, job_id, field))
    if include_files and Path(shared_root).is_dir():
        for md in _iter_markdown(Path(shared_root)):
            text = _read_text(md)
            if text is None:
                continue
            for old, new, new_abs in _matches(text, forms, home):
                add(Finding("skill-file", str(md), old, new, exists(new_abs), path=md))
    return out


def job_keys(jobs: list) -> list[str]:
    """A key per job: its id when that is a non-empty string or int unique in the list, else '#<index>'.

    Jobs without a usable id are named by position; rewrite() re-checks that
    the job at that position still has no usable id, and skips it otherwise
    (the list changed: which job is meant is ambiguous).
    """
    ids = [
        str(j["id"]) if isinstance(j, dict) and isinstance(j.get("id"), (str, int)) and str(j["id"]) else None
        for j in jobs
    ]
    counts: dict[str, int] = {}
    for i in ids:
        if i is not None:
            counts[i] = counts.get(i, 0) + 1
    return [
        i if i is not None and counts[i] == 1 and not i.startswith("#") else f"#{n}"
        for n, i in enumerate(ids)
    ]


def _fsync_dir(path: Path) -> None:
    winfs.fsync_path(path)


def write_atomic(path: Path, data: bytes) -> None:
    """Replace *path* (through a symlink) with *data*: tmp + fsync + same mode + rename + dir fsync."""
    target = Path(path).resolve()
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        winfs.replace_retry(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(target.parent)


@contextmanager
def cron_lock(jobs_path: Path, timeout: float = _LOCK_TIMEOUT) -> Iterator[None]:
    """Hold Hermes' cross-process cron lock, <cron dir>/.jobs.lock (cron/jobs.py _jobs_lock)."""
    lock = Path(jobs_path).parent / JOBS_LOCK
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | filelock.O_BINARY, 0o600)
    try:
        deadline = time.monotonic() + timeout
        # Offset 0: Hermes' own cron lock takes byte 0 on Windows, so ours must match.
        while not filelock.try_lock(fd, 0):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"could not lock {lock} within {timeout} s") from None
            time.sleep(0.05)
        try:
            yield
        finally:
            filelock.unlock(fd, 0)
    finally:
        os.close(fd)


def load_jobs_file(path: Path) -> tuple[object, list[dict]]:
    """Parse jobs.json as Hermes _parse_jobs_file does; return (top-level data, job dicts).

    utf-8-sig absorbs a BOM and a strict JSON failure retries with
    strict=False. The canonical shape is {"jobs": [...]}; a bare list is
    accepted. A missing file is ([], []).
    """
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return [], []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(raw, strict=False)
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise CronStoreError(f"{path}: 'jobs' is not a list; refusing to rewrite it")
    return data, [j for j in jobs if isinstance(j, dict)]


def rewrite_backup_path(backup_dir: Path, finding: Finding) -> Path:
    """Where rewrite() keeps the original of the file *finding* lives in."""
    if finding.kind == "cron":
        return Path(backup_dir) / REWRITES / "cron" / str(finding.profile) / JOBS_FILE
    assert finding.path is not None
    return Path(backup_dir) / REWRITES / "files" / winfs.relative_anchor(Path(finding.path).resolve())


def _backup_once(src: Path, dest: Path) -> None:
    """Copy src to dest unless an earlier pass already kept the original there."""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    with open(dest, "rb") as fh:
        os.fsync(fh.fileno())
    _fsync_dir(dest.parent)


def _replace_all(text: str, findings: list[Finding]) -> tuple[str, list[Finding], list[Finding]]:
    done, missing = [], []
    for f in findings:
        text, n = _exact(f.old).subn(lambda _m, new=f.new: new, text)
        (done if n else missing).append(f)
    return text, done, missing


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


# The WriteHook of the rewrite() call in progress (a context variable, so the
# per-file helpers keep their signatures).
_HOOK: contextvars.ContextVar[WriteHook | None] = contextvars.ContextVar("commons_write_hook", default=None)


def _write(write: PlannedWrite, data: bytes) -> None:
    """Back the file up, tell the hook, write it, tell the hook again."""
    hook = _HOOK.get()
    _backup_once(write.path, write.backup)
    if hook is not None:
        hook.before(write)
    write_atomic(write.path, data)
    if hook is not None:
        hook.after(write)


def _rewrite_cron(path: Path, findings: list[Finding], backup: Path) -> tuple[list[Finding], list[Finding]]:
    with cron_lock(path):
        raw = _read_bytes(path)
        data, jobs = load_jobs_file(path)
        by_job: dict[tuple[str, str], list[Finding]] = {}
        for f in findings:
            by_job.setdefault((str(f.job_id), str(f.field)), []).append(f)
        done: list[Finding] = []
        missing: list[Finding] = []
        for job, key in zip(jobs, job_keys(jobs), strict=True):
            if not isinstance(job, dict):
                continue
            for field in PROMPT_FIELDS:
                todo = by_job.pop((key, field), [])
                if todo and isinstance(job.get(field), str):
                    job[field], d, m = _replace_all(job[field], todo)
                    done += d
                    missing += m
                else:
                    missing += todo
        for left in by_job.values():
            missing += left
        if done and raw is not None:
            new = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
            profile = done[0].profile
            _write(PlannedWrite("cron", Path(path), backup, _sha(raw), _sha(new), profile), new)
    return done, missing


def _rewrite_file(path: Path, findings: list[Finding], backup: Path) -> tuple[list[Finding], list[Finding]]:
    raw = _read_bytes(path)
    text = _read_text(path) if raw is not None else None
    if text is None or raw is None:
        return [], findings
    new_text, done, missing = _replace_all(text, findings)
    if done:
        new = new_text.encode("utf-8")
        _write(PlannedWrite("skill-file", Path(path), backup, _sha(raw), _sha(new)), new)
    return done, missing


def rewrite(
    findings: Iterable[Finding],
    backup_dir: Path,
    cron_paths: Mapping[str, Path],
    hook: WriteHook | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Apply every finding whose new path exists; return (rewritten, skipped).

    Each touched file is first copied to rewrite_backup_path (kept from the
    first pass if one exists), then rewritten atomically with fsync. Cron
    findings go to cron_paths[profile] under Hermes' cron lock. *hook*, if
    given, hears of every write just before and just after it (still under
    the cron lock), so a caller can journal it ahead. Skipped: findings
    whose target is missing, whose profile has no cron path, or whose text
    is no longer in the file.
    """
    findings = list(findings)
    skipped = [f for f in findings if not f.new_exists]
    files: dict[tuple[str, Path], list[Finding]] = {}
    for f in findings:
        if not f.new_exists:
            continue
        if f.kind == "cron":
            if f.profile not in cron_paths:
                skipped.append(f)
                continue
            key = ("cron", Path(cron_paths[f.profile]))
        else:
            assert f.path is not None
            key = ("skill-file", Path(f.path))
        files.setdefault(key, []).append(f)
    rewritten: list[Finding] = []
    token = _HOOK.set(hook)
    try:
        _rewrite_groups(files, backup_dir, rewritten, skipped)
    finally:
        _HOOK.reset(token)
    return rewritten, skipped


def _rewrite_groups(
    files: Mapping[tuple[str, Path], list[Finding]],
    backup_dir: Path,
    rewritten: list[Finding],
    skipped: list[Finding],
) -> None:
    for (kind, path), group in files.items():
        backup = rewrite_backup_path(backup_dir, group[0])
        try:
            if kind == "cron":
                done, missing = _rewrite_cron(path, group, backup)
            else:
                done, missing = _rewrite_file(path, group, backup)
        except FileNotFoundError as exc:
            if exc.errno != errno.ENOENT:
                raise
            done, missing = [], group
        rewritten += done
        skipped += missing
