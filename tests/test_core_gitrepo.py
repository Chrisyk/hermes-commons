"""Tests for commons.core.gitrepo: a locked wrapper around local git."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from commons.core.gitrepo import GitError, GitRepo, GitTimeout, LockTimeout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path) -> GitRepo:
    r = GitRepo(tmp_path / "lib")
    r.init()
    return r


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_init_is_idempotent(tmp_path):
    r = GitRepo(tmp_path / "lib")
    assert not r.is_repo()
    r.init()
    assert r.is_repo()
    assert _git(r.path, "symbolic-ref", "--short", "HEAD").strip() == "main"
    _write(r.path / "a.txt", "a\n")
    sha = r.commit_all("first")
    r.init()
    assert r.is_repo()
    assert _git(r.path, "rev-parse", "HEAD").strip() == sha


def test_subdir_of_other_repo_is_not_a_repo(repo):
    inner = GitRepo(repo.path / "inner")
    (repo.path / "inner").mkdir()
    assert not inner.is_repo()


def test_lock_file_is_not_reported_or_committed(repo):
    assert repo.status_porcelain() == []
    _write(repo.path / "a.txt", "a\n")
    assert repo.status_porcelain() == ["?? a.txt"]
    repo.commit_all("add a")
    assert _git(repo.path, "ls-files").split() == ["a.txt"]


def test_commit_paths_returns_full_sha(repo):
    f = _write(repo.path / "skills" / "one" / "SKILL.md", "one\n")
    sha = repo.commit_paths([f.parent], "add one")
    assert sha is not None and len(sha) == 40
    assert _git(repo.path, "rev-parse", "HEAD").strip() == sha
    assert _git(repo.path, "log", "-1", "--format=%s").strip() == "add one"


def test_commit_paths_only_commits_given_paths(repo):
    _write(repo.path / "a.txt", "a\n")
    _write(repo.path / "b.txt", "b\n")
    repo.commit_paths([Path("a.txt")], "only a")
    assert _git(repo.path, "ls-files").split() == ["a.txt"]
    assert repo.status_porcelain() == ["?? b.txt"]


def test_commit_paths_records_deletion(repo):
    f = _write(repo.path / "a.txt", "a\n")
    repo.commit_all("add a")
    f.unlink()
    sha = repo.commit_paths([f], "remove a")
    assert sha is not None
    assert _git(repo.path, "ls-files").split() == []


def test_nothing_to_commit_returns_none(repo):
    f = _write(repo.path / "a.txt", "a\n")
    assert repo.commit_paths([f], "add a") is not None
    assert repo.commit_paths([f], "again") is None
    assert repo.commit_all("again") is None


def test_commit_all_on_empty_repo_returns_none(repo):
    assert repo.commit_all("nothing") is None


def test_path_outside_repo_raises(repo, tmp_path):
    outside = _write(tmp_path / "elsewhere.txt", "x\n")
    with pytest.raises(ValueError):
        repo.commit_paths([outside], "nope")
    with pytest.raises(ValueError):
        repo.commit_paths([Path("../elsewhere.txt")], "nope")


def test_log_filters_by_path(repo):
    a = _write(repo.path / "a" / "SKILL.md", "a1\n")
    b = _write(repo.path / "b" / "SKILL.md", "b1\n")
    sha_a1 = repo.commit_paths([a], "a one")
    sha_b1 = repo.commit_paths([b], "b one")
    a.write_text("a2\n", encoding="utf-8")
    sha_a2 = repo.commit_paths([a], "a two")

    everything = repo.log()
    assert [e[0] for e in everything] == [sha_a2, sha_b1, sha_a1]
    assert [e[2] for e in everything] == ["a two", "b one", "a one"]
    assert all("T" in e[1] for e in everything)

    assert [e[0] for e in repo.log(repo.path / "a")] == [sha_a2, sha_a1]
    assert [e[0] for e in repo.log(Path("b"))] == [sha_b1]
    assert len(repo.log(n=1)) == 1


def test_log_on_empty_repo(repo):
    assert repo.log() == []


def test_revert(repo):
    f = _write(repo.path / "a.txt", "one\n")
    repo.commit_all("one")
    f.write_text("two\n", encoding="utf-8")
    bad = repo.commit_all("two")
    sha = repo.revert(bad)
    assert len(sha) == 40 and sha != bad
    assert f.read_text(encoding="utf-8") == "one\n"
    assert repo.log(n=1)[0][0] == sha


def test_git_failure_raises_git_error(repo):
    with pytest.raises(GitError) as info:
        repo.revert("0" * 40)
    assert info.value.stderr
    assert "rev-parse" in info.value.cmd


def _commit_many(path: str, worker: int, count: int) -> None:
    r = GitRepo(Path(path))
    for i in range(count):
        f = _write(Path(path) / f"w{worker}" / f"f{i}.txt", f"{worker}-{i}\n")
        assert r.commit_paths([f], f"worker {worker} file {i}") is not None


def test_two_processes_committing_all_land(repo):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_commit_many, args=(str(repo.path), w, 5)) for w in (1, 2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    assert _git(repo.path, "rev-list", "--count", "HEAD").strip() == "10"
    assert len(_git(repo.path, "ls-files").split()) == 10
    fsck = subprocess.run(
        ["git", "-C", str(repo.path), "fsck", "--strict"], check=False, capture_output=True, text=True
    )
    assert fsck.returncode == 0, fsck.stderr
    assert repo.status_porcelain() == []


def _try_commit(path: str, queue) -> None:
    r = GitRepo(Path(path), lock_timeout=0.3)
    try:
        r.commit_all("blocked")
    except LockTimeout:
        queue.put("LockTimeout")
    else:
        queue.put("committed")


def test_held_lock_makes_other_process_time_out(repo):
    _write(repo.path / "a.txt", "a\n")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with repo.locked():
        p = ctx.Process(target=_try_commit, args=(str(repo.path), queue))
        p.start()
        result = queue.get(timeout=60)
        p.join(60)
    assert result == "LockTimeout"
    assert repo.log() == []
    # Lock released: the same commit now goes through.
    assert repo.commit_all("unblocked") is not None


def test_locked_is_reentrant(repo):
    _write(repo.path / "a.txt", "a\n")
    with repo.locked():
        assert repo.commit_all("inside lock") is not None


def test_fallback_identity_without_user_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_CONFIG_GLOBAL",
        "EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    r = GitRepo(tmp_path / "lib")
    r.init()
    _write(r.path / "a.txt", "a\n")
    assert r.commit_all("anon") is not None
    assert _git(r.path, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
        "commons <commons@localhost>|commons <commons@localhost>"
    )


def test_repo_identity_is_kept(repo):
    _git(repo.path, "config", "user.name", "Someone")
    _git(repo.path, "config", "user.email", "someone@example.com")
    _write(repo.path / "a.txt", "a\n")
    repo.commit_all("named")
    assert _git(repo.path, "log", "-1", "--format=%an <%ae>").strip() == "Someone <someone@example.com>"


def test_hooks_are_disabled(repo):
    hook = repo.path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _write(repo.path / "a.txt", "a\n")
    assert repo.commit_all("hook would fail") is not None


def _slow_git(tmp_path: Path) -> Path:
    """A fake git that takes the index lock, starts a grandchild and hangs. argv is git -C <repo> ..."""
    script = tmp_path / "slowgit"
    script.write_text(
        '#!/bin/sh\ntouch "$2/.git/index.lock"\nsleep 60 &\necho $! > "$2/../grandchild.pid"\nwait\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _gone(pid: int) -> bool:
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat_line.rsplit(")", 1)[1].split()[0] == "Z"


def test_hung_git_is_killed_and_its_index_lock_removed(repo, tmp_path):
    slow = GitRepo(repo.path, git_bin=str(_slow_git(tmp_path)), git_timeout=0.5)
    start = time.monotonic()
    with pytest.raises(GitTimeout) as info:
        slow.commit_all("never")
    assert time.monotonic() - start < 10
    assert isinstance(info.value, GitError)
    assert not (repo.path / ".git" / "index.lock").exists()
    grandchild = int((repo.path.parent / "grandchild.pid").read_text())
    deadline = time.monotonic() + 5
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild)
    # The real repo still works afterwards and the lock was released.
    _write(repo.path / "a.txt", "a\n")
    assert repo.commit_all("after timeout") is not None


def test_timeout_keeps_an_index_lock_that_was_already_there(repo, tmp_path):
    lock = repo.path / ".git" / "index.lock"
    lock.write_text("someone else\n", encoding="utf-8")
    slow = GitRepo(repo.path, git_bin=str(_slow_git(tmp_path)), git_timeout=0.3)
    with pytest.raises(GitTimeout):
        slow.commit_all("never")
    assert lock.read_text(encoding="utf-8") == "someone else\n"


def test_unlocked_read_timeout_never_removes_index_lock(repo, tmp_path):
    # status runs without the repo lock, so an index.lock that appears meanwhile
    # may belong to a concurrent locked writer and must be left alone.
    slow = GitRepo(repo.path, git_bin=str(_slow_git(tmp_path)), git_timeout=0.3)
    with pytest.raises(GitTimeout):
        slow.status_porcelain()
    assert (repo.path / ".git" / "index.lock").exists()


def test_git_env_does_not_redirect_commits(repo, tmp_path, monkeypatch):
    other = GitRepo(tmp_path / "other")
    other.init()
    monkeypatch.setenv("GIT_DIR", str(other.path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other.path))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other.path / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'user.email'='evil@example.com'")
    _write(repo.path / "z.txt", "z\n")
    sha = repo.commit_paths([Path("z.txt")], "z")
    assert repo.is_repo()
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.delenv(var)
    assert _git(repo.path, "rev-parse", "HEAD").strip() == sha
    assert "evil" not in _git(repo.path, "log", "-1", "--format=%an <%ae>")
    assert _git(repo.path, "ls-files").split() == ["z.txt"]
    assert other.log() == []


@pytest.mark.parametrize("sub", ["backups", "staging", "retired"])
def test_commit_all_never_commits_commons_state(repo, sub):
    _write(repo.path / ".commons" / sub / "b1" / "config.yaml", "api_key: SECRET\n")
    _write(repo.path / "a.txt", "a\n")
    assert repo.status_porcelain() == ["?? a.txt"]
    repo.commit_all("all")
    assert _git(repo.path, "ls-files").split() == ["a.txt"]


def test_exclude_lines_added_once(repo):
    repo.init()
    lines = (repo.path / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    for line in ("/.commons/lock", "/.commons/backups/", "/.commons/staging/", "/.commons/retired/"):
        assert lines.count(line) == 1


def test_is_repo_on_plain_dir_leaves_it_empty(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not GitRepo(plain).is_repo()
    assert list(plain.iterdir()) == []


def test_read_only_calls_take_no_lock(repo):
    _write(repo.path / "a.txt", "a\n")
    repo.commit_all("a")
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with repo.locked():
            holding.set()
            release.wait(30)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(10)
        blocked = GitRepo(repo.path, lock_timeout=0.2)
        assert blocked.is_repo()
        assert blocked.status_porcelain() == []
        assert [e[2] for e in blocked.log()] == ["a"]
    finally:
        release.set()
        t.join()


def test_read_only_calls_do_not_create_state_dir(repo):
    _write(repo.path / "a.txt", "a\n")
    repo.commit_all("a")
    (repo.path / ".commons" / "lock").unlink()
    (repo.path / ".commons").rmdir()
    assert repo.is_repo()
    assert repo.status_porcelain() == []
    assert len(repo.log()) == 1
    assert not (repo.path / ".commons").exists()


@pytest.mark.parametrize("bad", ["--quit", "-n", "HEAD", "abc", "g" * 40, "A" * 40, "0" * 41, ""])
def test_revert_rejects_non_sha(repo, bad):
    with pytest.raises(ValueError):
        repo.revert(bad)


def test_revert_accepts_short_sha(repo):
    f = _write(repo.path / "a.txt", "one\n")
    repo.commit_all("one")
    f.write_text("two\n", encoding="utf-8")
    bad = repo.commit_all("two")
    repo.revert(bad[:7])
    assert f.read_text(encoding="utf-8") == "one\n"


def test_lock_file_names_holder_and_timeout_names_pid(repo):
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with repo.locked():
            holding.set()
            release.wait(30)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holding.wait(10)
        assert repo.lock_path.read_text(encoding="utf-8").split()[0] == str(os.getpid())
        with pytest.raises(LockTimeout) as info, GitRepo(repo.path, lock_timeout=0.2).locked():
            pass
        assert str(os.getpid()) in str(info.value)
    finally:
        release.set()
        t.join()


def test_four_threads_committing_all_land(repo):
    errors: list[BaseException] = []

    def work(worker: int) -> None:
        try:
            r = GitRepo(repo.path, lock_timeout=60)
            for i in range(5):
                f = _write(repo.path / f"t{worker}" / f"f{i}.txt", f"{worker}-{i}\n")
                with r.locked():  # nested: reentrant within this thread
                    assert r.commit_paths([f], f"thread {worker} file {i}") is not None
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []
    assert _git(repo.path, "rev-list", "--count", "HEAD").strip() == "20"
    assert len(_git(repo.path, "ls-files").split()) == 20
    fsck = subprocess.run(
        ["git", "-C", str(repo.path), "fsck", "--strict"], check=False, capture_output=True, text=True
    )
    assert fsck.returncode == 0, fsck.stderr
    assert repo.status_porcelain() == []


def test_head_and_changed_since(repo):
    assert repo.head() is None
    _write(repo.path / "a" / "x.md", "1\n")
    _write(repo.path / ".commons" / "lock", "")
    assert repo.changed_since(None) == ["a/x.md"]
    first = repo.commit_all("first")
    assert repo.head() == first
    assert repo.changed_since(first) == []
    _write(repo.path / "a" / "x.md", "2\n")
    _write(repo.path / "b" / "new.md", "n\n")
    second = repo.commit_paths([repo.path / "a"], "edit a")
    assert repo.changed_since(first) == ["a/x.md", "b/new.md"]
    assert repo.changed_since(second) == ["b/new.md"]
    (repo.path / "a" / "x.md").unlink()
    assert repo.changed_since(second) == ["a/x.md", "b/new.md"]
    with pytest.raises(ValueError):
        repo.changed_since("HEAD")


def test_exists_at(repo):
    _write(repo.path / "a" / "x.md", "1\n")
    first = repo.commit_all("first")
    _write(repo.path / "b" / "y.md", "2\n")
    repo.commit_all("second")
    assert repo.exists_at(first, "a") and repo.exists_at(first, repo.path / "a" / "x.md")
    assert not repo.exists_at(first, "b")
    with pytest.raises(ValueError):
        repo.exists_at("main", "a")
