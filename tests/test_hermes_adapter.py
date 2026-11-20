"""The Hermes adapter, driven against fake Hermes modules placed in sys.modules."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from commons import hermes_adapter as ha
from commons.core.configio import ConfigError, ConfigWriter
from commons.core.fleet import Profile
from tests.fleet import hub_content_hash, skill_md, write_files


def _fake(monkeypatch, dotted: str, **attrs) -> types.ModuleType:
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


def _no_module(monkeypatch, dotted: str) -> None:
    monkeypatch.setitem(sys.modules, dotted, None)  # import of it raises ImportError


# ------------------------------------------------------------------ simple wrappers


def test_default_root_and_state_dir(monkeypatch, tmp_path):
    _fake(monkeypatch, "hermes_constants", get_default_hermes_root=lambda: tmp_path)
    assert ha.default_root() == tmp_path
    assert ha.state_dir() == tmp_path / "commons"


def test_missing_symbol_raises_hermes_api_changed(monkeypatch):
    _fake(monkeypatch, "hermes_constants")
    with pytest.raises(ha.HermesApiChanged, match="hermes_constants.get_default_hermes_root"):
        ha.default_root()


def test_missing_module_raises_hermes_api_changed(monkeypatch):
    _no_module(monkeypatch, "hermes_constants")
    with pytest.raises(ha.HermesApiChanged):
        ha.active_profile_home()


def test_is_deleted_active_home_and_name(monkeypatch, tmp_path):
    seen = []
    _fake(
        monkeypatch,
        "hermes_constants",
        named_profile_is_deleted=lambda p: seen.append(p) or True,
        get_hermes_home=lambda: tmp_path / "profiles" / "dev",
        profile_name_for_home=lambda p: Path(p).name,
    )
    assert ha.is_deleted(tmp_path) is True and seen == [tmp_path]
    assert ha.active_profile_home() == tmp_path / "profiles" / "dev"
    assert ha.active_profile_name() == "dev"


def test_active_profile_name_falls_back_to_default(monkeypatch, tmp_path):
    _fake(
        monkeypatch,
        "hermes_constants",
        get_hermes_home=lambda: tmp_path,
        profile_name_for_home=lambda p: None,
    )
    assert ha.active_profile_name() == "default"


def test_md5_dir_hash_calls_hermes(monkeypatch, tmp_path):
    _fake(monkeypatch, "tools.skills_sync", dir_hash=lambda d: f"md5:{Path(d).name}")
    assert ha.md5_dir_hash(tmp_path / "x") == "md5:x"


def test_hub_hash_mirrors_hermes_content_hash(tmp_path):
    write_files(
        tmp_path / "s", {"SKILL.md": skill_md("s", "d", ["a"]), "references/r.md": "r\n", "b.bin": b"\0\1"}
    )
    assert ha.hub_hash(tmp_path / "s") == hub_content_hash(tmp_path / "s")
    one = tmp_path / "flat.md"
    one.write_text("x\n", encoding="utf-8")
    import hashlib

    assert ha.hub_hash(one) == "sha256:" + hashlib.sha256(b"x\n").hexdigest()[:16]


# ------------------------------------------------------------------ locate


def test_locate_returns_the_skill_dir(monkeypatch, tmp_path):
    roots = [(1, tmp_path / "skills")]
    calls = []

    def locate_skill(name, local, rts):
        calls.append((name, local, rts))
        return (
            None,
            tmp_path / "skills" / "general" / name,
            tmp_path / "skills" / "general" / name / "SKILL.md",
        )

    _fake(
        monkeypatch,
        "tools.skills_tool",
        skill_lookup_path_error=lambda n: None,
        skill_search_dirs=lambda: (roots, tmp_path / "skills"),
        locate_skill=locate_skill,
    )
    assert ha.locate("foo") == tmp_path / "skills" / "general" / "foo"
    assert calls == [("foo", None, roots)]


def test_locate_legacy_flat_file_and_errors(monkeypatch, tmp_path):
    flat = tmp_path / "skills" / "foo.md"
    mod = _fake(
        monkeypatch,
        "tools.skills_tool",
        skill_lookup_path_error=lambda n: None,
        skill_search_dirs=lambda: ([], None),
        locate_skill=lambda n, l, r: (None, None, flat),
    )
    assert ha.locate("foo") == flat
    mod.locate_skill = lambda n, l, r: ('{"error": "not found"}', None, None)
    assert ha.locate("foo") is None
    mod.skill_lookup_path_error = lambda n: "bad name"
    assert ha.locate("../x") is None


# ------------------------------------------------------------------ bundled index


def test_bundled_index_maps_names_to_dir_hash(monkeypatch, tmp_path):
    bundled = tmp_path / "repo" / "skills"
    write_files(bundled / "general" / "alpha", {"SKILL.md": skill_md("alpha-name", "d", ["a"])})
    write_files(bundled / "beta", {"SKILL.md": skill_md("beta", "d", ["b"])})
    write_files(bundled / "beta" / "references" / "inner", {"SKILL.md": skill_md("inner", "d", ["x"])})
    write_files(bundled / ".git" / "junk", {"SKILL.md": skill_md("junk", "d", ["j"])})
    hashed = []
    _fake(monkeypatch, "hermes_cli.config", get_project_root=lambda: tmp_path / "repo")
    _fake(monkeypatch, "hermes_constants", get_bundled_skills_dir=lambda default=None: Path(default))
    _fake(
        monkeypatch,
        "tools.skills_sync",
        dir_hash=lambda d: hashed.append(Path(d).name) or f"h-{Path(d).name}",
    )
    _fake(
        monkeypatch,
        "tools.skill_usage",
        read_skill_name=lambda md, fb: yaml.safe_load(md.read_text().split("---")[1])["name"],
    )
    index = ha.bundled_index()
    assert index("alpha-name") == "h-alpha"
    assert index("beta") == "h-beta"
    assert index("inner") is None and index("junk") is None and index("nope") is None
    index("alpha-name")
    assert sorted(hashed) == ["alpha", "beta"]  # hashed once, lazily


# ------------------------------------------------------------------ HermesCliWriter


class FakeRunner:
    """Stands in for subprocess.run: records argv/env, applies config set/unset to the YAML file."""

    def __init__(self, home_of, returncode=0, stderr=""):
        self.calls = []
        self.home_of = home_of
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.returncode == 0:
            prof = argv[argv.index("-p") + 1]
            path = self.home_of(prof) / "config.yaml"
            data = yaml.safe_load(path.read_text()) if path.is_file() else {}
            data = data or {}
            sub = argv[argv.index("config") + 1]
            key = argv[argv.index("--") + 1]
            top, leaf = key.split(".")
            if sub == "set":
                raw = argv[argv.index("--") + 2]
                value = yaml.safe_load(raw) if raw.startswith("[") else raw
                data.setdefault(top, {})[leaf] = value
            else:
                data.get(top, {}).pop(leaf, None)
                if data.get(top) == {}:
                    del data[top]
            path.write_text(yaml.safe_dump(data))
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)


def _writer(tmp_path, runner=None, **kw):
    homes = {"default": tmp_path, "dev": tmp_path / "profiles" / "dev"}
    for h in homes.values():
        h.mkdir(parents=True, exist_ok=True)
    runner = runner or FakeRunner(homes.__getitem__)
    w = ha.HermesCliWriter(root=tmp_path, runner=runner, command=lambda args: ["/opt/hermes", *args], **kw)
    return w, runner, {n: Profile(n, h) for n, h in homes.items()}


def test_writer_is_a_config_writer(tmp_path):
    w, _, _ = _writer(tmp_path)
    assert isinstance(w, ConfigWriter)


def test_writer_set_runs_hermes_config_set_with_profile(tmp_path):
    w, runner, ps = _writer(tmp_path)
    assert w.set(ps["default"], "skills.create_dir", "~/shared skills") == "edited"
    argv, kw = runner.calls[0]
    assert argv == [
        "/opt/hermes",
        "-p",
        "default",
        "config",
        "set",
        "--",
        "skills.create_dir",
        "~/shared skills",
    ]
    assert kw["env"]["HERMES_HOME"] == str(tmp_path)
    assert kw["timeout"] == w.timeout and kw["check"] is False
    assert w.get(ps["default"], "skills.create_dir") == "~/shared skills"
    assert w.set(ps["default"], "skills.create_dir", "~/shared skills") == "unchanged"
    assert len(runner.calls) == 1


def test_writer_list_add_and_remove_set_the_whole_list(tmp_path):
    w, runner, ps = _writer(tmp_path)
    (ps["dev"].home / "config.yaml").write_text("plugins:\n  enabled: [a]\n")
    assert w.add_to_list(ps["dev"], "plugins.enabled", "commons") == "edited"
    assert runner.calls[-1][0][-1] == "[a, commons]"
    assert runner.calls[-1][0][1:3] == ["-p", "dev"]
    assert w.add_to_list(ps["dev"], "plugins.enabled", "commons") == "unchanged"
    assert w.remove_from_list(ps["dev"], "plugins.enabled", "a") == "edited"
    assert runner.calls[-1][0][-1] == "[commons]"
    assert w.get(ps["dev"], "plugins.enabled") == ["commons"]
    assert w.remove_from_list(ps["dev"], "plugins.enabled", "zzz") == "unchanged"


def test_writer_unset_skips_absent_key(tmp_path):
    w, runner, ps = _writer(tmp_path)
    assert w.unset(ps["default"], "skills.create_dir") == "unchanged"
    assert runner.calls == []
    w.set(ps["default"], "skills.create_dir", "/x")
    assert w.unset(ps["default"], "skills.create_dir") == "edited"
    assert runner.calls[-1][0][3:] == ["config", "unset", "--", "skills.create_dir"]
    assert w.get(ps["default"], "skills.create_dir") is None


def test_writer_reports_failures_clearly(tmp_path):
    homes = {"default": tmp_path}
    w, _, ps = _writer(tmp_path, runner=FakeRunner(homes.__getitem__, returncode=1, stderr="Cannot set 'x'"))
    with pytest.raises(ConfigError, match="Cannot set 'x'"):
        w.set(ps["default"], "skills.create_dir", "/x")


def test_writer_timeout_is_a_config_error(tmp_path):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    w, _, ps = _writer(tmp_path, runner=slow, timeout=7)
    with pytest.raises(ConfigError, match="timed out after 7"):
        w.set(ps["default"], "skills.create_dir", "/x")


def test_writer_verifies_the_value_landed(tmp_path):
    def liar(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    w, _, ps = _writer(tmp_path, runner=liar)
    with pytest.raises(ConfigError, match="did not take"):
        w.set(ps["default"], "skills.create_dir", "/x")


def test_writer_default_command_uses_hermes_command(monkeypatch, tmp_path):
    _fake(monkeypatch, "hermes_cli.chat_launch", hermes_command=lambda args=(): ["/bin/hermes", *args])
    w = ha.HermesCliWriter(root=tmp_path)
    assert w.command(["config", "get"]) == ["/bin/hermes", "config", "get"]
    _fake(monkeypatch, "hermes_cli.chat_launch")
    with pytest.raises(ha.HermesApiChanged):
        ha.HermesCliWriter(root=tmp_path).command(["x"])


def test_writer_refuses_a_name_hermes_would_casefold(tmp_path):
    w, runner, _ = _writer(tmp_path)
    home = tmp_path / "profiles" / "Dev"
    home.mkdir(parents=True)
    with pytest.raises(ConfigError, match="not one `hermes -p` accepts"):
        w.set(Profile("Dev", home), "skills.create_dir", "/x")
    assert runner.calls == []


def test_writer_refuses_a_profile_outside_its_root(tmp_path):
    w, runner, _ = _writer(tmp_path)
    with pytest.raises(ConfigError, match="refusing to write"):
        w.set(Profile("dev", tmp_path / "elsewhere"), "skills.create_dir", "/x")
    assert runner.calls == []
