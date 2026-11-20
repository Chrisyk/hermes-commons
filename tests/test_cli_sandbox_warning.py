"""The CLI warns on every run while COMMONS_HERMES_ROOT is set."""

import argparse
import io

from commons import cli


def test_warns_when_sandbox_root_set(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMONS_HERMES_ROOT", str(tmp_path))
    monkeypatch.setitem(cli.COMMANDS, "status", lambda args, out, err: 0)
    err = io.StringIO()
    assert cli.run_command(argparse.Namespace(commons_command="status"), io.StringIO(), err) == 0
    assert "COMMONS_HERMES_ROOT is set" in err.getvalue()


def test_silent_without_sandbox_root(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "status", lambda args, out, err: 0)
    err = io.StringIO()
    cli.run_command(argparse.Namespace(commons_command="status"), io.StringIO(), err)
    assert err.getvalue() == ""
