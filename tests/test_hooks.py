"""Lifecycle and session hooks, with the Hermes adapter replaced by the test fleet."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
import types
from pathlib import Path

import pytest
import yaml

from commons import hermes_adapter as ha
from commons import hooks
from commons.core.fleet import discover
from commons.core.gitrepo import GitError, GitRepo
from commons.core.journal import operation
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


# ------------------------------------------------------------------ profile context


def test_worker_runs_each_job_in_the_callers_context():
    var = contextvars.ContextVar("v", default="process")
    seen = []
    w = hooks.Worker()
    token = var.set("caller")
    try:
        w.submit(lambda: seen.append(var.get()))
    finally:
        var.reset(token)
    w.submit(lambda: seen.append(var.get()))
    assert w.flush(10)
    assert seen == ["caller", "process"]


def _fake_module(monkeypatch, dotted, **attrs):
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    mod = types.ModuleType(dotted)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, dotted, mod)
    return mod


@pytest.fixture
def hermes_like(tmp_path, monkeypatch):
    """Fake Hermes modules that keep the active home in a ContextVar, as hermes_constants does.

    Mirrors the public mechanism of Hermes v0.23: set_hermes_home_override
    pushes the home into hermes_scope's ContextVar state (gateway/run.py binds
    it per turn), get_hermes_home reads it, profile_name_for_home maps a home
    to its profile id, and tools.skills_tool searches the active home's skills
    dir and its skills.create_dir. Nothing in hermes_adapter is patched.
    """
    root = tmp_path / "h"
    shared = tmp_path / "shared"
    (root / "skills").mkdir(parents=True)
    (root / "config.yaml").write_text("skills: {}\n", encoding="utf-8")  # default: no shared root
    dev = root / "profiles" / "dev"
    (dev / "skills").mkdir(parents=True)
    (dev / "config.yaml").write_text(f"skills:\n  create_dir: {shared}\n", encoding="utf-8")
    write_files(shared / "general" / "foo", {"SKILL.md": skill_md("foo", "Foo.", ["one"])})
    repo = GitRepo(shared)
    repo.init()
    repo.commit_all("start")
    home_var = contextvars.ContextVar("hermes_scope_state", default=None)

    def set_hermes_home_override(path):
        return home_var.set(None if path is None else str(path))

    def get_hermes_home():
        return Path(home_var.get() or root)

    def profile_name_for_home(path):
        p = Path(path)
        if p == root:
            return "default"
        return p.name if p.parent.name == "profiles" else None

    hc = _fake_module(
        monkeypatch,
        "hermes_constants",
        set_hermes_home_override=set_hermes_home_override,
        reset_hermes_home_override=home_var.reset,
        get_hermes_home=get_hermes_home,
        get_default_hermes_root=lambda: root,
        profile_name_for_home=profile_name_for_home,
        named_profile_is_deleted=lambda p: False,
    )

    def skill_search_dirs():
        home = get_hermes_home()
        roots = [(1, home / "skills")]
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        create = (cfg.get("skills") or {}).get("create_dir")
        if create:
            roots.append((2, Path(create)))
        return roots, home / "skills"

    def locate_skill(name, _local, roots):
        for _tier, d in roots:
            for md in sorted(Path(d).rglob("SKILL.md")):
                if md.parent.name == name:
                    return None, md.parent, md
        return '{"error": "not found"}', None, None

    _fake_module(
        monkeypatch,
        "tools.skills_tool",
        skill_lookup_path_error=lambda n: None,
        skill_search_dirs=skill_search_dirs,
        locate_skill=locate_skill,
    )
    monkeypatch.delenv("COMMONS_HERMES_ROOT", raising=False)
    worker = hooks.Worker()
    monkeypatch.setattr(hooks, "WORKER", worker)
    yield {"hc": hc, "root": root, "dev": dev, "shared": shared, "repo": repo, "worker": worker}
    worker.flush(10)


def test_hook_job_sees_the_profile_the_caller_set(hermes_like):
    hc, shared, repo = hermes_like["hc"], hermes_like["shared"], hermes_like["repo"]
    _edit(shared, "from dev")
    seen = {}

    def caller():
        token = hc.set_hermes_home_override(hermes_like["dev"])
        try:
            seen["caller"] = ha.active_profile_name()
            hooks.on_skill_lifecycle(action="edited", skill_name="foo", session_id="t1")
        finally:
            hc.reset_hermes_home_override(token)

    # The turn runs on its own thread, as a gateway turn does; the worker is another thread.
    t = threading.Thread(target=caller)
    t.start()
    t.join(10)
    assert hermes_like["worker"].flush(10)
    assert seen["caller"] == "dev"
    assert ha.active_profile_name() == "default"  # the override never leaked out of the turn
    assert repo.log(n=1)[0][2] == "edited foo (profile dev, session t1)"


def test_hook_job_without_override_uses_the_default_profile(hermes_like):
    # default has no create_dir, so locate() finds nothing in its roots and nothing is committed.
    _edit(hermes_like["shared"], "from default")
    hooks.on_skill_lifecycle(action="edited", skill_name="foo", session_id="t2")
    assert hermes_like["worker"].flush(10)
    assert len(hermes_like["repo"].log()) == 1


def test_session_start_checks_the_callers_profile(hermes_like, caplog):
    hc, dev = hermes_like["hc"], hermes_like["dev"]
    write_files(dev / "skills" / "general" / "foo", {"SKILL.md": skill_md("foo", "Local.", ["l"])})
    token = hc.set_hermes_home_override(dev)
    try:
        with caplog.at_level(logging.WARNING, logger="commons.hooks"):
            hooks.on_session_start(session_id="s")
            assert hermes_like["worker"].flush(10)
    finally:
        hc.reset_hermes_home_override(token)
    assert any("profile dev" in r.getMessage() and "foo" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ unfinished journals


def test_hook_does_not_commit_while_a_journal_is_unfinished(wired, caplog):
    lay = wired["layout"]
    try:
        with operation(lay, [], "promote-x", "promote"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    _edit(wired["shared"], "during an unfinished operation")
    with caplog.at_level(logging.WARNING, logger="commons.hooks"):
        hooks.on_skill_lifecycle(action="edited", skill_name="foo", session_id="s")
        assert wired["worker"].flush(10)
    assert len(wired["repo"].log()) == 1
    assert wired["repo"].status_porcelain() != []
    assert any(
        "did not finish" in r.getMessage() and "not committed" in r.getMessage() for r in caplog.records
    )


# ------------------------------------------------------------------ lock shared: in-lock snapshot


def _use_guard_state(monkeypatch, settings):
    from commons import guard
    from commons.core import consistency as cons

    state = guard._State()
    monkeypatch.setattr(guard, "STATE", state)
    monkeypatch.setattr(guard, "cached_settings", lambda: cons.Settings(lock=settings))


def test_lock_shared_keeps_a_version_replaced_before_its_commit(wired, monkeypatch):
    _use_guard_state(monkeypatch, "shared")
    shared = wired["shared"]
    gate = threading.Event()
    real = hooks._commit_skill

    def slow(*a, **k):
        gate.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(hooks, "_commit_skill", slow)
    _edit(shared, "written by A")
    hooks.on_skill_lifecycle(action="patched", skill_name="foo", session_id="A", provenance="local")
    md = shared / "general" / "foo" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8").replace("written by A", "B overwrote it"), encoding="utf-8")
    gate.set()
    assert wired["worker"].flush(10)
    kept = list((wired["layout"].state_dir / "overwritten").iterdir())
    assert len(kept) == 1 and kept[0].name.endswith("-foo")
    assert "written by A" in (kept[0] / "SKILL.md").read_text(encoding="utf-8")
    meta = json.loads((kept[0] / ".commons-overwritten.json").read_text(encoding="utf-8"))
    assert meta["session"] == "A"


def test_lock_shared_keeps_nothing_when_the_folder_is_unchanged(wired, monkeypatch):
    _use_guard_state(monkeypatch, "shared")
    _edit(wired["shared"], "fine")
    hooks.on_skill_lifecycle(action="patched", skill_name="foo", session_id="A", provenance="local")
    assert wired["worker"].flush(10)
    assert not (wired["layout"].state_dir / "overwritten").exists()


def test_lock_own_takes_no_snapshot(wired, monkeypatch):
    _use_guard_state(monkeypatch, "own")
    monkeypatch.setattr(hooks, "_read_tree", lambda *a, **k: pytest.fail("no copy with lock own"))
    _edit(wired["shared"], "x")
    hooks.on_skill_lifecycle(action="patched", skill_name="foo", session_id="A", provenance="local")
    assert wired["worker"].flush(10)
