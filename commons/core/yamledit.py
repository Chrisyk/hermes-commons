"""A minimal line editor for the two config.yaml keys commons writes.

`hermes config set` keeps comments, key order and quoting (it ends in
utils.atomic_roundtrip_yaml_update: a ruamel round-trip load, one key
changed, dump). PyYAML cannot round-trip, so YamlFileWriter matches that
behaviour with this editor for the shapes commons writes: a scalar or a
list two levels deep (skills.create_dir, plugins.enabled) under a
block-style top-level mapping. Only the lines of that key change; every
other line stays byte for byte. A one-line flow list ('enabled: [a, b]')
is edited on its own line. Anything else (a flow-style parent, a
multi-line or tagged value, tabs, a deeper key, a key spelled quoted, with a
space before its colon or as an explicit '? key') raises Unsupported, and
the caller falls back to a full dump. Stdlib + PyYAML.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_TOP = re.compile(r"^(?P<key>[A-Za-z0-9_][A-Za-z0-9_.-]*):(?P<rest>(?:[ \t].*)?)$")
_CHILD = re.compile(r"^(?P<indent> +)(?P<key>[A-Za-z0-9_][A-Za-z0-9_.-]*):(?P<rest>(?:[ \t].*)?)$")
_ITEM = re.compile(r"^(?P<indent> *)- (?P<value>.*)$")
_SPECIAL = ("[", "{", "|", ">", "&", "*", "!", "'", '"')


def _odd_spelling(line: str, key: str) -> bool:
    """True when *line* holds *key* spelled other than plain 'key:' ('"key":', 'key :', '? key')."""
    s = line.lstrip(" ")
    k = re.escape(key)
    return bool(re.match(rf"""(?:\?[ \t]+["']?{k}["']?[ \t]*$|["']{k}["'][ \t]*:|{k}[ \t]+:)""", s))


class Unsupported(Exception):
    """A layout this editor does not handle; the caller falls back to a full dump."""


def flow(value: Any) -> str:
    """One-line YAML for a value (flow style)."""
    text = yaml.safe_dump(
        value, default_flow_style=True, width=2**31 - 1, allow_unicode=True, sort_keys=False
    )
    return text.removesuffix("\n...\n").strip()


def _filler(line: str) -> bool:
    """Blank or comment-only line."""
    s = line.strip()
    return not s or s.startswith("#")


def _split(rest: str) -> tuple[str, str]:
    """' value  # note' -> ('value', '  # note'). Quoted values are refused (Unsupported)."""
    body = rest.strip()
    if body.startswith(("'", '"')):
        raise Unsupported("quoted value")
    m = re.search(r"[ \t]#", rest)
    if m:
        value = rest[: m.start()].rstrip()
        return value.strip(), rest[len(value) :]
    return body, ""


class Lines:
    """A YAML text as lines, with the edits YamlFileWriter needs."""

    def __init__(self, text: str):
        if "\t" in text:
            raise Unsupported("tabs")
        self.lines = [line.rstrip("\r\n") for line in text.splitlines()]
        self.nl = "\r\n" if "\r\n" in text else "\n"
        self.final_nl = text.endswith(("\n", "\r"))

    def text(self) -> str:
        if not self.lines:
            return ""
        return self.nl.join(self.lines) + (self.nl if self.final_nl else "")

    # ------------------------------------------------------------ locating

    def _top(self, key: str) -> int | None:
        found = None
        for n, line in enumerate(self.lines):
            m = _TOP.match(line)
            if m and m["key"] == key:
                if found is not None:
                    raise Unsupported(f"{key} appears twice")
                if _split(m["rest"])[0]:
                    raise Unsupported(f"{key} is not a block mapping")
                found = n
        return found

    def _block_end(self, top: int) -> int:
        """End (exclusive) of the indented block under line *top*, trailing filler left out."""
        last = top
        for n in range(top + 1, len(self.lines)):
            line = self.lines[n]
            if _filler(line):
                continue
            if not line.startswith(" "):
                break
            last = n
        return last + 1

    def _indent(self, top: int, end: int) -> str:
        for line in self.lines[top + 1 : end]:
            if not _filler(line):
                m = _CHILD.match(line)
                if not m:
                    raise Unsupported("unusual block layout")
                return m["indent"]
        return "  "

    def _child(self, top: int, end: int, indent: str, key: str) -> int | None:
        found = None
        for n in range(top + 1, end):
            m = _CHILD.match(self.lines[n])
            if m and m["indent"] == indent and m["key"] == key:
                if found is not None:
                    raise Unsupported(f"{key} appears twice")
                found = n
        return found

    def _value_end(self, child: int, end: int, indent: str) -> int:
        """End (exclusive) of the value lines of the child key at *child*."""
        last = child
        for n in range(child + 1, end):
            line = self.lines[n]
            if _filler(line):
                continue
            width = len(line) - len(line.lstrip(" "))
            if width < len(indent) or (width == len(indent) and not line.lstrip(" ").startswith("- ")):
                break
            last = n
        return last + 1

    def _locate(self, top_key: str, key: str) -> tuple[int, int, str, int | None]:
        for line in self.lines:
            if _odd_spelling(line, top_key) or _odd_spelling(line, key):
                raise Unsupported(f"{top_key}.{key} is spelled in an unusual way")
        top = self._top(top_key)
        if top is None:
            return -1, -1, "  ", None
        end = self._block_end(top)
        indent = self._indent(top, end)
        return top, end, indent, self._child(top, end, indent, key)

    def _append(self, new: list[str]) -> None:
        self.lines += new
        self.final_nl = True

    # ------------------------------------------------------------ edits

    def set_scalar(self, top_key: str, key: str, value: Any) -> None:
        if isinstance(value, (dict, list)):
            raise Unsupported("not a scalar")
        dumped = flow(value)
        top, end, indent, n = self._locate(top_key, key)
        if top < 0:
            self._append([f"{top_key}:", f"  {key}: {dumped}"])
            return
        if n is None:
            self.lines[end:end] = [f"{indent}{key}: {dumped}"]
            return
        if self._value_end(n, end, indent) != n + 1:
            raise Unsupported(f"{top_key}.{key} spans several lines")
        m = _CHILD.match(self.lines[n])
        assert m is not None
        old, comment = _split(m["rest"])
        if old.startswith(_SPECIAL):
            raise Unsupported(f"{top_key}.{key} has a non-plain value")
        self.lines[n] = f"{indent}{key}: {dumped}{comment}"

    def set_list(self, top_key: str, key: str, items: list) -> None:
        if any(isinstance(x, (dict, list)) for x in items):
            raise Unsupported("nested list items")
        dumped = [flow(x) for x in items]
        top, end, indent, n = self._locate(top_key, key)
        if top < 0:
            body = [f"  {key}:", *[f"  - {d}" for d in dumped]] if dumped else [f"  {key}: []"]
            self._append([f"{top_key}:", *body])
            return
        if n is None:
            body = (
                [f"{indent}{key}:", *[f"{indent}- {d}" for d in dumped]] if dumped else [f"{indent}{key}: []"]
            )
            self.lines[end:end] = body
            return
        stop = self._value_end(n, end, indent)
        m = _CHILD.match(self.lines[n])
        assert m is not None
        head, comment = _split(m["rest"])
        if stop == n + 1 and (head.startswith("[") or head in ("", "null", "~")):
            if head.startswith("[") and not head.endswith("]"):
                raise Unsupported(f"{top_key}.{key} is a multi-line flow list")
            self.lines[n] = f"{indent}{key}: {flow(list(items))}{comment}"
            return
        if head:
            raise Unsupported(f"{top_key}.{key} is not a block list")
        item_rows = [k for k in range(n + 1, stop) if not _filler(self.lines[k])]
        matches = [_ITEM.match(self.lines[k]) for k in item_rows]
        if not all(matches) or len({mm["indent"] for mm in matches if mm}) != 1:
            raise Unsupported(f"{top_key}.{key} is not a simple block list")
        item_indent = matches[0]["indent"] if matches[0] else indent
        current = []
        for mm in matches:
            assert mm is not None
            text, _ = _split(mm["value"])
            if text.startswith(_SPECIAL):
                raise Unsupported("non-plain list item")
            current.append(yaml.safe_load(text))
        kept = [x for x in current if x in items]
        if kept != [x for x in items if x in current]:
            raise Unsupported("list order changed")
        keep_rows = [k for k, v in zip(item_rows, current, strict=True) if v in items]
        drop_rows = [k for k in item_rows if k not in keep_rows]
        added = [d for x, d in zip(items, dumped, strict=True) if x not in current]
        if not keep_rows and not added:
            self.lines[n] = f"{indent}{key}: []{comment}"
        else:
            at = keep_rows[-1] + 1 if keep_rows else item_rows[-1] + 1 if item_rows else n + 1
            self.lines[at:at] = [f"{item_indent}- {d}" for d in added]
        for k in sorted(drop_rows, reverse=True):
            del self.lines[k]

    def unset(self, top_key: str, key: str) -> None:
        top, end, indent, n = self._locate(top_key, key)
        if n is None:
            return
        del self.lines[n : self._value_end(n, end, indent)]
        end = self._block_end(top)
        if all(_filler(line) for line in self.lines[top + 1 : end]):
            del self.lines[top:end]
