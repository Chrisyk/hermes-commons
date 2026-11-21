"""The guard hooks (commons.guard) against a shared root, with the adapter faked."""

from __future__ import annotations

import json

import pytest

import commons
from commons import guard
from commons import hermes_adapter as ha
from commons.core import consistency as cons
from commons.core.layout import Layout
from tests.fleet import skill_md, write_files

SKILL = "foo"


@pytest.fixture
def env(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    folder = shared / "general" / SKILL
    write_files(folder, {"SKILL.md": skill_md(SKILL, "Foo.", ["one", "two"]), "references/r.md": "ref\n"})
    state = tmp_path / "state"
    lay = Layout(shared_root=shared, state_dir=state)
    profile = {"name": "dev"}
    monkeypatch.setattr(ha, "fleet", list)
    monkeypatch.setattr(ha, "layout", lambda profiles, shared_root=None: lay)
    monkeypatch.setattr(ha, "state_dir", lambda: state)
    monkeypatch.setattr(ha, "active_profile_name", lambda: profile["name"])
    monkeypatch.setattr(ha, "locate", lambda name: folder if name == SKILL else None)
    monkeypatch.setattr(guard, "STATE", guard._State())
    cons.write_settings(state, cons.Settings(guard="strict"))
    return {"folder": folder, "md": folder / "SKILL.md", "state": state, "profile": profile}


_n = iter(range(10**6))


def call(tool, args, session="A", req=None):
    """One tool call through both hooks; returns the block message or None (and runs the 'tool')."""
    cid = f"c{next(_n)}"
    kw = {"session_id": session, "tool_call_id": cid, "api_request_id": req or cid}
    verdict = guard.pre_tool_call(tool_name=tool, args=args, **kw)
    if verdict:
        return verdict["message"]
    return None, kw


def done(tool, kw, ok=True):
    guard.post_tool_call(
        tool_name=tool, result=json.dumps({"success": ok}), status=None if ok else "error", **kw
    )


def view(session="A", file_path=None):
    _, kw = call("skill_view", {"name": SKILL, **({"file_path": file_path} if file_path else {})}, session)
    done("skill_view", kw)


def rewrite(env, text, session="A"):
    r = call("skill_manage", {"operations": [{"action": "patch", "name": SKILL, "content": text}]}, session)
    if isinstance(r, str):
        return r
    env["md"].write_text(text, encoding="utf-8")  # what Hermes would do
    done("skill_manage", r[1])
    return None


def test_stale_full_rewrite_is_refused(env):
    view("A")
    view("B")
    assert rewrite(env, skill_md(SKILL, "Foo.", ["from B"]), "B") is None
    msg = rewrite(env, skill_md(SKILL, "Foo.", ["from A, stale"]), "A")
    assert "nothing was written" in msg and "changed since this session last read it" in msg
    assert "from B" in env["md"].read_text(encoding="utf-8")
    view("A")
    assert rewrite(env, skill_md(SKILL, "Foo.", ["from A, fresh"]), "A") is None


def test_rewrite_without_any_read_is_refused(env):
    assert "has not read it" in rewrite(env, "x", "A")


def test_own_writes_do_not_need_a_re_read(env):
    view("A")
    for i in range(3):
        assert rewrite(env, skill_md(SKILL, "Foo.", [f"v{i}"]), "A") is None


def test_exact_patch_passes_and_fuzzy_stale_patch_is_refused(env):
    view("A")
    env["md"].write_text(env["md"].read_text(encoding="utf-8").replace("two", "TWO by B"), encoding="utf-8")
    ok = call(
        "skill_manage", {"action": "patch", "name": SKILL, "old_string": "one", "new_string": "uno"}, "A"
    )
    assert not isinstance(ok, str)
    bad = call(
        "skill_manage", {"action": "patch", "name": SKILL, "old_string": "two", "new_string": "dos"}, "A"
    )
    assert "does not occur exactly" in bad


def test_same_response_read_does_not_count(env):
    _, kw = call("skill_view", {"name": SKILL}, "A", req="R1")
    done("skill_view", kw)
    r = call("skill_manage", {"action": "edit", "name": SKILL, "content": "x"}, "A", req="R1")
    assert isinstance(r, str)


def test_supporting_files_are_tracked_separately(env):
    view("A")  # SKILL.md only
    r = call(
        "skill_manage",
        {"action": "write_file", "name": SKILL, "file_path": "references/r.md", "file_content": "new\n"},
        "A",
    )
    assert isinstance(r, str)
    view("A", "references/r.md")
    r = call(
        "skill_manage",
        {"action": "write_file", "name": SKILL, "file_path": "references/r.md", "file_content": "new\n"},
        "A",
    )
    assert not isinstance(r, str)
    new = call(
        "skill_manage",
        {
            "action": "write_file",
            "name": SKILL,
            "file_path": "references/brand-new.md",
            "file_content": "n\n",
        },
        "A",
    )
    assert not isinstance(new, str)  # a new file needs no read


def test_generic_write_file_inside_the_shared_root(env):
    r = call("write_file", {"path": str(env["md"]), "content": "x"}, "A")
    assert isinstance(r, str)
    _, kw = call("read_file", {"path": str(env["md"])}, "A")
    done("read_file", kw)
    assert not isinstance(call("write_file", {"path": str(env["md"]), "content": "x"}, "A"), str)


def test_partial_read_file_does_not_count(env):
    _, kw = call("read_file", {"path": str(env["md"]), "offset": 3}, "A")
    done("read_file", kw)
    assert isinstance(call("write_file", {"path": str(env["md"]), "content": "x"}, "A"), str)


def test_files_outside_the_shared_root_are_ignored(env, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")
    assert call("write_file", {"path": str(outside), "content": "y"}, "A")[0] is None


def test_warn_logs_and_lets_it_through(env):
    cons.write_settings(env["state"], cons.Settings(guard="warn"))
    assert not isinstance(rewrite(env, "stale", "A"), str)
    entry = json.loads((env["state"] / "guard.log").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["level"] == "warn" and entry["tool"] == "skill_manage"


def test_off_and_per_profile_overrides_switch_live(env):
    cons.write_settings(env["state"], cons.Settings(guard="off", guard_overrides={"dev": "strict"}))
    assert isinstance(rewrite(env, "x", "A"), str)
    env["profile"]["name"] = "other"
    assert rewrite(env, "x", "A") is None


def test_failed_write_is_not_remembered(env):
    view("A")
    before = env["md"].read_text(encoding="utf-8")
    r = call("skill_manage", {"action": "edit", "name": SKILL, "content": "never landed"}, "A")
    done("skill_manage", r[1], ok=False)
    env["md"].write_text(before, encoding="utf-8")
    assert rewrite(env, "fresh", "A") is None


def test_guard_errors_let_the_call_through(env, monkeypatch):
    monkeypatch.setattr(ha, "locate", lambda name: (_ for _ in ()).throw(RuntimeError("boom")))
    assert guard.pre_tool_call(tool_name="skill_manage", args={"action": "edit", "name": SKILL}) is None


def test_other_tools_are_not_touched(env, monkeypatch):
    monkeypatch.setattr(guard, "_pre", lambda *a: pytest.fail("terminal must not be inspected"))
    assert guard.pre_tool_call(tool_name="terminal", args={"command": "ls"}) is None


# ------------------------------------------------------------------ registration


class Ctx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, cb, **kw):
        self.hooks.append(name)

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_default_registers_no_tool_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HERMES_ROOT", str(tmp_path))
    ctx = Ctx()
    commons.register(ctx)
    assert ctx.hooks == ["on_skill_lifecycle", "on_session_start"]


@pytest.mark.parametrize(
    ("data", "wanted"),
    [
        ({"lock": "shared"}, False),
        ({"guard": "off", "guard_overrides": {"p": "off"}}, False),
        ({"guard": "warn"}, True),
        ({"guard": "off", "guard_overrides": {"p": "strict"}}, True),
    ],
)
def test_guard_hooks_register_only_when_some_profile_runs_it(tmp_path, monkeypatch, data, wanted):
    monkeypatch.setenv("COMMONS_HERMES_ROOT", str(tmp_path))
    (tmp_path / "commons").mkdir()
    (tmp_path / "commons" / "consistency.json").write_text(json.dumps(data), encoding="utf-8")
    ctx = Ctx()
    commons.register(ctx)
    assert ("pre_tool_call" in ctx.hooks) is wanted and ("post_tool_call" in ctx.hooks) is wanted
