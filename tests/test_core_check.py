"""Tests for commons.core.check: drift findings and the safe fix."""

from __future__ import annotations

import json

import yaml

from commons.core.check import Finding, fix_safe, render_findings, run_check
from commons.core.fleet import discover
from commons.core.gitrepo import GitRepo
from commons.core.journal import operation
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home, skill_md, write_files
from tests.test_core_apply import layout_for, run_apply


def set_up(root):
    result = run_apply(root)
    assert result.error is None, result.error
    return layout_for(root)


def check(root, **kw):
    return run_check(discover(root), layout_for(root), dir_hash_md5, hub_content_hash, home=root, **kw)


def codes(findings, code=None):
    return [f for f in findings if code is None or f.code == code]


def edit_config(root, profile, fn):
    path = profile_home(root, profile) / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def shared_skill(lay, rel, files):
    write_files(lay.shared_root / rel, files)
    GitRepo(lay.shared_root).commit_paths([lay.shared_root / rel], f"add {rel}")


def test_clean_after_setup_has_only_known_findings(classic_fleet):
    set_up(classic_fleet)
    found = check(classic_fleet)
    assert {f.code for f in found if f.severity == "warn"} == {"ambiguous"}  # device's two dup copies
    assert [(f.profile, f.skill) for f in codes(found, "ambiguous")] == [("device", "dup")]


def test_plugin_off(classic_fleet):
    set_up(classic_fleet)
    edit_config(classic_fleet, "pikvm", lambda d: d["plugins"].update(enabled=[]))
    found = codes(check(classic_fleet), "plugin-off")
    assert [(f.profile, f.severity) for f in found] == [("pikvm", "warn")]


def test_create_dir_missing(classic_fleet):
    set_up(classic_fleet)
    edit_config(classic_fleet, "pocket", lambda d: d["skills"].pop("create_dir"))
    assert [f.profile for f in codes(check(classic_fleet), "create-dir-missing")] == ["pocket"]


def test_create_dir_elsewhere(classic_fleet):
    set_up(classic_fleet)
    other = classic_fleet / "other-skills"
    other.mkdir()
    edit_config(classic_fleet, "pocket", lambda d: d["skills"].update(create_dir=str(other)))
    found = codes(check(classic_fleet), "create-dir-elsewhere")
    assert [f.profile for f in found] == ["pocket"] and str(other) in found[0].message


def test_shadowed(classic_fleet):
    set_up(classic_fleet)
    write_files(
        profile_home(classic_fleet, "pikvm") / "skills" / "general" / "same",
        {"SKILL.md": skill_md("same", "Stray copy.", ["S1"])},
    )
    found = codes(check(classic_fleet), "shadowed")
    assert [(f.profile, f.skill, f.severity) for f in found] == [("pikvm", "same", "warn")]


def test_ambiguous_in_shared_root(classic_fleet):
    lay = set_up(classic_fleet)
    shared_skill(lay, "x/same", {"SKILL.md": skill_md("same", "Second shared copy.", ["other"])})
    found = codes(check(classic_fleet), "ambiguous")
    assert ("default", "same") in {(f.profile, f.skill) for f in found}


def test_uncommitted_and_fix_safe(classic_fleet):
    lay = set_up(classic_fleet)
    md = lay.shared_root / "general" / "same" / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8") + "edited by hand\n", encoding="utf-8")
    found = check(classic_fleet)
    assert [f.skill for f in codes(found, "uncommitted")] == [None]
    sha = fix_safe(found, lay)
    repo = GitRepo(lay.shared_root)
    assert sha == repo.head()
    assert repo.log(n=1)[0][2] == "commons: commit edits made outside Hermes"
    assert repo.status_porcelain() == []
    assert codes(check(classic_fleet), "uncommitted") == []


def test_fix_safe_does_nothing_without_uncommitted(classic_fleet):
    lay = set_up(classic_fleet)
    head = GitRepo(lay.shared_root).head()
    assert fix_safe(check(classic_fleet), lay) is None
    assert GitRepo(lay.shared_root).head() == head


def test_stale_path(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    old = profile_home(root, "device") / "skills" / "general" / "same" / "SKILL.md"
    jobs = profile_home(root, "device") / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True, exist_ok=True)
    jobs.write_text(json.dumps({"jobs": [{"id": "j1", "prompt": f"read {old} first"}]}), encoding="utf-8")
    found = codes(check(classic_fleet), "stale-path")
    assert len(found) == 1 and found[0].profile == "device"
    assert str(lay.shared_root / "general" / "same" / "SKILL.md") in found[0].message


def test_bad_frontmatter(classic_fleet):
    lay = set_up(classic_fleet)
    shared_skill(
        lay, "general/broken", {"SKILL.md": "---\nname: broken\ndescription: [unclosed\n---\nbody\n"}
    )
    found = codes(check(classic_fleet), "bad-frontmatter")
    assert [f.skill for f in found] == ["broken"]


def test_name_mismatch_is_info(classic_fleet):
    lay = set_up(classic_fleet)
    shared_skill(lay, "general/folder-name", {"SKILL.md": skill_md("declared-name", "Renamed.", ["x"])})
    found = codes(check(classic_fleet), "name-mismatch")
    assert [(f.skill, f.severity) for f in found] == [("declared-name", "info")]


def test_missing_ref(classic_fleet):
    lay = set_up(classic_fleet)
    shared_skill(lay, "general/refs", {"SKILL.md": skill_md("refs", "Refs.", ["see `references/gone.md`"])})
    found = codes(check(classic_fleet), "missing-ref")
    assert [f.skill for f in found] == ["refs"] and "references/gone.md" in found[0].message


def test_oversize(classic_fleet):
    lay = set_up(classic_fleet)
    shared_skill(lay, "general/big", {"SKILL.md": skill_md("big", "Big.", ["x" * 100] * 401)})
    assert [f.skill for f in codes(check(classic_fleet), "oversize")] == ["big"]


def _take_over(lay, entry):
    data = json.loads(lay.config_path.read_text(encoding="utf-8"))
    data["taken_over"].append(entry)
    lay.config_path.write_text(json.dumps(data), encoding="utf-8")
    GitRepo(lay.shared_root).commit_paths([lay.config_path], "record take-over")


def test_upstream_newer_bundled(classic_fleet):
    lay = set_up(classic_fleet)
    _take_over(lay, {"name": "same", "profile": "default", "kind": "bundled-edited", "origin_hash": "aaa"})
    found = codes(check(classic_fleet, bundled_index={"same": "bbb"}), "upstream-newer")
    assert [(f.skill, f.severity) for f in found] == [("same", "warn")]
    assert codes(check(classic_fleet, bundled_index={"same": "aaa"}), "upstream-newer") == []
    skipped = codes(check(classic_fleet), "upstream-newer")
    assert [f.severity for f in skipped] == ["info"]


def test_upstream_newer_hub(classic_fleet):
    root = classic_fleet
    lay = set_up(root)
    _take_over(
        lay, {"name": "hubskill", "profile": "default", "kind": "hub-edited", "origin_hash": "sha256:old"}
    )
    found = codes(check(root, bundled_index={}), "upstream-newer")
    assert [(f.skill, f.profile) for f in found] == [("hubskill", "default")]


def test_unfinished_apply(classic_fleet):
    lay = set_up(classic_fleet)
    try:
        with operation(lay, discover(classic_fleet), "promote-x", "promote"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    found = codes(check(classic_fleet), "unfinished-apply")
    assert len(found) == 1 and "promote-x" in found[0].message


def test_render_and_quiet():
    warn = Finding("warn", "shadowed", "pikvm", "same", "shadowed by a local copy")
    info = Finding("info", "name-mismatch", None, "x", "folder y")
    assert render_findings([], quiet=True) == ""
    assert render_findings([info], quiet=True) == ""
    out = render_findings([warn, info])
    assert "shadowed" in out and "name-mismatch" in out and "1 warning" in out
    quiet = render_findings([warn, info], quiet=True)
    assert "shadowed" in quiet and "name-mismatch" not in quiet
    assert "no problems" in render_findings([])


def test_fix_safe_refuses_while_a_journal_is_unfinished(classic_fleet):
    lay = set_up(classic_fleet)
    md = lay.shared_root / "general" / "same" / "SKILL.md"
    md.write_text("half done\n", encoding="utf-8")
    try:
        with operation(lay, discover(classic_fleet), "promote-x", "promote"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    repo = GitRepo(lay.shared_root)
    head = repo.head()
    out = fix_safe(check(classic_fleet), lay)
    assert isinstance(out, Finding) and "promote-x" in out.message and "restore" in out.message
    assert repo.head() == head and repo.status_porcelain() != []
