"""Consistency settings and the .locks switch (commons.core.consistency)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from commons.core import consistency as cons
from commons.core import filelock
from commons.core.fleet import discover


@pytest.fixture
def four(tmp_path):
    root = tmp_path / "h"
    for home in (root, *(root / "profiles" / n for n in ("device", "pikvm", "pocket"))):
        (home / "skills" / ".locks").mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
        (home / "skills" / ".locks" / "old.lock").write_text("", encoding="utf-8")
    return {"root": root, "state": root / "commons", "profiles": discover(root)}


def test_missing_file_means_lock_own_and_guard_off(tmp_path):
    s = cons.read_settings(tmp_path)
    assert (s.lock, s.guard, s.exists, s.guard_anywhere()) == ("own", "off", False, False)


def test_bad_values_fall_back_and_are_reported(tmp_path):
    cons.settings_path(tmp_path).write_text(
        json.dumps({"lock": "both", "guard": "max", "guard_overrides": {"x": "loud"}}), encoding="utf-8"
    )
    s = cons.read_settings(tmp_path)
    assert (s.lock, s.guard, dict(s.guard_overrides)) == ("own", "off", {})
    assert len(s.problems) == 3


def test_unreadable_file_gives_defaults_with_a_problem(tmp_path):
    cons.settings_path(tmp_path).write_text("{not json", encoding="utf-8")
    s = cons.read_settings(tmp_path)
    assert s.lock == "own" and s.problems
    with pytest.raises(ValueError):
        cons.read_settings_strict(tmp_path)


def test_write_then_read_round_trips(tmp_path):
    cons.write_settings(tmp_path, cons.Settings("shared", "warn", {"pocket": "strict"}))
    s = cons.read_settings(tmp_path)
    assert (s.lock, s.guard, s.guard_for("pocket"), s.guard_for("device")) == (
        "shared",
        "warn",
        "strict",
        "warn",
    )


def test_override_alone_turns_the_guard_on():
    assert cons.Settings(guard_overrides={"pocket": "warn"}).guard_anywhere()
    assert not cons.Settings(guard_overrides={"pocket": "off"}).guard_anywhere()


def _linked(profiles, target):
    return all(cons.link_state(p).points_to(target) for p in profiles)


def test_shared_links_every_profile_and_backs_up_the_old_folders(four):
    res = cons.switch_locks(four["profiles"], four["state"], "shared")
    assert sorted(res.linked) == ["default", "device", "pikvm", "pocket"]
    assert _linked(four["profiles"], cons.lock_target(four["state"], "", "shared"))
    kept = sorted(p.name for p in res.backup.iterdir())
    assert kept == ["default.locks", "device.locks", "pikvm.locks", "pocket.locks"]
    assert all((res.backup / k / "old.lock").is_file() for k in kept)  # moved, not deleted
    assert cons.lock_findings(four["profiles"], four["state"], cons.Settings("shared")) == []


def test_switching_again_is_a_no_op(four):
    cons.switch_locks(four["profiles"], four["state"], "shared")
    again = cons.switch_locks(four["profiles"], four["state"], "shared")
    assert again.linked == [] and again.backup is None


def test_own_after_shared_gives_each_profile_its_own_folder_via_a_link(four):
    cons.switch_locks(four["profiles"], four["state"], "shared")
    res = cons.switch_locks(four["profiles"], four["state"], "own")
    assert res.backup is None  # link over link: nothing to move
    for p in four["profiles"]:
        st = cons.link_state(p)
        assert st.kind == "link" and st.points_to(cons.lock_target(four["state"], p.name, "own"))
    assert cons.lock_findings(four["profiles"], four["state"], cons.Settings("own")) == []


def test_own_on_a_fresh_install_touches_nothing(four):
    res = cons.switch_locks(four["profiles"], four["state"], "own")
    assert res.linked == [] and res.backup is None
    assert all(cons.link_state(p).kind == "dir" for p in four["profiles"])


def test_a_held_lock_aborts_the_switch_and_changes_nothing(four):
    held = four["root"] / "profiles" / "device" / "skills" / ".locks" / "old.lock"
    fd = os.open(held, os.O_RDWR | os.O_CREAT | filelock.O_BINARY)
    try:
        assert filelock.try_lock(fd, 0)
        with pytest.raises(cons.ConsistencyError, match="in progress"):
            cons.switch_locks(four["profiles"], four["state"], "shared")
    finally:
        os.close(fd)
    assert all(cons.link_state(p).kind == "dir" for p in four["profiles"])


def test_hermes_recreating_locks_in_the_gap_is_retried(four):
    calls = []

    def recreate(path: Path):
        if len(calls) < 2:
            calls.append(path)
            path.mkdir()  # what Hermes' skill_file_lock does on demand
            (path / "fresh.lock").write_text("", encoding="utf-8")

    res = cons.switch_locks(four["profiles"], four["state"], "shared", after_set_aside=recreate)
    assert len(calls) == 2
    assert _linked(four["profiles"], cons.lock_target(four["state"], "", "shared"))
    assert len(res.set_aside) == 4 + 2  # the recreated folders were moved too, never deleted


def test_unshared_profile_is_reported(four):
    cons.switch_locks(four["profiles"], four["state"], "shared")
    pocket = four["root"] / "profiles" / "pocket" / "skills" / ".locks"
    os.unlink(pocket)
    pocket.mkdir()
    found = cons.lock_findings(four["profiles"], four["state"], cons.Settings("shared"))
    assert [(c, p) for c, p, _m in found] == [("locks-unshared", "pocket")]


def test_a_file_where_locks_should_be_refuses(four):
    stray = four["root"] / "profiles" / "pikvm" / "skills" / ".locks"
    for f in stray.iterdir():
        f.unlink()
    stray.rmdir()
    stray.write_text("not a folder", encoding="utf-8")
    with pytest.raises(cons.ConsistencyError, match="neither a folder"):
        cons.switch_locks(four["profiles"], four["state"], "shared")
