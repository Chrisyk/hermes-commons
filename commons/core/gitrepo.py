"""A small locked wrapper around local git for the shared skill library.

Every public operation that writes holds an exclusive fcntl.flock on
<path>/.commons/lock, so concurrent Hermes processes and threads (hooks, cron,
setup, the gateway's profiles) never interleave git commands. Read-only calls
(is_repo, status_porcelain, log) take no lock, create nothing and pass
--no-optional-locks so git does not refresh the index behind a writer.

Every git call disables hooks and commit signing, never prompts, runs with a
timeout (the whole process group is killed on expiry), ignores GIT_DIR-style
environment overrides, and falls back to the identity
"commons <commons@localhost>" when no user.name / user.email is configured.
Stdlib only. Linux and macOS.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = ".commons"
LOCK_NAME = "lock"
DEFAULT_LOCK_TIMEOUT = 10.0
DEFAULT_GIT_TIMEOUT = 30.0
FALLBACK_NAME = "commons"
FALLBACK_EMAIL = "commons@localhost"
_POLL_SECONDS = 0.05
# State commons keeps inside the library that must never be committed: the
# lock and the folders backups, staging copies and retired skills live in.
_EXCLUDE_LINES = (
    f"/{LOCK_DIR}/{LOCK_NAME}",
    f"/{LOCK_DIR}/backups/",
    f"/{LOCK_DIR}/staging/",
    f"/{LOCK_DIR}/retired/",
)
# Same paths as pathspecs, so read-only status hides them even in a repo whose
# info/exclude predates a line (read-only calls never write the exclude file).
_EXCLUDE_PATHSPECS = tuple(f":(exclude,top){line.strip('/')}" for line in _EXCLUDE_LINES)
_SHA = re.compile(r"[0-9a-f]{7,40}")
# Environment that would point git at another repo, index or config. HOME and
# XDG_CONFIG_HOME stay so the user's identity in ~/.gitconfig still applies.
_ENV_DROP = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    }
)
_ENV_DROP_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

# Reentrancy bookkeeping: (lock path, thread id) -> [fd, depth]. flock locks
# belong to an open file description, so a second open() in the same thread
# would block on itself; nested locked() calls reuse the first fd instead.
# Other threads open their own description and so wait like another process.
_held: dict[tuple[str, int], list[int]] = {}
_held_guard = threading.Lock()


class GitError(RuntimeError):
    def __init__(self, cmd: list[str], stderr: str, returncode: int | None = None):
        self.cmd = cmd
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"git failed ({returncode}): {' '.join(cmd)}\n{stderr.strip()}")


class GitTimeout(GitError):
    """git ran past git_timeout; its process group was killed."""

    def __init__(self, cmd: list[str], stderr: str, timeout: float):
        self.timeout = timeout
        super().__init__(cmd, stderr, None)
        self.args = (f"git timed out after {timeout} s and was killed: {' '.join(cmd)}",)

    def __str__(self) -> str:
        return self.args[0]


class LockTimeout(TimeoutError):
    pass


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP and not k.startswith(_ENV_DROP_PREFIXES)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class GitRepo:
    def __init__(
        self,
        path: Path,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        git_timeout: float = DEFAULT_GIT_TIMEOUT,
        git_bin: str = "git",
    ):
        self.path = Path(path)
        self.lock_timeout = lock_timeout
        self.git_timeout = git_timeout
        self.git_bin = git_bin

    # Locking

    @property
    def lock_path(self) -> Path:
        return self.path / LOCK_DIR / LOCK_NAME

    def _lock_key(self) -> tuple[str, int]:
        return (str(self.lock_path.resolve()), threading.get_ident())

    def _holds_lock(self) -> bool:
        with _held_guard:
            return self._lock_key() in _held

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the repo lock; reentrant within one thread, exclusive across threads and processes."""
        lock_path = self.lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        key = self._lock_key()
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
            self._write_holder(fd)
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
                        f"could not lock {self.lock_path} within {self.lock_timeout} s; "
                        f"held by {self._describe_holder(fd)}"
                    ) from None
                time.sleep(_POLL_SECONDS)

    @staticmethod
    def _write_holder(fd: int) -> None:
        """Record "<pid> <argv[0]>" in the lock file so a waiter can name the holder."""
        argv0 = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "python"
        data = f"{os.getpid()} {argv0}\n".encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, data, 0)

    @staticmethod
    def _describe_holder(fd: int) -> str:
        try:
            text = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
        except OSError:
            text = ""
        if not text:
            return "an unknown holder"
        pid, _, argv0 = text.partition(" ")
        return f"pid {pid} ({argv0})" if argv0 else f"pid {pid}"

    # Low level git

    def _git_dir_guess(self) -> Path:
        """<path>/.git, following a "gitdir:" file, without running git."""
        dot_git = self.path / ".git"
        if dot_git.is_file():
            with contextlib.suppress(OSError, ValueError):
                text = dot_git.read_text(encoding="utf-8").strip()
                if text.startswith("gitdir:"):
                    target = Path(text.removeprefix("gitdir:").strip())
                    return target if target.is_absolute() else self.path / target
        return dot_git

    def _run(
        self, *args: str, check: bool = True, identity: bool = False, read_only: bool = False
    ) -> subprocess.CompletedProcess:
        cmd = [self.git_bin, "-C", str(self.path)]
        if read_only:
            cmd.append("--no-optional-locks")
        cmd += ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        if identity:
            cmd += self._fallback_identity()
        cmd += list(args)
        # Only a lock holder may clean up index.lock after a kill: without the
        # repo lock, a fresh index.lock may belong to a concurrent writer.
        index_lock = self._git_dir_guess() / "index.lock" if self._holds_lock() else None
        lock_existed = index_lock is None or index_lock.exists()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_git_env(),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.git_timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            if not lock_existed and index_lock is not None:
                with contextlib.suppress(FileNotFoundError):
                    index_lock.unlink()
            raise GitTimeout(cmd, "", self.git_timeout) from None
        except BaseException:
            self._kill_group(proc)
            raise
        result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        if check and result.returncode != 0:
            raise GitError(cmd, result.stderr, result.returncode)
        return result

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Something outside the group still holds the pipes; stop reading.
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            proc.wait()

    def _config_set(self, key: str) -> bool:
        proc = self._run("config", "--get", key, check=False, read_only=True)
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
        proc = self._run("rev-parse", "--show-toplevel", check=False, read_only=True)
        if proc.returncode != 0:
            return False
        return Path(proc.stdout.strip()).resolve() == self.path.resolve()

    def _ensure_excluded(self) -> None:
        """Keep commons state (lock, backups, staging, retired) out of commits via .git/info/exclude."""
        git_dir = Path(self._run("rev-parse", "--absolute-git-dir").stdout.strip())
        exclude = git_dir / "info" / "exclude"
        try:
            text = exclude.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        present = set(text.splitlines())
        missing = [line for line in _EXCLUDE_LINES if line not in present]
        if not missing:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write("".join(line + "\n" for line in missing))

    def _has_head(self) -> bool:
        proc = self._run("rev-parse", "--verify", "-q", "HEAD", check=False, read_only=True)
        return proc.returncode == 0

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
        """True if path is the top level of a git work tree (a subdir of another repo is not). Read-only."""
        return self._is_repo_unlocked()

    def init(self) -> None:
        """Create the repo on branch main if it is not one yet. Idempotent."""
        self.path.mkdir(parents=True, exist_ok=True)
        with self.locked():
            if not self._is_repo_unlocked():
                self._run("init", "-q", "-b", "main")
            self._ensure_excluded()

    def status_porcelain(self) -> list[str]:
        """`git status --porcelain=v1` lines, commons state hidden. Read-only: no lock taken."""
        out = self._run("status", "--porcelain=v1", "--", ".", *_EXCLUDE_PATHSPECS, read_only=True).stdout
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

    def commit_all(self, message: str, exclude: Iterable[Path] = ()) -> str | None:
        """Stage and commit everything in the work tree but *exclude*; None if nothing changed.

        *exclude* paths (absolute or repo-relative, files or folders) are
        neither staged nor committed; an earlier staged change under one is
        unstaged first, so it can never ride along.
        """
        rels = [self._relative(p) for p in exclude]
        specs = [f":(exclude,top,literal){r}" for r in rels]
        with self.locked():
            self._ensure_excluded()
            if rels and self._has_head():
                self._run("reset", "-q", "--", *[f":(top,literal){r}" for r in rels], check=False)
            elif rels:
                self._run("rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", *rels, check=False)
            self._run("add", "-A", "--", ".", *specs)
            if self._run("diff", "--cached", "--quiet", check=False).returncode == 0:
                return None
            self._run("commit", "-q", "-m", message, identity=True)
            return self._head()

    def log(self, path: Path | None = None, n: int = 20) -> list[tuple[str, str, str]]:
        """Newest first (sha, committer ISO 8601 date, subject).

        path filters with a plain pathspec (a folder matches everything under
        it). No --follow: git only supports it for a single file, and skill
        moves are recorded as their own commits anyway. Read-only: no lock taken.
        """
        args = ["log", f"-n{int(n)}", "--format=%H%x1f%cI%x1f%s"]
        if path is not None:
            args += ["--", self._relative(path)]
        if not self._has_head():
            return []
        out = self._run(*args, read_only=True).stdout
        entries = []
        for line in out.split("\n"):
            if line:
                sha, date, subject = line.split("\x1f", 2)
                entries.append((sha, date, subject))
        return entries

    def head(self) -> str | None:
        """Full sha of HEAD, or None before the first commit. Read-only: no lock taken."""
        if not self._has_head():
            return None
        return self._run("rev-parse", "HEAD", read_only=True).stdout.strip()

    def changed_since(self, sha: str | None) -> list[str]:
        """Repo-relative POSIX paths changed in the work tree since commit *sha*, sorted.

        Committed and uncommitted edits, deletions and untracked files all
        count; commons state is left out. sha None means everything in the
        work tree. Read-only: no lock taken.
        """
        if sha is not None and (not isinstance(sha, str) or not _SHA.fullmatch(sha)):
            raise ValueError(f"bad commit sha {sha!r}: use 7 to 40 lowercase hex digits")
        if sha is None:
            listed = self._run("ls-files", "-z", "--", ".", *_EXCLUDE_PATHSPECS, read_only=True).stdout
        else:
            full = self._run("rev-parse", "--verify", f"{sha}^{{commit}}", read_only=True).stdout.strip()
            listed = self._run(
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                full,
                "--",
                ".",
                *_EXCLUDE_PATHSPECS,
                read_only=True,
            ).stdout
        others = self._run(
            "ls-files", "--others", "--exclude-standard", "-z", "--", ".", *_EXCLUDE_PATHSPECS, read_only=True
        ).stdout
        return sorted({p for p in (listed + others).split("\0") if p})

    def exists_at(self, sha: str, path: Path | str) -> bool:
        """True if *path* (a file or folder) is in commit *sha*. Read-only: no lock taken."""
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise ValueError(f"bad commit sha {sha!r}: use 7 to 40 lowercase hex digits")
        rel = self._relative(Path(path))
        proc = self._run("cat-file", "-e", f"{sha}:{rel}", check=False, read_only=True)
        return proc.returncode == 0

    def resolve_commit(self, sha: str) -> str:
        """Full commit id of *sha* (7 to 40 lowercase hex digits); GitError if it is no commit. Read-only."""
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise ValueError(f"bad commit sha {sha!r}: use 7 to 40 lowercase hex digits")
        return self._run("rev-parse", "--verify", "-q", f"{sha}^{{commit}}", read_only=True).stdout.strip()

    def files_in_commit(self, sha: str) -> list[str]:
        """Repo-relative paths commit *sha* changed against its first parent (as git show --name-only). Read-only."""
        full = self.resolve_commit(sha)
        out = self._run(
            "diff-tree",
            "--root",
            "-r",
            "-z",
            "--no-renames",
            "--name-only",
            "--no-commit-id",
            full,
            read_only=True,
        ).stdout
        return sorted({p for p in out.split("\0") if p})

    def files_at(self, sha: str, path: Path | str) -> list[str]:
        """Repo-relative paths of every file under *path* in commit *sha*. Read-only."""
        full = self.resolve_commit(sha)
        rel = self._relative(Path(path))
        out = self._run("ls-tree", "-r", "-z", "--name-only", full, "--", rel, read_only=True).stdout
        return sorted({p for p in out.split("\0") if p})

    def checkout_path(self, sha: str, path: Path | str) -> None:
        """`git checkout <sha> -- <path>`: the work tree and index of *path* as in commit *sha*.

        Files under *path* that *sha* does not have are left alone (git does
        not remove them); the caller moves them aside first.
        """
        rel = self._relative(Path(path))
        with self.locked():
            full = self.resolve_commit(sha)
            self._run("checkout", "-q", full, "--", rel)

    def revert(self, sha: str) -> str:
        """Revert one commit with a new commit and return its sha. Aborts cleanly on conflict.

        sha must be 7 to 40 lowercase hex digits (never a ref or an option);
        git revert takes no '--' before commits, so it is resolved to a full
        commit id with rev-parse first and only that is passed on.
        """
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise ValueError(f"bad commit sha {sha!r}: use 7 to 40 lowercase hex digits")
        with self.locked():
            full = self._run("rev-parse", "--verify", f"{sha}^{{commit}}").stdout.strip()
            try:
                self._run("revert", "--no-edit", full, identity=True)
            except GitError:
                with contextlib.suppress(GitError):
                    self._run("revert", "--abort", check=False)
                raise
            return self._head()
