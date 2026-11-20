"""Group skill copies by name across a fleet and decide how each name can be shared.

Pure stdlib, no Hermes imports. Only movable copies (the profile's own skills
and bundled or hub skills the user edited, never anything reached through a
symlink) take part in the identical / containment / conflict decision; pinned
copies (pristine bundled or hub installs, symlinks) stay listed in the group.

Containment is judged file by file (see _contained) and errs towards
'conflict', because setup later retires every contained copy.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from commons.core.fleet import DEFAULT_PROFILE
from commons.core.scan import SKILL_FILE, SkillCopy, _hash_entries

Status = Literal[
    "identical", "contained", "conflict", "stays-local", "in-profile-dup", "unreadable", "nested"
]

MOVABLE_KINDS = frozenset({"own", "bundled-edited", "hub-edited"})
TAKEN_OVER_KINDS = frozenset({"bundled-edited", "hub-edited"})

_SNIFF = 8 * 1024
_TRAILING_WS = " \t\r"
_VOLATILE_KEY = re.compile(r"^(version|updated|last_updated):")


@dataclass
class Group:
    name: str
    copies: list[SkillCopy]
    status: Status
    keep: SkillCopy | None = None
    unique_lines: dict[str, int] = field(default_factory=dict)
    unique_files: dict[str, list[str]] = field(default_factory=dict)
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


def _text_lines(data: bytes, skill_md: bool) -> tuple[str, ...]:
    """Lines of a text file as compared for containment.

    CRLF becomes LF, a final newline does not make an extra empty line, and
    only trailing spaces, tabs and CRs are stripped: indentation, blank lines,
    short lines and code-fence tags all count. In SKILL.md only, lines of the
    leading --- block whose key is version:, updated: or last_updated: are
    dropped, since a version bump alone loses nothing.
    """
    text = data.decode("utf-8").replace("\r\n", "\n")
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    lines = [line.rstrip(_TRAILING_WS) for line in raw]
    if skill_md and lines and lines[0].lstrip("\ufeff") == "---":
        end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
        if end is not None:
            lines = [ln for i, ln in enumerate(lines) if not (0 < i < end and _VOLATILE_KEY.match(ln))]
    return tuple(lines)


@dataclass(frozen=True)
class _File:
    kind: Literal["text", "binary", "link"]
    executable: bool
    lines: tuple[str, ...] = ()  # text files only
    digest: str = ""  # sha256 of the bytes (binary) or of the link target (link)


def _is_executable(full: str) -> bool:
    return bool(os.stat(full).st_mode & 0o111)


def _read_file(rel: str, full: str) -> _File:
    with open(full, "rb") as fh:
        data = fh.read()
    executable = _is_executable(full)
    if is_text(data):
        return _File("text", executable, lines=_text_lines(data, rel == SKILL_FILE))
    return _File("binary", executable, digest=hashlib.sha256(data).hexdigest())


def _content(directory: Path) -> dict[str, _File]:
    """Every file and inner symlink of a skill tree by POSIX rel path, walked like scan.tree_hash.

    Raises OSError when any part of the tree cannot be read.
    """
    entries, error = _hash_entries(Path(directory))
    if error:
        raise OSError(error)
    out: dict[str, _File] = {}
    for rel, kind, full in sorted(entries):
        if kind == "L":
            target = os.readlink(full).encode("utf-8", "surrogateescape")
            out[rel] = _File("link", False, digest=hashlib.sha256(target).hexdigest())
        elif stat.S_ISREG(os.lstat(full).st_mode):
            out[rel] = _read_file(rel, full)
    return out


def file_lines(path: Path) -> Counter[str]:
    """The compared lines of one text file as a multiset (see _text_lines).

    A file named SKILL.md gets the frontmatter exemption. Raises ValueError
    for a binary file and OSError when it cannot be read.
    """
    path = Path(path)
    data = path.read_bytes()
    if not is_text(data):
        raise ValueError(f"not a text file: {path}")
    return Counter(_text_lines(data, path.name == SKILL_FILE))


def _is_subsequence(small: tuple[str, ...], big: tuple[str, ...]) -> bool:
    it = iter(big)
    return all(line in it for line in small)


def _file_contained(a: _File, b: _File) -> bool:
    if a.kind != b.kind or a.executable != b.executable:
        return False
    if a.kind == "text":
        return _is_subsequence(a.lines, b.lines)
    return a.digest == b.digest


def _contained(a: dict[str, _File], b: dict[str, _File]) -> bool:
    """True when copy *a* loses nothing if only copy *b* is kept.

    Per file: every rel path of *a* exists in *b* (empty files too, so a
    renamed or added file is never contained), with the same kind and the
    same executable bit; binaries and inner symlinks must be identical;
    a text file's lines must appear in *b*'s file of the same path in the
    same order, with any lines inserted around them. Requiring order is a
    deliberate choice over a plain multiset check: a copy whose steps were
    reordered is a different skill, and soundness beats recall here, since a
    false 'contained' retires a copy. merge --accept is the second check.
    """
    return all(rel in b and _file_contained(f, b[rel]) for rel, f in a.items())


def _size(content: dict[str, _File]) -> tuple[int, int]:
    return (sum(len(f.lines) for f in content.values()), len(content))


def _unique(c: str, survivors: list[str], contents: dict[str, dict[str, _File]]) -> tuple[int, list[str]]:
    """(lines of *c* no other survivor has in the same file, multiset; files only *c* has)."""
    others = [contents[o] for o in survivors if o != c]
    lines = 0
    only: list[str] = []
    for rel, f in contents[c].items():
        if not any(rel in o for o in others):
            only.append(rel)
        if f.kind != "text":
            continue
        seen: Counter[str] = Counter()
        for o in others:
            if rel in o and o[rel].kind == "text":
                seen |= Counter(o[rel].lines)
        lines += sum((Counter(f.lines) - seen).values())
    return lines, sorted(only)


def _exec_bits(copy: SkillCopy) -> frozenset[str]:
    entries, _ = _hash_entries(copy.path)
    return frozenset(rel for rel, kind, full in entries if kind == "F" and _is_executable(full))


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
    try:
        # tree_hash ignores file modes, so equal hashes still need equal executable bits.
        if len({c.tree_hash for c in movable}) == 1 and len({_exec_bits(c) for c in movable}) == 1:
            result.status = "identical"
            result.keep = movable[0]
            return result
        contents = {c.id: _content(c.path) for c in movable}
    except OSError:
        result.status = "unreadable"
        return result
    rank = {c.id: i for i, c in enumerate(movable)}
    # Smallest copy first; on equal size the later copy goes first, so of two
    # copies that contain each other the earlier profile's copy survives.
    pending = sorted(movable, key=lambda c: (_size(contents[c.id]), -rank[c.id]))
    survivors = list(movable)
    for a in pending:
        if any(b is not a and _contained(contents[a.id], contents[b.id]) for b in survivors):
            survivors.remove(a)
    if len(survivors) == 1:
        result.status = "contained"
        result.keep = survivors[0]
        return result

    result.status = "conflict"
    ids = [c.id for c in survivors]
    for cid in ids:
        result.unique_lines[cid], result.unique_files[cid] = _unique(cid, ids, contents)
    return result


def group(copies: Iterable[SkillCopy]) -> list[Group]:
    """Group copies by resolved skill name and classify each group, sorted by name.

    Rules, first match wins, over the movable copies (kind own, bundled-edited
    or hub-edited and not via_symlink): none -> stays-local; any with an error
    -> unreadable; any nested in another skill -> nested; two with different
    trees in one profile -> in-profile-dup; one tree hash -> identical (keep
    the first copy in profile order, executable bits equal too); one copy left
    after dropping every copy contained in another (see _contained) ->
    contained (keep it); otherwise conflict, with per surviving copy the count
    of lines no other survivor has in the same file (multiset) and the files
    no other survivor has.
    """
    by_name: dict[str, list[SkillCopy]] = {}
    for c in copies:
        by_name.setdefault(c.name, []).append(c)
    return [_decide(name, members) for name, members in sorted(by_name.items())]
