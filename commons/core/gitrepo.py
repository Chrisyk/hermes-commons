"""A small locked wrapper around local git for the shared skill library.

Every public operation holds an exclusive fcntl.flock on <path>/.commons/lock,
so concurrent Hermes processes (hooks, cron, setup) never interleave git
commands. Every git call disables hooks and commit signing, never prompts, and
falls back to the identity "commons <commons@localhost>" when the repo has no
user.name / user.email configured. Stdlib only. Linux and macOS.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = ".commons"
LOCK_NAME = "lock"
DEFAULT_LOCK_TIMEOUT = 10.0
FALLBACK_NAME = "commons"
FALLBACK_EMAIL = "commons@localhost"
_POLL_SECONDS = 0.05
_EXCLUDE_LINE = f"/{LOCK_DIR}/{LOCK_NAME}"

# Reentrancy bookkeeping: (lock path, thread id) -> [fd, depth]. flock locks
# belong to an open file description, so a second open() in the same process
# would block on itself; nested locked() calls reuse the first fd instead.
_held: dict[tuple[str, int], list[int]] = {}
_held_guard = threading.Lock()


class GitError(RuntimeError):
    def __init__(self, cmd: list[str], stderr: str, returncode: int | None = None):
        self.cmd = cmd
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"git failed ({returncode}): {' '.join(cmd)}\n{stderr.strip()}")


class LockTimeout(TimeoutError):
    pass


class GitRepo:
    def __init__(self, path: Path, lock_timeout: float = DEFAULT_LOCK_TIMEOUT):
        self.path = Path(path)
        self.lock_timeout = lock_timeout

    # Locking

    @property
    def lock_path(self) -> Path:
        return self.path / LOCK_DIR / LOCK_NAME

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the repo lock; reentrant within one thread of one process."""
        lock_path = self.lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        key = (str(lock_path.resolve()), threading.get_ident())
        with _held_guard:
            entry = _held.get(key)
            if entry is not None:
                entry[1] += 1
        if entry is not None:
            try:
                yield
            finally:
                with _held_guard:
                    entry[1] -= 1
            return
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            self._acquire(fd)
            with _held_guard:
                _held[key] = [fd, 1]
            try:
                yield
            finally:
                with _held_guard:
                    del _held[key]
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _acquire(self, fd: int) -> None:
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not lock {self.lock_path} within {self.lock_timeout} s"
                    ) from None
                time.sleep(_POLL_SECONDS)

    # Low level git

    def _run(self, *args: str, check: bool = True, identity: bool = False) -> subprocess.CompletedProcess:
        cmd = ["git", "-C", str(self.path), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        if identity:
            cmd += self._fallback_identity()
        cmd += list(args)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL
        )
        if check and proc.returncode != 0:
            raise GitError(cmd, proc.stderr, proc.returncode)
        return proc

    def _config_set(self, key: str) -> bool:
        proc = self._run("config", "--get", key, check=False)
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def _fallback_identity(self) -> list[str]:
        extra: list[str] = []
        if not self._config_set("user.name"):
            extra += ["-c", f"user.name={FALLBACK_NAME}"]
        if not self._config_set("user.email"):
            extra += ["-c", f"user.email={FALLBACK_EMAIL}"]
        return extra

    def _is_repo_unlocked(self) -> bool:
        if not self.path.is_dir():
            return False
        proc = self._run("rev-parse", "--show-toplevel", check=False)
        if proc.returncode != 0:
            return False
        return Path(proc.stdout.strip()).resolve() == self.path.resolve()

    def _ensure_excluded(self) -> None:
        """Keep the lock file out of status and commits via .git/info/exclude."""
        git_dir = Path(self._run("rev-parse", "--absolute-git-dir").stdout.strip())
        exclude = git_dir / "info" / "exclude"
        try:
            lines = exclude.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        if _EXCLUDE_LINE in lines:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            if lines and lines[-1] != "":
                fh.write("\n")
            fh.write(_EXCLUDE_LINE + "\n")

    def _has_head(self) -> bool:
        return self._run("rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0

    def _head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def _relative(self, path: Path) -> str:
        root = self.path.resolve()
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        p = Path(os.path.normpath(p))
        # Resolve the parent so prefixes like /tmp -> /private/tmp match, but
        # never the last component: a symlink inside the repo stays itself.
        if p != root and p.parent != p:
            p = p.parent.resolve() / p.name
        try:
            rel = p.relative_to(root)
        except ValueError:
            raise ValueError(f"{path} is outside the repo {self.path}") from None
        return rel.as_posix() or "."

    # Public operations

    def is_repo(self) -> bool:
        """True if path is the top level of a git work tree (a subdir of another repo is not)."""
        if not self.path.is_dir():
            return False
        with self.locked():
            return self._is_repo_unlocked()

    def init(self) -> None:
        """Create the repo on branch main if it is not one yet. Idempotent."""
        self.path.mkdir(parents=True, exist_ok=True)
        with self.locked():
            if not self._is_repo_unlocked():
                self._run("init", "-q", "-b", "main")
            self._ensure_excluded()

    def status_porcelain(self) -> list[str]:
        with self.locked():
            self._ensure_excluded()
            out = self._run("status", "--porcelain=v1").stdout
        return [line for line in out.split("\n") if line]

    def commit_paths(self, paths: list[Path], message: str) -> str | None:
        """Stage and commit only these paths (additions, edits, deletions).

        Returns the full sha, or None if the paths have no changes. Paths may be
        absolute or relative to the repo; anything outside raises ValueError.
        """
        if not paths:
            raise ValueError("commit_paths needs at least one path")
        rels = [self._relative(p) for p in paths]
        with self.locked():
            self._ensure_excluded()
            self._run("add", "-A", "--", *rels)
            if self._run("diff", "--cached", "--quiet", "--", *rels, check=False).returncode == 0:
                return None
            self._run("commit", "-q", "-m", message, "--", *rels, identity=True)
            return self._head()

    def commit_all(self, message: str) -> str | None:
        """Stage and commit everything in the work tree; None if nothing changed."""
        with self.locked():
            self._ensure_excluded()
            self._run("add", "-A")
            if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
                return None
            self._run("commit", "-q", "-m", message, identity=True)
            return self._head()

    def log(self, path: Path | None = None, n: int = 20) -> list[tuple[str, str, str]]:
        """Newest first (sha, committer ISO 8601 date, subject).

        path filters with a plain pathspec (a folder matches everything under
        it). No --follow: git only supports it for a single file, and skill
        moves are recorded as their own commits anyway.
        """
        args = ["log", f"-n{int(n)}", "--format=%H%x1f%cI%x1f%s"]
        if path is not None:
            args += ["--", self._relative(path)]
        with self.locked():
            if not self._has_head():
                return []
            out = self._run(*args).stdout
        entries = []
        for line in out.split("\n"):
            if line:
                sha, date, subject = line.split("\x1f", 2)
                entries.append((sha, date, subject))
        return entries

    def revert(self, sha: str) -> str:
        """Revert one commit with a new commit and return its sha. Aborts cleanly on conflict."""
        with self.locked():
            try:
                self._run("revert", "--no-edit", sha, identity=True)
            except GitError:
                self._run("revert", "--abort", check=False)
                raise
            return self._head()
