"""The scheduled check offer: the script it writes and the `hermes cron create` command it runs."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys

import pytest

from commons import cli
from tests.test_cli import FakeTTY, applied, env, run  # noqa: F401 - env is a fixture


def fake_command(args):
    return ["/opt/hermes/bin/hermes", *args]


class Runner:
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)


@pytest.mark.skipif(sys.platform == "win32", reason="covers the POSIX .sh script; Windows writes a .py script")
def test_script_text_and_mode(tmp_path):
    path = cli.write_check_script(tmp_path, fake_command(["commons", "check", "--fix-safe", "--quiet"]))
    assert path == tmp_path / "scripts" / "commons-check.sh"
    assert path.read_text(encoding="utf-8") == (
        "#!/bin/sh\nexec /opt/hermes/bin/hermes commons check --fix-safe --quiet\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


@pytest.mark.skipif(sys.platform == "win32", reason="covers the POSIX .sh script; Windows writes a .py script")
def test_script_quotes_a_launcher_path_with_spaces(tmp_path):
    path = cli.write_check_script(tmp_path, ["/my apps/hermes", "commons", "check"])
    assert path.read_text(encoding="utf-8").splitlines()[1] == "exec '/my apps/hermes' commons check"


@pytest.mark.skipif(sys.platform == "win32", reason="covers the POSIX .sh script; Windows writes a .py script")
def test_cron_create_command_line(tmp_path):
    runner = Runner()
    msg = cli.schedule_check(tmp_path, command=fake_command, runner=runner)
    assert len(runner.calls) == 1
    argv, kw = runner.calls[0]
    assert argv == [
        "/opt/hermes/bin/hermes",
        "-p",
        "default",
        "cron",
        "create",
        "0 * * * *",
        "--script",
        "commons-check.sh",
        "--name",
        "commons hourly check",
        "--no-agent",
    ]
    assert kw["env"]["HERMES_HOME"] == str(tmp_path)
    assert kw["timeout"] == cli.CRON_TIMEOUT and kw["check"] is False
    assert (tmp_path / "scripts" / "commons-check.sh").is_file()
    assert "created" in msg


def test_existing_job_is_not_created_twice(tmp_path):
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"name": cli.CRON_NAME}]}))
    runner = Runner()
    assert "already exists" in cli.schedule_check(tmp_path, command=fake_command, runner=runner)
    assert runner.calls == []
    assert not (tmp_path / "scripts").exists()


def test_cron_failure_is_reported(tmp_path):
    with pytest.raises(RuntimeError, match="exit 1.*no such option"):
        cli.schedule_check(tmp_path, command=fake_command, runner=Runner(1, "no such option"))


def test_cron_timeout_is_reported(tmp_path):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    with pytest.raises(RuntimeError, match="timed out"):
        cli.schedule_check(tmp_path, command=fake_command, runner=slow)


# ------------------------------------------------------------------ the offer at the end of setup


@pytest.fixture
def scheduled(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "schedule_check", lambda root, **kw: calls.append(root) or "scheduled it")
    return calls


def test_offer_accepted_on_a_tty(env, scheduled, monkeypatch, capsys):  # noqa: F811
    run("setup")
    monkeypatch.setattr("sys.stdin", FakeTTY("y\ny\n"))
    assert run("setup", "--apply") == 0
    assert scheduled == [env["root"]]
    assert "scheduled it" in capsys.readouterr().out


def test_offer_declined_on_a_tty(env, scheduled, monkeypatch, capsys):  # noqa: F811
    run("setup")
    monkeypatch.setattr("sys.stdin", FakeTTY("y\nn\n"))
    assert run("setup", "--apply") == 0
    assert scheduled == []
    assert "hourly check not created" in capsys.readouterr().out


def test_yes_never_creates_a_job_unasked(env, scheduled, capsys):  # noqa: F811
    run("setup")
    assert run("setup", "--apply", "--yes") == 0
    assert scheduled == []
    assert "setup --schedule --yes" in capsys.readouterr().out


def test_schedule_flag_with_apply_creates_it(env, scheduled):  # noqa: F811
    run("setup")
    assert run("setup", "--apply", "--yes", "--schedule") == 0
    assert scheduled == [env["root"]]


def test_schedule_alone_asks_then_creates(env, scheduled, monkeypatch):  # noqa: F811
    assert run("setup", "--schedule") == 2
    assert scheduled == []
    monkeypatch.setattr("sys.stdin", FakeTTY("y\n"))
    assert run("setup", "--schedule") == 0
    assert scheduled == [env["root"]]
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run("setup", "--schedule", "--yes") == 0
    assert len(scheduled) == 2


def test_no_script_is_written_by_a_plain_apply(env, capsys):  # noqa: F811
    applied(env)
    assert not (env["root"] / "scripts").exists()
    assert not os.path.exists(env["root"] / "cron" / "jobs.json") or cli.CRON_NAME not in (
        env["root"] / "cron" / "jobs.json"
    ).read_text(encoding="utf-8")
