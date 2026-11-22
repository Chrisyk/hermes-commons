"""Tests for commons.core.apply: carrying out a setup proposal with a journal."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from commons.core import apply as apply_mod
from commons.core.apply import ApplyResult, apply, intents, load_journal
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.layout import Layout
from commons.core.proposal import apply_choice, build_proposal
from commons.core.resolve import TIER_CREATE_DIR, TIER_LOCAL, resolve
from tests.conftest import PROFILES
from tests.fleet import dir_hash_md5, hub_content_hash, skill_md, write_files

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def layout_for(root: Path) -> Layout:
    return Layout(shared_root=root / "shared", state_dir=root / "commons")


def propose(root: Path, cron=None):
    return build_proposal(
        discover(root), layout_for(root), dir_hash_md5, hub_content_hash, cron or {}, home=root
    )


def run_apply(root: Path, prop=None) -> ApplyResult:
    prop = prop or propose(root)
    return apply(prop, discover(root), layout_for(root), YamlFileWriter(), NOW, home=root)


def names_by_profile(root: Path) -> dict[str, dict]:
    return {p.name: resolve(p, dir_hash_md5, hub_content_hash) for p in discover(root)}


def test_apply_classic_fleet(classic_fleet):
    root = classic_fleet
    before = names_by_profile(root)
    originals = {
        (p, rel): {f.relative_to(d): f.read_bytes() for f in d.rglob("*") if f.is_file()}
        for p in PROFILES
        for rel in ("general/same", "general/contained")
        for d in [(root if p == "default" else root / "profiles" / p) / "skills" / rel]
    }
    result = run_apply(root)
    assert result.error is None, result.error
    lay = layout_for(root)
    shared = lay.shared_root
    after = names_by_profile(root)
    for profile in PROFILES:
        res = after[profile]
        assert set(before[profile]) <= set(res), profile  # no capability loss
        for name in ("same", "contained"):
            r = res[name]
            assert r.winner_tier == TIER_CREATE_DIR, (profile, name)
            assert r.winner_path == shared / "general" / name / "SKILL.md"
            assert r.shadowed == [] and r.ambiguous == []
    for profile in ("default", "device"):
        r = after[profile]["conflict"]
        assert r.winner_tier == TIER_LOCAL
    # the bigger contained copy (device) is what landed
    assert "contained line 6" in (shared / "general/contained/SKILL.md").read_text(encoding="utf-8")
    for p in discover(root):
        assert p.create_dir() == shared
        assert "commons" in p.plugins_enabled()
    bid = result.backup_id
    retired = lay.backups_dir / bid / "retired"
    for (profile, rel), files in originals.items():
        dest = retired / profile / rel
        assert {f.relative_to(dest): f.read_bytes() for f in dest.rglob("*") if f.is_file()} == files
    journal = load_journal(lay, bid)
    assert journal["complete"] is True and journal["error"] is None and journal["problems"] == []
    assert len(intents(journal, "retire")) == 8
    assert all(i["state"] == "done" for i in intents(journal))
    assert journal["steps"] == [
        "backup",
        "git",
        "copy",
        "commit",
        "config",
        "retire",
        "paths",
        "commons-config",
        "verify",
    ]
    repo = GitRepo(shared)
    subjects = [s for _, _, s in repo.log()]
    assert subjects == ["commons: record setup", "commons setup 2026-11-20T12:00:00Z"]
    assert repo.status_porcelain() == []
    config = json.loads((shared / ".commons" / "config.json").read_text(encoding="utf-8"))
    assert config == {"profiles": list(PROFILES), "taken_over": [], "intentional_overrides": []}
    assert result.steps_done == journal["steps"]


def test_journal_records_config_intents(classic_fleet):
    result = run_apply(classic_fleet)
    journal = load_journal(layout_for(classic_fleet), result.backup_id)
    for p in discover(classic_fleet):
        before = journal["profiles"][p.name]["config_before_sha256"]
        mine = [
            i for i in intents(journal) if i["args"].get("profile") == p.name and i["op"].startswith("config")
        ]
        assert [i["op"] for i in mine] == ["config_set", "config_list_add"]
        cset, cadd = (i["args"] for i in mine)
        assert cset["old_value_present"] is False and cset["old_value"] is None
        assert cadd["was_present"] is False and cadd["item"] == "commons"
        assert before != cset["config_sha256_after"] != cadd["config_sha256_after"]
        retire = intents(journal, "retire")[0]["args"]
        assert retire["fingerprint"] and Path(retire["dst"]).is_dir()


def test_forced_failure_at_second_retire(classic_fleet, monkeypatch):
    real = apply_mod.retire
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("disk on fire")
        return real(*args, **kwargs)

    monkeypatch.setattr(apply_mod, "retire", flaky)
    result = run_apply(classic_fleet)
    assert result.error and "disk on fire" in result.error
    journal = load_journal(layout_for(classic_fleet), result.backup_id)
    assert journal["steps"] == result.steps_done == ["backup", "git", "copy", "commit", "config"]
    retires = intents(journal, "retire")
    assert [i["state"] for i in retires] == ["done", "pending"]
    assert journal["complete"] is False and "disk on fire" in journal["error"]
    first = retires[0]["args"]
    assert Path(first["dst"]).is_dir()
    assert not Path(first["src"]).exists()
    assert Path(retires[1]["args"]["src"]).is_dir()


def test_keep_choice_moves_conflict(classic_fleet):
    prop = apply_choice(propose(classic_fleet), "conflict=keep:device")
    assert run_apply(classic_fleet, prop).error is None
    shared = classic_fleet / "shared"
    assert "D_device_only" in (shared / "general/conflict/SKILL.md").read_text(encoding="utf-8")
    for profile in ("default", "device"):
        assert names_by_profile(classic_fleet)[profile]["conflict"].winner_tier == TIER_CREATE_DIR


def test_existing_different_target_fails_before_any_retire(classic_fleet):
    prop = propose(classic_fleet)
    write_files(classic_fleet / "shared" / "general" / "same", {"SKILL.md": skill_md("other", "d", ["x"])})
    result = run_apply(classic_fleet, prop)
    assert result.error and "different content" in result.error
    assert result.steps_done == ["backup", "git"]
    journal = load_journal(layout_for(classic_fleet), result.backup_id)
    assert not intents(journal, "retire") and not intents(journal, "config_set")
    assert (classic_fleet / "skills" / "general" / "same").is_dir()


def test_identical_existing_target_is_skipped(classic_fleet):
    prop = propose(classic_fleet)
    src = classic_fleet / "skills" / "general" / "same"
    write_files(
        classic_fleet / "shared" / "general" / "same",
        {f.name: f.read_bytes() for f in src.iterdir() if f.is_file()},
    )
    result = run_apply(classic_fleet, prop)
    assert result.error is None
    journal = load_journal(layout_for(classic_fleet), result.backup_id)
    assert [c["args"]["skipped"] for c in intents(journal, "copy") if c["args"]["name"] == "same"] == [True]


def test_uncommitted_shared_state_is_snapshotted_first(classic_fleet):
    shared = classic_fleet / "shared"
    write_files(shared / "lib" / "own", {"SKILL.md": skill_md("own", "d", ["mine"])})
    repo = GitRepo(shared)
    repo.init()
    repo.commit_all("first")
    (shared / "lib" / "own" / "SKILL.md").write_text(skill_md("own", "d", ["edited"]), encoding="utf-8")
    result = run_apply(classic_fleet)
    assert result.error is None
    subjects = [s for _, _, s in repo.log()]
    assert subjects[-2:] == ["commons: snapshot before setup", "first"]
    journal = load_journal(layout_for(classic_fleet), result.backup_id)
    assert journal["git"]["snapshot_sha"] and journal["git"]["base_sha"] == journal["git"]["snapshot_sha"]


def test_stale_proposal_is_refused(classic_fleet):
    prop = propose(classic_fleet)
    (classic_fleet / "profiles" / "pocket" / "skills" / "general" / "same" / "notes.txt").write_text(
        "changed\n", encoding="utf-8"
    )
    result = run_apply(classic_fleet, prop)
    assert result.error and "changed since the proposal" in result.error
    assert result.backup_id is None and result.steps_done == []


@pytest.mark.skipif(sys.platform == "win32", reason="the test builds POSIX-style mixed-separator path strings")
def test_cron_and_reference_rewrites(classic_fleet):
    root = classic_fleet
    old = root / "profiles" / "pikvm" / "skills" / "general" / "same"
    jobs_path = root / "profiles" / "pikvm" / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(
        json.dumps({"jobs": [{"id": "j1", "prompt": f"cat {old}/notes.txt"}]}), encoding="utf-8"
    )
    before = jobs_path.read_bytes()
    write_files(
        root / "profiles" / "device" / "skills" / "general" / "contained", {"references/r.md": f"{old}\n"}
    )
    # device's contained copy changed: rebuild so it is still the kept copy
    result = run_apply(root)
    assert result.error is None, result.error
    shared = root / "shared"
    prompt = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"][0]["prompt"]
    assert prompt == f"cat {shared}/general/same/notes.txt"
    assert (shared / "general/contained/references/r.md").read_text(
        encoding="utf-8"
    ) == f"{shared}/general/same\n"
    journal = load_journal(layout_for(root), result.backup_id)
    cron = intents(journal, "cron_write")
    assert [c["args"]["profile"] for c in cron] == ["pikvm"]
    assert Path(cron[0]["args"]["backup_copy"]).read_bytes() == before
    assert len(intents(journal, "md_write")) == 1
    assert GitRepo(shared).status_porcelain() == []


def test_proposal_for_other_root_is_refused(classic_fleet, tmp_path):
    prop = propose(classic_fleet)
    lay = Layout(classic_fleet / "elsewhere", classic_fleet / "commons")
    result = apply(prop, discover(classic_fleet), lay, YamlFileWriter(), NOW)
    assert result.error and result.steps_done == []


@pytest.mark.parametrize("missing", ["device"])
def test_missing_profile_is_refused(classic_fleet, missing):
    prop = propose(classic_fleet)
    profiles = [p for p in discover(classic_fleet) if p.name != missing]
    result = apply(prop, profiles, layout_for(classic_fleet), YamlFileWriter(), NOW)
    assert result.error and "missing" in result.error
