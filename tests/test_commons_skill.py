"""The commons skill shipped inside the plugin and registered with ctx.register_skill."""

from __future__ import annotations

from pathlib import Path

import yaml

import commons
from commons.core.frontmatter import frontmatter_block

SKILL = Path(commons.__file__).resolve().parent / "skills" / "commons" / "SKILL.md"


def _frontmatter() -> dict:
    block = frontmatter_block(SKILL.read_text(encoding="utf-8"))
    assert block is not None
    data = yaml.safe_load(block[0])
    assert isinstance(data, dict)
    return data


def test_frontmatter_parses_and_names_the_skill():
    fm = _frontmatter()
    assert fm["name"] == "commons"
    assert fm["description"] == commons.SKILL_DESCRIPTION


def test_size_limits_hold():
    text = SKILL.read_text(encoding="utf-8")
    assert len(text) < 2000
    assert len(_frontmatter()["description"]) <= 60


def test_says_what_the_design_asks():
    text = SKILL.read_text(encoding="utf-8")
    for needle in (
        "hermes commons check",
        "hermes commons promote",
        "hermes commons demote",
        "hermes commons merge --next",
        "hermes commons merge --accept",
        "never copy",
    ):
        assert needle in text, needle
    assert "\u2014" not in text and "\u2013" not in text  # no em or en dashes


class Ctx:
    def __init__(self):
        self.skills = []

    def register_hook(self, *a, **k):
        pass

    def register_cli_command(self, *a, **k):
        pass

    def register_command(self, *a, **k):
        pass

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, path, description))


def test_register_skill_is_called_with_a_path():
    ctx = Ctx()
    commons.register(ctx)
    assert len(ctx.skills) == 1
    name, path, description = ctx.skills[0]
    assert name == "commons"
    assert isinstance(path, Path) and path == SKILL and path.is_file()
    assert description == commons.SKILL_DESCRIPTION
