"""Group skill copies by name across a fleet and decide how each name can be shared.

Pure stdlib, no Hermes imports. Only movable copies (the profile's own skills
and bundled or hub skills the user edited, never anything reached through a
symlink) take part in the identical / containment / conflict decision; pinned
copies (pristine bundled or hub installs, symlinks) stay listed in the group.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from commons.core.fleet import DEFAULT_PROFILE
from commons.core.scan import SkillCopy, _hash_entries

Status = Literal[
    "identical", "contained", "conflict", "stays-local", "in-profile-dup", "unreadable", "nested"
]

MOVABLE_KINDS = frozenset({"own", "bundled-edited", "hub-edited"})
TAKEN_OVER_KINDS = frozenset({"bundled-edited", "hub-edited"})

_MIN_LINE = 4
_SNIFF = 8 * 1024
_TRIVIAL = re.compile(r"^(version:|updated:|last_updated:|---$|```\w*$|#+\s*$|[-*|>]\s*$)")


@dataclass
class Group:
    name: str
    copies: list[SkillCopy]
    status: Status
    keep: SkillCopy | None = None
    unique_lines: dict[str, int] = field(default_factory=dict)
    taken_over: bool = False


def profile_order(profile: str) -> tuple[bool, str]:
    """Stable fleet order: the default profile first, then the others alphabetically."""
    return (profile != DEFAULT_PROFILE, profile)


def _copy_order(copy: SkillCopy) -> tuple[tuple[bool, str], str]:
    return (profile_order(copy.profile), copy.rel)


def is_movable(copy: SkillCopy) -> bool:
    return copy.kind in MOVABLE_KINDS and not copy.via_symlink


def is_text(data: bytes) -> bool:
    """True unless *data* has a NUL in its first 8 KiB or is not strict UTF-8."""
    if b"\0" in data[:_SNIFF]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _lines_of(data: bytes) -> set[str]:
    # utf-8-sig drops a leading BOM so a first '---' line still reads as trivial.
    out = set()
    for raw in data.decode("utf-8-sig", errors="replace").splitlines():
        line = " ".join(raw.split())
        if len(line) >= _MIN_LINE and not _TRIVIAL.match(line):
            out.add(line)
    return out


@dataclass(frozen=True)
class _Content:
    lines: frozenset[str]
    opaque: dict[str, str]  # rel -> sha256 of bytes, for binary files and inner symlinks


def _content(directory: Path) -> _Content:
    """Normalized text lines and opaque-file digests of a skill tree, walked like scan.tree_hash.

    Raises OSError when any part of the tree cannot be read.
    """
    entries, error = _hash_entries(Path(directory))
    if error:
        raise OSError(error)
    lines: set[str] = set()
    opaque: dict[str, str] = {}
    for rel, kind, full in sorted(entries):
        if kind == "L":
            target = os.readlink(full).encode("utf-8", "surrogateescape")
            opaque[rel] = hashlib.sha256(b"L\0" + target).hexdigest()
            continue
        if not os.path.isfile(full):
            continue
        with open(full, "rb") as fh:
            data = fh.read()
        if is_text(data):
            lines |= _lines_of(data)
        else:
            opaque[rel] = hashlib.sha256(data).hexdigest()
    return _Content(frozenset(lines), opaque)


def normalized_lines(path: Path) -> set[str]:
    """Normalized, non-trivial lines of one text file, or of every text file in a skill tree.

    Each line is decoded as UTF-8 (errors replaced), internal whitespace is
    collapsed to single spaces, and lines shorter than 4 chars or matching the
    trivial pattern (version:, updated:, last_updated:, ---, code fences, bare
    headings and bare list/quote/table markers) are dropped. Binary files
    contribute nothing.
    """
    path = Path(path)
    if path.is_dir():
        return set(_content(path).lines)
    data = path.read_bytes()
    return _lines_of(data) if is_text(data) else set()


def _contained(a: _Content, b: _Content) -> bool:
    return a.lines <= b.lines and all(b.opaque.get(rel) == digest for rel, digest in a.opaque.items())


def _decide(name: str, copies: list[SkillCopy]) -> Group:
    copies = sorted(copies, key=_copy_order)
    taken_over = any(c.kind in TAKEN_OVER_KINDS for c in copies)
    result = Group(name=name, copies=copies, status="stays-local", taken_over=taken_over)
    movable = [c for c in copies if is_movable(c)]
    if not movable:
        return result
    if any(c.error for c in movable):
        result.status = "unreadable"
        return result
    if any(c.parent_rel is not None for c in movable):
        result.status = "nested"
        return result
    by_profile: dict[str, set[str]] = {}
    for c in movable:
        by_profile.setdefault(c.profile, set()).add(c.tree_hash)
    if any(len(hashes) > 1 for hashes in by_profile.values()):
        result.status = "in-profile-dup"
        return result
    if len({c.tree_hash for c in movable}) == 1:
        result.status = "identical"
        result.keep = movable[0]
        return result

    try:
        contents = {c.id: _content(c.path) for c in movable}
    except OSError:
        result.status = "unreadable"
        return result
    rank = {c.id: i for i, c in enumerate(movable)}
    # Smallest line set first; on equal size the later copy goes first, so of two
    # copies that contain each other the earlier profile's copy survives.
    pending = sorted(movable, key=lambda c: (len(contents[c.id].lines), -rank[c.id]))
    survivors = list(movable)
    for a in pending:
        if any(b is not a and _contained(contents[a.id], contents[b.id]) for b in survivors):
            survivors.remove(a)
    if len(survivors) == 1:
        result.status = "contained"
        result.keep = survivors[0]
        return result

    result.status = "conflict"
    for c in survivors:
        others = set().union(*(contents[o.id].lines for o in survivors if o is not c))
        result.unique_lines[c.id] = len(contents[c.id].lines - others)
    return result


def group(copies: Iterable[SkillCopy]) -> list[Group]:
    """Group copies by resolved skill name and classify each group, sorted by name.

    Rules, first match wins, over the movable copies (kind own, bundled-edited
    or hub-edited and not via_symlink): none -> stays-local; any with an error
    -> unreadable; any nested in another skill -> nested; two with different
    trees in one profile -> in-profile-dup; one tree hash -> identical (keep
    the first copy in profile order); one copy left after dropping every copy
    contained in another -> contained (keep it); otherwise conflict, with the
    count of lines each surviving copy has that no other survivor has.
    """
    by_name: dict[str, list[SkillCopy]] = {}
    for c in copies:
        by_name.setdefault(c.name, []).append(c)
    return [_decide(name, members) for name, members in sorted(by_name.items())]
