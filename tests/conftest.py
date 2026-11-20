"""Shared pytest fixtures: synthetic Hermes fleets built in tmp dirs."""

import pytest

from tests.fleet import make_fleet, skill_md

PROFILES = ("default", "device", "pikvm", "pocket")


@pytest.fixture(autouse=True)
def _no_sandbox_root(monkeypatch):
    """Keep a COMMONS_HERMES_ROOT exported in the shell from leaking into tests."""
    monkeypatch.delenv("COMMONS_HERMES_ROOT", raising=False)


@pytest.fixture
def fleet(tmp_path):
    """Return a builder; calling it twice writes both fleets into the same tmp_path."""
    return lambda spec: make_fleet(tmp_path, spec)


def _classic_spec() -> dict:
    same = {
        "SKILL.md": skill_md("same", "Identical everywhere.", ["S1", "S2"]),
        "notes.txt": "shared notes\n",
    }
    contained_lines = [f"contained line {i}" for i in range(1, 7)]
    contained_small = {"SKILL.md": skill_md("contained", "Contained skill.", contained_lines[:4])}
    contained_big = {"SKILL.md": skill_md("contained", "Contained skill.", contained_lines)}
    pristine = {"SKILL.md": skill_md("bundled-pristine", "Bundled and untouched.", ["P1", "P2"])}
    edited_lines = ["E1", "E2"]
    edited_origin = {"SKILL.md": skill_md("bundled-edited", "Bundled then edited.", edited_lines)}

    spec: dict = {p: {} for p in PROFILES}
    for p in PROFILES:
        spec[p]["general/same"] = dict(same)
        spec[p]["general/contained"] = dict(contained_big if p == "device" else contained_small)
        spec[p]["bundled/bundled-pristine"] = dict(pristine)
        if p in ("pikvm", "pocket"):
            extra = edited_lines + [f"extra line from {p}"]
            spec[p]["bundled/bundled-edited"] = {
                "SKILL.md": skill_md("bundled-edited", "Bundled then edited.", extra)
            }
        else:
            spec[p]["bundled/bundled-edited"] = dict(edited_origin)

    spec["default"]["general/conflict"] = {
        "SKILL.md": skill_md("conflict", "Diverged copies.", ["A", "B", "C_default_only"])
    }
    spec["device"]["general/conflict"] = {
        "SKILL.md": skill_md("conflict", "Diverged copies.", ["A", "B", "D_device_only"])
    }
    spec["default"]["tools/hubskill"] = {"SKILL.md": skill_md("hubskill", "Installed from the hub.", ["H1"])}
    spec["default"]["general/linked"] = "symlink:_linked/linked"
    spec["device"]["a/dup"] = {"SKILL.md": skill_md("dup", "First dup.", ["first"])}
    spec["device"]["b/dup"] = {"SKILL.md": skill_md("dup", "Second dup.", ["second"])}

    spec["_bundled"] = {p: {"bundled-pristine": pristine, "bundled-edited": edited_origin} for p in PROFILES}
    spec["_hub"] = {"default": {"hubskill": "tools/hubskill"}}
    return spec


@pytest.fixture
def classic_fleet(tmp_path):
    return make_fleet(tmp_path, _classic_spec())
