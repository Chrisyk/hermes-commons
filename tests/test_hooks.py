"""Lifecycle and session hooks, with the Hermes adapter replaced by the test fleet."""

from __future__ import annotations

import json
import logging
import threading

import pytest

from commons import hermes_adapter as ha
from commons import hooks
from commons.core.fleet import discover
from commons.core.gitrepo import GitError, GitRepo
from commons.core.layout import Layout
from tests.fleet import dir_hash_md5, hub_content_hash, skill_md, write_files


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A two-profile fleet whose create_dir is a git-tracked shared root; adapter pointed at it."""
    root = tmp_path / "h"
    shared = tmp_path / "shared"
    for home in (root, root / "profiles" / "dev"):
        (home / "skills").mkdir(parents=True)
        (home / "config.yaml").write_text(f"skills:\n  create_dir: {shared}\n", encoding="utf-8")
    write_files(shared / "general" / "foo", {"SKILL.md": skill_md("foo", "Foo.", ["one"])})
    repo = GitRepo(shared)
    repo.init()
    repo.commit_all("start")
    lay = Layout(shared_root=shared, state_dir=root / "commons")
    state = {"active": "dev", "located": {"foo": shared / "general" / "foo"}}
    monkeypatch.setattr(ha, "fleet", lambda: discover(root))
    monkeypatch.setattr(ha, "layout", lambda profiles, shared_root=None: lay)
    monkeypatch.setattr(ha, "state_dir", lambda: lay.state_dir)
    monkeypatch.setattr(ha, "active_profile_name", lambda: state["active"])
    monkeypatch.setattr(ha, "locate", lambda name: state["located"].get(name))
    monkeypatch.setattr(ha, "md5_dir_hash", dir_hash_md5)
    monkeypatch.setattr(ha, "hub_hash", hub_content_hash)
    worker = hooks.Worker()
    monkeypatch.setattr(hooks, "WORKER", worker)
    yield {"root": root, "shared": shared, "repo": repo, "layout": lay, "state": state, "worker": worker}
    worker.flush(10)


def _edit(shared, line):
    md = shared / "general" / "foo" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def test_three_edits_make_three_commits(wired):
    shared, repo = wired["shared"], wired["repo"]
    for i, action in enumerate(["created", "edited", "patched"]):
        _edit(shared, f"line {i}")
        hooks.on_skill_lifecycle(action=action, skill_name="foo", session_id=f"s{i}", provenance="local")
        assert wired["worker"].flush(10)
    subjects = [s for _sha, _date, s in repo.log()]
    assert subjects[:3] == [
        "patched foo (profile dev, session s2)",
        "edited foo (profile dev, session s1)",
        "created foo (profile dev, session s0)",
    ]
    assert repo.status_porcelain() == []


def test_loaded_and_unknown_actions_commit_nothing(wired):
    _edit(wired["shared"], "x")
    for action in ("loaded", "installed", "deleted", None):
        hooks.on_skill_lifecycle(action=action, skill_name="foo", session_id="s")
    assert wired["worker"].flush(10)
    assert len(wired["repo"].log()) == 1


def test_skill_outside_the_shared_root_is_not_committed(wired):
    local = wired["root"] / "skills" / "bar"
    write_files(local, {"SKILL.md": skill_md("bar", "Bar.", ["b"])})
    wired["state"]["located"]["bar"] = local
    hooks.on_skill_lifecycle(action="created", skill_name="bar", session_id="s")
    hooks.on_skill_lifecycle(action="edited", skill_name="missing", session_id="s")
    assert wired["worker"].flush(10)
    assert len(wired["repo"].log()) == 1


def test_git_failure_is_swallowed_and_logged(wired, monkeypatch, caplog):
    def boom(self, paths, message):
        raise GitError(["git", "commit"], "disk full", 1)

    monkeypatch.setattr(GitRepo, "commit_paths", boom)
    _edit(wired["shared"], "x")
    with caplog.at_level(logging.WARNING, logger="commons.hooks"):
        assert hooks.on_skill_lifecycle(action="edited", skill_name="foo", session_id="s") is None
        assert wired["worker"].flush(10)
    assert any("disk full" in r.getMessage() for r in caplog.records)


def test_hook_returns_before_the_commit_runs(wired, monkeypatch):
    gate = threading.Event()
    real = GitRepo.commit_paths

    def slow(self, paths, message):
        gate.wait(10)
        return real(self, paths, message)

    monkeypatch.setattr(GitRepo, "commit_paths", slow)
    _edit(wired["shared"], "x")
    hooks.on_skill_lifecycle(action="edited", skill_name="foo", session_id="s")
    assert len(wired["repo"].log()) == 1  # not yet
    gate.set()
    assert wired["worker"].flush(10)
    assert len(wired["repo"].log()) == 2


def test_a_full_queue_drops_with_a_warning(caplog):
    gate = threading.Event()
    w = hooks.Worker(maxsize=1)
    w.submit(lambda: gate.wait(10))
    with caplog.at_level(logging.WARNING, logger="commons.hooks"):
        for _ in range(3):
            w.submit(lambda: None)
    gate.set()
    assert w.flush(10)
    assert any("queue is full" in r.getMessage() for r in caplog.records)


def test_hook_errors_before_the_queue_never_raise(monkeypatch, caplog):
    monkeypatch.setattr(hooks, "WORKER", None)

    def bad_submit(fn):
        raise RuntimeError("no thread")

    monkeypatch.setattr(hooks, "_submit", bad_submit)
    with caplog.at_level(logging.WARNING, logger="commons.hooks"):
        assert hooks.on_skill_lifecycle(action="edited", skill_name="foo") is None
        assert hooks.on_session_start(session_id="s") is None
    assert sum("no thread" in r.getMessage() for r in caplog.records) == 2


# ------------------------------------------------------------------ session start shadow check


def test_session_start_logs_new_shadows_once(wired, caplog):
    root, lay = wired["root"], wired["layout"]
    write_files(
        root / "profiles" / "dev" / "skills" / "general" / "foo",
        {"SKILL.md": skill_md("foo", "Local.", ["l"])},
    )
    with caplog.at_level(logging.WARNING, logger="commons.hooks"):
        hooks.on_session_start(session_id="s1")
        assert wired["worker"].flush(10)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("dev" in m and "foo" in m and "shadow" in m for m in msgs)
    cache = json.loads((lay.state_dir / hooks.CACHE_FILE).read_text(encoding="utf-8"))
    assert cache["shadows"]["dev"]["shadowed"] == ["foo"]
    assert not (lay.shared_root / ".commons" / hooks.CACHE_FILE).exists()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="commons.hooks"):
        hooks.on_session_start(session_id="s2")
        assert wired["worker"].flush(10)
    assert not any("shadow" in r.getMessage() for r in caplog.records)


def test_session_start_cache_skips_the_scan(wired, monkeypatch):
    calls = []
    real = hooks._shadowed

    def counting(profile, layout):
        calls.append(profile.name)
        return real(profile, layout)

    monkeypatch.setattr(hooks, "_shadowed", counting)
    hooks.on_session_start(session_id="a")
    hooks.on_session_start(session_id="b")
    assert wired["worker"].flush(10)
    assert calls == ["dev"]
    write_files(wired["shared"] / "general" / "new", {"SKILL.md": skill_md("new", "N.", ["n"])})
    hooks.on_session_start(session_id="c")
    assert wired["worker"].flush(10)
    assert calls == ["dev", "dev"]


def test_session_start_ignores_intentional_overrides(wired, caplog):
    root, lay = wired["root"], wired["layout"]
    write_files(
        root / "profiles" / "dev" / "skills" / "general" / "foo",
        {"SKILL.md": skill_md("foo", "Local.", ["l"])},
    )
    lay.commons_dir.mkdir(exist_ok=True)
    lay.config_path.write_text(json.dumps({"intentional_overrides": [{"name": "foo", "profile": "dev"}]}))
    with caplog.at_level(logging.WARNING, logger="commons.hooks"):
        hooks.on_session_start(session_id="s")
        assert wired["worker"].flush(10)
    assert not any("shadow" in r.getMessage() for r in caplog.records)
