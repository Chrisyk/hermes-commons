"""`hermes commons ...` and `/commons ...`, driven against the classic fleet with the adapter faked."""

from __future__ import annotations

import argparse
import io
import json

import pytest

import commons
from commons import cli
from commons import hermes_adapter as ha
from commons.core.backup import list_backups, tree_sha256
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.layout import Layout
from tests.conftest import PROFILES
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def env(classic_fleet, monkeypatch):
    root = classic_fleet
    state = root / "commons"

    def layout(profiles, shared_root=None):
        return Layout(shared_root=shared_root or root / "shared", state_dir=state)

    monkeypatch.setattr(ha, "fleet", lambda: discover(root))
    monkeypatch.setattr(ha, "layout", layout)
    monkeypatch.setattr(ha, "default_root", lambda: root)
    monkeypatch.setattr(ha, "state_dir", lambda: state)
    monkeypatch.setattr(ha, "md5_dir_hash", dir_hash_md5)
    monkeypatch.setattr(ha, "hub_hash", hub_content_hash)
    monkeypatch.setattr(ha, "bundled_index", dict)
    monkeypatch.setattr(ha, "active_profile_name", lambda: "default")
    monkeypatch.setattr(ha, "HermesCliWriter", lambda **kw: YamlFileWriter())
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # no TTY unless a test says so
    monkeypatch.setattr(cli, "home_dir", lambda: root)
    monkeypatch.delenv(ha.ROOT_ENV, raising=False)
    return {"root": root, "state": state, "shared": root / "shared", "layout": layout(None)}


def run(*argv: str) -> int:
    parser = argparse.ArgumentParser(prog="hermes commons")
    cli.setup_fn(parser)
    args = parser.parse_args(list(argv))
    return cli.handler_fn(args)


def snapshot(root):
    return {
        p: (
            tree_sha256(profile_home(root, p) / "skills"),
            (profile_home(root, p) / "config.yaml").read_bytes(),
        )
        for p in PROFILES
    }


def applied(env):
    assert run("setup") == 0
    assert run("setup", "--apply", "--yes") == 0


# ------------------------------------------------------------------ setup


def test_setup_prints_and_saves_the_proposal_and_changes_nothing(env, capsys):
    before = snapshot(env["root"])
    assert run("setup") == 0
    out = capsys.readouterr().out
    assert "## What next" in out and "setup --apply" in out
    assert "1. review" in out and "2. apply" in out and "3. cancel" in out
    saved = sorted((env["state"] / "proposals").glob("*.json"))
    assert len(saved) == 1 and saved[0].with_suffix(".md").is_file()
    assert snapshot(env["root"]) == before
    assert not env["shared"].exists()


def test_setup_choose_changes_the_saved_proposal(env, capsys):
    assert run("setup", "--choose", "conflict=keep:device") == 0
    saved = max((env["state"] / "proposals").glob("*.json"))
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert "conflict" in {m["name"] for m in data["moves"]}
    assert run("setup", "--choose", "nope=merge") == 1
    assert "no conflict named" in capsys.readouterr().err


def test_setup_apply_needs_confirmation_without_a_tty(env, capsys):
    run("setup")
    before = snapshot(env["root"])
    assert run("setup", "--apply") == 2
    assert "needs confirmation; rerun with --yes" in capsys.readouterr().err
    assert snapshot(env["root"]) == before


def test_setup_apply_with_yes_applies_and_reports(env, capsys):
    run("setup")
    capsys.readouterr()
    assert run("setup", "--apply", "--yes") == 0
    out = capsys.readouterr().out
    assert "backup" in out and "verify" in out and "no problems" in out
    for p in discover(env["root"]):
        assert p.create_dir() is not None
        assert "commons" in p.plugins_enabled()
    assert (env["shared"] / "general" / "same" / "SKILL.md").is_file()
    assert "hourly" in out  # the scheduled check is offered, not created
    assert not (env["root"] / "scripts" / "commons-check.sh").exists()


def test_setup_apply_asks_on_a_tty_and_respects_no(env, monkeypatch, capsys):
    run("setup")
    before = snapshot(env["root"])
    monkeypatch.setattr("sys.stdin", FakeTTY("n\n"))
    assert run("setup", "--apply") == 1
    assert "nothing changed" in capsys.readouterr().out
    assert snapshot(env["root"]) == before


def test_setup_apply_without_a_saved_proposal(env, capsys):
    assert run("setup", "--apply", "--yes") == 1
    assert "run `hermes commons setup` first" in capsys.readouterr().err


def test_setup_apply_refuses_a_stale_proposal(env, capsys):
    run("setup")
    md = profile_home(env["root"], "device") / "skills" / "general" / "same" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    assert run("setup", "--apply", "--yes") == 1
    assert "changed since the proposal was made" in capsys.readouterr().out
    assert list_backups(env["layout"]) == []


def test_setup_resume_unknown_id(env, capsys):
    assert run("setup", "--resume", "nope", "--yes") == 1
    assert capsys.readouterr().err.startswith("commons:")


# ------------------------------------------------------------------ read-only commands


def test_status_and_only(env, capsys):
    assert run("status") == 0
    out = capsys.readouterr().out
    assert all(p in out for p in PROFILES)
    assert run("status", "--only", "device", "--skill", "same") == 0
    out = capsys.readouterr().out
    assert "device" in out and "pikvm" not in out and "same" in out


def test_check_reports_and_quiet(env, capsys):
    applied(env)
    capsys.readouterr()
    assert run("check") == 0
    assert "commons check" in capsys.readouterr().out
    assert run("check", "--quiet") == 0


def test_check_fix_safe_commits_without_asking(env, capsys):
    applied(env)
    md = env["shared"] / "general" / "same" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "edited by hand\n", encoding="utf-8")
    assert GitRepo(env["shared"]).status_porcelain()
    capsys.readouterr()
    assert run("check", "--fix-safe") == 0
    assert "committed" in capsys.readouterr().out
    assert GitRepo(env["shared"]).status_porcelain() == []


# ------------------------------------------------------------------ promote, demote


def test_promote_and_demote(env, capsys):
    applied(env)
    skills = profile_home(env["root"], "pikvm") / "skills"
    write_files(skills / "general" / "mine", {"SKILL.md": skill_md("mine", "Mine.", ["m"])})
    assert run("promote", "mine", "--from", "pikvm") == 2
    assert run("promote", "mine", "--from", "pikvm", "--yes") == 0
    assert (env["shared"] / "general" / "mine" / "SKILL.md").is_file()
    assert not (skills / "general" / "mine").exists()
    assert run("demote", "mine", "--to", "pocket", "--yes") == 0
    assert (profile_home(env["root"], "pocket") / "skills" / "general" / "mine" / "SKILL.md").is_file()
    assert run("promote", "nothing-here", "--yes") == 1
    assert capsys.readouterr().err.startswith("commons:")


# ------------------------------------------------------------------ merge


def test_merge_next_accept_and_skip(env, capsys):
    applied(env)
    capsys.readouterr()
    assert run("merge", "--next") == 0
    out = capsys.readouterr().out
    assert "bundled-edited" in out and "pikvm" in out and "pocket" in out
    assert run("merge", "--skip", "bundled-edited", "--yes") == 0
    assert run("merge", "--next", "--seed", "variant:default") == 0
    out = capsys.readouterr().out
    assert "conflict" in out
    staging = env["state"] / "staging" / "conflict"
    md = staging / "SKILL.md"
    assert md.is_file()
    assert run("merge", "--accept", "conflict", "--yes") == 1  # D_device_only is lost
    assert "D_device_only" in capsys.readouterr().out
    md.write_text(md.read_text(encoding="utf-8") + "D_device_only\n", encoding="utf-8")
    assert run("merge", "--accept", "conflict") == 2
    assert run("merge", "--accept", "conflict", "--yes") == 0
    merged = env["shared"] / "general" / "conflict" / "SKILL.md"
    assert "D_device_only" in merged.read_text(encoding="utf-8")
    assert run("merge", "--accept", "nope", "--yes") == 1


# ------------------------------------------------------------------ log, undo, restore


def test_log_and_undo(env, capsys):
    applied(env)
    md = env["shared"] / "general" / "same" / "SKILL.md"
    original = md.read_text(encoding="utf-8")
    md.write_text(original + "oops\n", encoding="utf-8")
    GitRepo(env["shared"]).commit_paths([md.parent], "edited same")
    capsys.readouterr()
    assert run("log", "same") == 0
    assert "edited same" in capsys.readouterr().out
    assert run("log") == 0
    assert run("undo", "same") == 2
    assert run("undo", "same", "--yes") == 0
    assert md.read_text(encoding="utf-8") == original


def test_restore_lists_then_restores(env, capsys):
    before = snapshot(env["root"])
    applied(env)
    capsys.readouterr()
    assert run("restore") == 0
    out = capsys.readouterr().out
    bid = list_backups(env["layout"])[0]
    assert bid in out and "setup" in out
    assert run("restore", bid) == 2
    assert run("restore", bid, "--yes") == 0
    assert "restored" in capsys.readouterr().out
    assert snapshot(env["root"]) == before
    assert run("restore") == 0
    assert "(restored)" in capsys.readouterr().out


# ------------------------------------------------------------------ slash command


def test_slash_allows_only_status_and_check(env):
    applied(env)
    assert "default" in cli.slash_handler("status")
    assert "default" in cli.slash_handler("")
    assert "commons check" in cli.slash_handler("check")
    for refused in ("check --fix-safe", "promote x --yes", "setup --apply --yes", "restore"):
        assert "only /commons status and /commons check" in cli.slash_handler(refused)
    assert "usage" in cli.slash_handler("status --bogus").lower()
    assert "unbalanced" in cli.slash_handler('status "x').lower() or "quotation" in cli.slash_handler(
        'status "x'
    )


# ------------------------------------------------------------------ register


class FakeCtx:
    def __init__(self):
        self.hooks, self.cli, self.commands, self.skills = [], [], [], []

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli.append((name, setup_fn, handler_fn))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append((name, handler, args_hint))

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, path, description))


def test_register_wires_hooks_cli_and_slash():
    ctx = FakeCtx()
    commons.register(ctx)
    assert {n for n, _ in ctx.hooks} == {"on_skill_lifecycle", "on_session_start"}
    assert [n for n, _s, _h in ctx.cli] == ["commons"]
    assert [(n, a) for n, _h, a in ctx.commands] == [("commons", "status|check")]


def test_registered_wrappers_reach_the_cli(env):
    ctx = FakeCtx()
    commons.register(ctx)
    _name, setup, handler = ctx.cli[0]
    parser = argparse.ArgumentParser()
    setup(parser)
    assert handler(parser.parse_args(["status"])) == 0
    assert "default" in ctx.commands[0][1]("status")
    hooks_by_name = dict(ctx.hooks)
    assert hooks_by_name["on_skill_lifecycle"](action="loaded", skill_name="x") is None


def test_register_imports_only_the_standard_library():
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "import sys\n"
        "sys.modules['yaml'] = None\n"
        "import commons\n"
        "class C:\n"
        "    def __getattr__(self, n):\n"
        "        return lambda *a, **k: None\n"
        "commons.register(C())\n"
        "bad = sorted(m for m in sys.modules if m.startswith('commons.'))\n"
        "assert not bad, bad\n"
    )
    repo = Path(commons.__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", probe], check=False, cwd=repo, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr


def test_check_without_a_bundled_index_still_runs(env, monkeypatch, capsys):
    def gone():
        raise ha.HermesApiChanged("tools.skill_usage.read_skill_name")

    monkeypatch.setattr(ha, "bundled_index", gone)
    applied(env)
    capsys.readouterr()
    assert run("check") == 0
    assert "commons check:" in capsys.readouterr().out


def test_writing_commands_without_yes_exit_2_and_change_nothing(env, capsys):
    applied(env)
    before = (snapshot(env["root"]), GitRepo(env["shared"]).log())
    for argv in (
        ("demote", "same", "--to", "pocket"),
        ("merge", "--skip", "bundled-edited"),
        ("undo", "same"),
        ("setup", "--schedule"),
    ):
        assert run(*argv) == 2, argv
        assert "needs confirmation; rerun with --yes" in capsys.readouterr().err
    assert (snapshot(env["root"]), GitRepo(env["shared"]).log()) == before


def test_missing_dependencies_give_one_clear_message(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "import argparse, sys\n"
        "sys.modules['yaml'] = None\n"
        "import commons\n"
        "p = argparse.ArgumentParser()\n"
        "commons.setup_fn(p)\n"
        "rc = commons.handler_fn(p.parse_args(['status', '--only', 'x']))\n"
        "print('RC', rc)\n"
        "print('SLASH', commons.slash_handler('status'))\n"
        "commons.on_skill_lifecycle(action='edited', skill_name='x')\n"
    )
    repo = Path(commons.__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", probe], check=False, cwd=repo, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "RC 1" in proc.stdout
    assert "SLASH commons cannot load its dependencies" in proc.stdout
    assert "hermes pm repair" in proc.stderr


def _merge_conflict(env):
    assert run("merge", "--skip", "bundled-edited", "--yes") == 0
    assert run("merge", "--next", "--seed", "variant:default") == 0
    md = env["state"] / "staging" / "conflict" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "D_device_only\n", encoding="utf-8")
    assert run("merge", "--accept", "conflict", "--yes") == 0


def test_restore_refuses_while_later_operations_are_not_restored(env, capsys):
    before = snapshot(env["root"])
    applied(env)
    setup_id = list_backups(env["layout"])[0]
    _merge_conflict(env)
    after_merge = snapshot(env["root"])
    capsys.readouterr()
    assert run("restore", setup_id, "--yes") == 1
    err = capsys.readouterr().err
    assert "later commons operations" in err and "--cascade" in err
    assert "merge-conflict" in err and "skip-bundled-edited" in err
    assert err.index("merge-conflict") < err.index("skip-bundled-edited")  # newest first
    assert snapshot(env["root"]) == after_merge  # nothing changed
    assert run("restore", setup_id, "--cascade") == 2  # still asks
    assert run("restore", setup_id, "--cascade", "--yes") == 0
    out = capsys.readouterr().out
    assert out.index("restored") < out.index(f"restored {setup_id}")
    assert snapshot(env["root"]) == before  # byte-identical skills trees and config.yaml
    assert run("restore") == 0
    rows = capsys.readouterr().out
    assert rows.count("(restored)") == 3


def test_restore_cascade_core_order(env):
    from commons.core.restore import RestoreError, later_operations, plan_restore, restore, restore_cascade

    applied(env)
    lay = env["layout"]
    setup_id = list_backups(lay)[0]
    _merge_conflict(env)
    later = later_operations(lay, setup_id)
    assert [b.split("-", 1)[1] for b in later] == ["merge-conflict", "skip-bundled-edited"]
    with pytest.raises(RestoreError, match="later commons operations"):
        restore(plan_restore(lay, setup_id), discover(env["root"]), YamlFileWriter())
    # the newest one on its own is fine, then the next
    restore(plan_restore(lay, later[0]), discover(env["root"]), YamlFileWriter())
    assert later_operations(lay, setup_id) == later[1:]
    res = restore_cascade(lay, setup_id, discover(env["root"]), YamlFileWriter())
    assert [bid for bid, _r in res.restored] == [later[1], setup_id]


# ------------------------------------------------------------------ one module identity

_IDENTITY_PROBE = """
import importlib, importlib.util, sys, types
repo, site = sys.argv[1], sys.argv[2]
if site:
    sys.path.append(site)  # PyYAML only: with -S the editable install of commons is not active
ns = types.ModuleType("hermes_plugins"); ns.__path__ = []; sys.modules["hermes_plugins"] = ns
# As hermes_cli/plugins_loader.py _load_directory_module does it.
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.commons", repo + "/__init__.py", submodule_search_locations=[repo])
mod = importlib.util.module_from_spec(spec)
mod.__package__ = spec.name
mod.__path__ = [repo]
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
import commons.core.configio as direct
from commons import hermes_adapter
from commons.core.configio import ConfigError
assert direct.ConfigError is ConfigError is hermes_adapter.ConfigError
for name in ("hermes_plugins.commons.commons.core.configio", "hermes_plugins.commons.commons"):
    try:
        other = importlib.import_module(name)
    except ImportError:
        continue
    assert getattr(other, "ConfigError", ConfigError) is ConfigError, name
copies = sorted(k for k in sys.modules if k.endswith("configio"))
assert copies == ["commons.core.configio"], copies
print("OK", sys.modules["commons"].__file__)
"""


@pytest.mark.parametrize("installed", [True, False])
def test_config_error_has_one_identity_however_imported(tmp_path, installed):
    import subprocess
    import sys
    import sysconfig
    from pathlib import Path

    repo = Path(commons.__file__).resolve().parent.parent
    probe = tmp_path / "probe.py"
    probe.write_text(_IDENTITY_PROBE, encoding="utf-8")
    flags = ["-I"] if installed else ["-I", "-S"]
    site = "" if installed else sysconfig.get_paths()["purelib"]
    proc = subprocess.run(
        [sys.executable, *flags, str(probe), str(repo), site],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("OK") and str(repo / "commons" / "__init__.py") in proc.stdout


# ------------------------------------------------------------------ minor refusals


def test_schedule_with_choose_is_an_error(env, capsys):
    run("setup")
    capsys.readouterr()
    assert run("setup", "--schedule", "--choose", "conflict=keep:default", "--yes") == 2
    assert "--schedule cannot be combined with --choose" in capsys.readouterr().err
    assert not (env["root"] / "scripts").exists()


def test_schedule_is_refused_under_the_root_override(env, monkeypatch, capsys):
    monkeypatch.setenv(ha.ROOT_ENV, str(env["root"]))
    before = snapshot(env["root"])
    assert run("setup", "--schedule", "--yes") == 1
    assert "cron scheduling is refused" in capsys.readouterr().err
    run("setup")
    assert run("setup", "--apply", "--yes", "--schedule") == 1
    assert snapshot(env["root"]) == before  # refused before anything was applied
    assert not (env["root"] / "scripts").exists()
    with pytest.raises(RuntimeError, match="COMMONS_HERMES_ROOT"):
        cli.schedule_check(env["root"], command=lambda a: ["hermes", *a], runner=lambda *a, **k: None)


def test_undo_with_no_commits_fails_before_asking(env, monkeypatch, capsys):
    applied(env)
    capsys.readouterr()
    write_files(env["shared"] / "general" / "fresh", {"SKILL.md": skill_md("fresh", "Uncommitted.", ["x"])})
    monkeypatch.setattr("sys.stdin", FakeTTY("y\n"))
    assert run("undo", "fresh") == 1
    io_ = capsys.readouterr()
    assert "has no commits" in io_.err and "[y/N]" not in io_.out
