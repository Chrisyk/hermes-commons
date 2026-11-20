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
    assert [(n, s, h) for n, s, h in ctx.cli] == [("commons", cli.setup_fn, cli.handler_fn)]
    assert [(n, h, a) for n, h, a in ctx.commands] == [("commons", cli.slash_handler, "status|check")]


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
