"""Tests for commons.core.gitrepo: a locked wrapper around local git."""

from __future__ import annotations

import multiprocessing
import subprocess
from pathlib import Path

import pytest

from commons.core.gitrepo import GitError, GitRepo, LockTimeout


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
    assert "revert" in info.value.cmd


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
