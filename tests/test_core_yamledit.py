"""YamlFileWriter keeps comments and order; cron jobs without an id are keyed by position."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from commons.core.configio import ConfigError, YamlFileWriter
from commons.core.fleet import Profile, discover
from commons.core.frontmatter import _UniqueKeyLoader
from commons.core.paths import find_stale_paths, job_keys, load_jobs_file, rewrite
from tests.fleet import profile_home

CONFIG = """\
# my hermes config
model:
  default: claude   # the good one
  provider: anthropic

skills:
  # where new skills go
  create_dir: ~/old-skills  # set by hand
  external_dirs:
    - ~/more

plugins:
  enabled:
    # first plugin
    - alpha
    - beta  # keep

display:
  theme: dark
"""


def _profile(tmp_path: Path, text: str) -> Profile:
    home = tmp_path / "p"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")
    return Profile(name="p", home=home)


def _changed(before: str, after: str) -> tuple[list[str], list[str]]:
    a, b = before.splitlines(), after.splitlines()
    return [x for x in a if x not in b], [x for x in b if x not in a]


def test_set_scalar_keeps_every_other_line(tmp_path):
    p = _profile(tmp_path, CONFIG)
    w = YamlFileWriter()
    assert w.set(p, "skills.create_dir", "/srv/shared") == "edited"
    after = (p.home / "config.yaml").read_text(encoding="utf-8")
    assert _changed(CONFIG, after) == (
        ["  create_dir: ~/old-skills  # set by hand"],
        ["  create_dir: /srv/shared  # set by hand"],
    )
    assert w.fallbacks == []
    assert w.set(p, "skills.create_dir", "/srv/shared") == "unchanged"


def test_list_add_and_remove_round_trip_byte_identical(tmp_path):
    p = _profile(tmp_path, CONFIG)
    w = YamlFileWriter()
    assert w.add_to_list(p, "plugins.enabled", "commons") == "edited"
    after = (p.home / "config.yaml").read_text(encoding="utf-8")
    assert _changed(CONFIG, after) == ([], ["    - commons"])
    assert after.index("- beta  # keep") < after.index("- commons") < after.index("display:")
    assert w.remove_from_list(p, "plugins.enabled", "commons") == "edited"
    assert (p.home / "config.yaml").read_text(encoding="utf-8") == CONFIG


def test_set_then_unset_new_key_round_trip(tmp_path):
    text = CONFIG.replace("  create_dir: ~/old-skills  # set by hand\n", "")
    p = _profile(tmp_path, text)
    w = YamlFileWriter()
    assert w.set(p, "skills.create_dir", "/srv/shared") == "edited"
    assert yaml.safe_load((p.home / "config.yaml").read_text())["skills"]["create_dir"] == "/srv/shared"
    assert w.unset(p, "skills.create_dir") == "edited"
    assert (p.home / "config.yaml").read_text(encoding="utf-8") == text


def test_missing_sections_are_appended(tmp_path):
    text = "# only a comment\nmodel:\n  default: x\n"
    p = _profile(tmp_path, text)
    w = YamlFileWriter()
    w.set(p, "skills.create_dir", "/s")
    w.add_to_list(p, "plugins.enabled", "commons")
    after = (p.home / "config.yaml").read_text(encoding="utf-8")
    assert after.startswith(text)
    assert yaml.safe_load(after) == {
        "model": {"default": "x"},
        "skills": {"create_dir": "/s"},
        "plugins": {"enabled": ["commons"]},
    }
    w.remove_from_list(p, "plugins.enabled", "commons")
    w.unset(p, "skills.create_dir")
    assert yaml.safe_load((p.home / "config.yaml").read_text()) == {
        "model": {"default": "x"},
        "plugins": {"enabled": []},
    }
    assert w.fallbacks == []


def test_one_line_flow_list_is_edited_on_its_line(tmp_path):
    text = "# c\nplugins:\n  enabled: [alpha]  # note\nother: 1\n"
    p = _profile(tmp_path, text)
    w = YamlFileWriter()
    assert w.add_to_list(p, "plugins.enabled", "commons") == "edited"
    assert (
        p.home / "config.yaml"
    ).read_text() == "# c\nplugins:\n  enabled: [alpha, commons]  # note\nother: 1\n"


@pytest.mark.parametrize(
    "text",
    [
        "plugins: {enabled: [alpha]}\n# trailing\n",
        "skills: {create_dir: /x}\n",
        'skills:\n  create_dir: "/quoted"\n',
    ],
)
def test_unusual_layout_falls_back_and_reports_it(tmp_path, text):
    p = _profile(tmp_path, text)
    w = YamlFileWriter()
    key = "plugins.enabled" if text.startswith("plugins") else "skills.create_dir"
    out = w.add_to_list(p, key, "commons") if key == "plugins.enabled" else w.set(p, key, "/new")
    assert out == "rewritten"
    assert [k for _, k, _ in w.fallbacks] == [key]
    data = yaml.safe_load((p.home / "config.yaml").read_text())
    assert data == yaml.safe_load(text) | (
        {"plugins": {"enabled": ["alpha", "commons"]}}
        if key == "plugins.enabled"
        else {"skills": {"create_dir": "/new"}}
    )


def test_crlf_and_bom_are_kept(tmp_path):
    text = "\ufeff# c\r\nskills:\r\n  create_dir: /old\r\n"
    home = tmp_path / "p"
    home.mkdir()
    (home / "config.yaml").write_bytes(text.encode("utf-8"))
    p = Profile(name="p", home=home)
    assert YamlFileWriter().set(p, "skills.create_dir", "/new") == "edited"
    assert (home / "config.yaml").read_bytes() == "\ufeff# c\r\nskills:\r\n  create_dir: /new\r\n".encode()


# ------------------------------------------------------------------ cron ids


def test_job_keys():
    jobs = [{"id": "a"}, {"prompt": "x"}, {"id": None}, {"id": "b"}, {"id": "b"}, {"id": 7}, "junk"]
    assert job_keys(jobs) == ["a", "#1", "#2", "#3", "#4", "7", "#6"]


def test_cron_job_without_id_is_rewritten_by_index(classic_fleet):
    root = classic_fleet
    old = profile_home(root, "pikvm") / "skills" / "general" / "same"
    new = root / "shared" / "general" / "same"
    new.mkdir(parents=True)
    (new / "SKILL.md").write_text("x")
    jobs_path = profile_home(root, "pikvm") / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs = [
        {"id": "keep", "prompt": "nothing here"},
        {"prompt": f"read {old}/SKILL.md"},
        {"prompt": f"also {old}/SKILL.md"},
    ]
    jobs_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    profiles = discover(root)
    found = find_stale_paths(
        profiles, root / "shared", {old: new}, {"pikvm": load_jobs_file(jobs_path)[1]}, home=root
    )
    assert sorted(f.job_id for f in found) == ["#1", "#2"]
    assert all("None" not in f.location for f in found)
    done, skipped = rewrite(found, root / "b", {"pikvm": jobs_path})
    assert len(done) == 2 and skipped == []
    data = json.loads(jobs_path.read_text())["jobs"]
    assert [j["prompt"] for j in data] == ["nothing here", f"read {new}/SKILL.md", f"also {new}/SKILL.md"]


def test_cron_job_without_id_is_skipped_when_list_shifted(classic_fleet):
    root = classic_fleet
    old = profile_home(root, "pikvm") / "skills" / "general" / "same"
    new = root / "shared" / "general" / "same"
    new.mkdir(parents=True)
    (new / "SKILL.md").write_text("x")
    jobs_path = profile_home(root, "pikvm") / "cron" / "jobs.json"
    jobs_path.parent.mkdir(parents=True)
    jobs_path.write_text(json.dumps({"jobs": [{"prompt": f"read {old}/SKILL.md"}]}), encoding="utf-8")
    found = find_stale_paths(
        discover(root), root / "shared", {old: new}, {"pikvm": load_jobs_file(jobs_path)[1]}, home=root
    )
    # a job with an id is inserted in front before the rewrite: '#0' now names a job with an id
    shifted = {"jobs": [{"id": "new", "prompt": "hello"}, {"prompt": f"read {old}/SKILL.md"}]}
    jobs_path.write_text(json.dumps(shifted), encoding="utf-8")
    before = jobs_path.read_bytes()
    done, skipped = rewrite(found, root / "b", {"pikvm": jobs_path})
    assert done == [] and len(skipped) == 1
    assert jobs_path.read_bytes() == before


def _keys_unique(text: str) -> None:
    """Every mapping in *text* has unique keys (what Hermes' ruamel round-trip insists on)."""
    yaml.load(text, Loader=_UniqueKeyLoader)


@pytest.mark.parametrize(
    "text",
    [
        '"skills":\n  create_dir: /old\n',
        "skills :\n  create_dir: /old\n",
        "'skills':\n  create_dir: /old\n",
        'skills:\n  "create_dir": /old\n  external_dirs: []\n',
        "skills:\n  create_dir : /old\n",
        'skills:\n  external_dirs: []\n  "create_dir": /old\n',
        "skills:\n  external_dirs: []\n  ? create_dir\n  : /old\n",
        "skills:\n  ? create_dir\n  : /old\n",
    ],
)
def test_odd_key_spellings_never_create_duplicates(tmp_path, text):
    p = _profile(tmp_path, text)
    w = YamlFileWriter()
    assert w.set(p, "skills.create_dir", "/new") == "rewritten"
    after = (p.home / "config.yaml").read_text(encoding="utf-8")
    _keys_unique(after)
    assert yaml.safe_load(after)["skills"]["create_dir"] == "/new"


def test_quoted_plugins_key_never_creates_duplicates(tmp_path):
    p = _profile(tmp_path, 'plugins:\n  "enabled":\n  - a\n')
    w = YamlFileWriter()
    w.add_to_list(p, "plugins.enabled", "commons")
    after = (p.home / "config.yaml").read_text(encoding="utf-8")
    _keys_unique(after)
    assert yaml.safe_load(after)["plugins"]["enabled"] == ["a", "commons"]


@pytest.mark.parametrize(
    "text",
    [
        "skills:\n  create_dir: /a\nskills:\n  create_dir: /b\n",
        "skills:\n  create_dir: /a\n  create_dir: /b\n",
        "model: x\ndisplay:\n  theme: a\n  theme: b\n",
    ],
)
def test_config_with_duplicate_keys_is_refused(tmp_path, text):
    p = _profile(tmp_path, text)
    w = YamlFileWriter()
    with pytest.raises(ConfigError, match="duplicate"):
        w.set(p, "skills.create_dir", "/new")
    assert (p.home / "config.yaml").read_text(encoding="utf-8") == text
