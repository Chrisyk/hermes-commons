"""SKILL.md frontmatter parsed the way Hermes parses it.

Hermes is never imported. Mirror of Hermes v0.23.1 agent/skill_utils.py
parse_frontmatter: a leading BOM is dropped; the block runs from a leading
'---' to the first match of r"\\n---\\s*\\n"; it is read with the shared YAML
1.1 safe loader (hermes_yaml.safe_load, ruamel, which rejects duplicate keys);
a non-mapping result is {}; any YAML error falls back to splitting each line
on its first ':' with key and value stripped.

PyYAML's SafeLoader also implements YAML 1.1 (y/n/yes/on booleans,
sexagesimal ints, dates); the one ruamel rule it lacks, refusing duplicate
keys, is added below so such a block takes the line fallback as in Hermes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError, SafeConstructor

_END = re.compile(r"\n---\s*\n")


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys, as ruamel's safe loader does."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    seen: list[Any] = []
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in seen
        except TypeError:
            duplicate = False
        if duplicate:
            raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.append(key)
    return SafeConstructor.construct_mapping(loader, node, deep)


def _construct_bool(loader: _UniqueKeyLoader, node: yaml.ScalarNode) -> bool:
    value = loader.construct_scalar(node).lower()
    return _YAML11_BOOL.get(value, SafeConstructor.bool_values.get(value, False))


# ruamel's YAML 1.1 resolver also reads bare y/Y/n/N as booleans; PyYAML does not.
_YAML11_BOOL = {"y": True, "n": False}
_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
_UniqueKeyLoader.add_constructor("tag:yaml.org,2002:bool", _construct_bool)
_UniqueKeyLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:y|Y|n|N)$"), list("yYnN"))


def frontmatter_block(content: str) -> tuple[str, str] | None:
    """(yaml text, body) of *content*, split as Hermes parse_frontmatter does; None without a block."""
    content = content.removeprefix("\ufeff")
    end = _END.search(content[3:]) if content.startswith("---") else None
    if not end:
        return None
    return content[3 : end.start() + 3], content[end.end() + 3 :]


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Mirror of Hermes agent/skill_utils.py parse_frontmatter (the frontmatter dict only)."""
    block = frontmatter_block(content)
    if block is None:
        return {}
    text = block[0]
    try:
        parsed = yaml.load(text, Loader=_UniqueKeyLoader)
    except Exception:  # noqa: BLE001 - Hermes falls back on any parse failure
        out: dict[str, Any] = {}
        for line in text.strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                out[key.strip()] = value.strip()
        return out
    return parsed if isinstance(parsed, dict) else {}


def read_frontmatter(skill_md: Path, strict: bool = False) -> dict[str, Any]:
    """Frontmatter of a SKILL.md, {} when it cannot be read.

    strict=False mirrors Hermes tools/skills_tool_plugin.py _safe_frontmatter
    (utf-8-sig, undecodable bytes replaced); strict=True mirrors
    agent/prompt_builder.py _parse_skill_file, where a decode error yields {}.
    """
    try:
        text = Path(skill_md).read_text(encoding="utf-8-sig", errors="strict" if strict else "replace")
    except (OSError, UnicodeDecodeError):
        return {}
    return parse_frontmatter(text)
