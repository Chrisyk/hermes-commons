"""Group skill copies by name across a fleet and decide how each name can be shared.

Pure stdlib, no Hermes imports. Only movable copies (the profile's own skills
and bundled or hub skills the user edited, never anything reached through a
symlink) take part in the identical / containment / conflict decision; pinned
copies (pristine bundled or hub installs, symlinks) stay listed in the group.

Containment is judged file by file (see _contained) and errs towards
'conflict', because setup later retires every contained copy.

Grouping reads its own content view of each copy (see _content), not the
scan.tree_hash walk: tree_hash mirrors Hermes and skips EXCLUDED_SKILL_DIRS
and all *.pyc/*.pyo, but a retired copy loses those files too, so here only
regenerable runtime caches are ignored and everything else (.git, .github,
node_modules, .venv, ...) counts, compared byte for byte. Directories count
too: a folder (empty or not) that only one copy has is content. The only
accepted normalizations, for containment only, are those of _text_lines:

- CRLF read as LF;
- trailing spaces, tabs and CRs stripped from each line;
- in SKILL.md, frontmatter version:, updated: and last_updated: lines ignored.

Whether a non-empty text file ends with a newline is NOT normalized: copies
that differ in it are neither contained nor identical. 'identical' compares
raw bytes and so accepts no normalization at all. merge --accept and the
setup backups cover the accepted ones.
"""

from __future__ import annotations

import codecs
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
from commons.core.scan import EXCLUDED_SKILL_DIRS, SKILL_FILE, SkillCopy

Status = Literal[
    "identical", "contained", "conflict", "stays-local", "in-profile-dup", "unreadable", "nested"
]

MOVABLE_KINDS = frozenset({"own", "bundled-edited", "hub-edited"})
TAKEN_OVER_KINDS = frozenset({"bundled-edited", "hub-edited"})

# Mirror of Hermes tools/skills_sync_optional.py _RUNTIME_CACHE_DIRS: the only
# excluded dirs whose loss is harmless, since they are regenerated on use.
_RUNTIME_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
# Every other Hermes-excluded dir is kept and compared as raw bytes.
_OPAQUE_DIRS = EXCLUDED_SKILL_DIRS - _RUNTIME_CACHE_DIRS
_BYTECODE_SUFFIXES = (".pyc", ".pyo")
_CHUNK = 1 << 20

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
    """True unless *data* has a NUL anywhere or is not strict UTF-8."""
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _read_stream(full: str) -> tuple[str, str | None]:
    """(sha256 of the bytes, decoded text or None when binary), reading 1 MiB chunks.

    Binary means a NUL anywhere or invalid strict UTF-8. Once a file is known
    to be binary its text is dropped and the rest is only hashed.
    """
    h = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    parts: list[str] | None = []
    with open(full, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            if parts is None:
                continue
            if b"\0" in chunk:
                parts = None
                continue
            try:
                parts.append(decoder.decode(chunk))
            except UnicodeDecodeError:
                parts = None
    if parts is not None:
        try:
            parts.append(decoder.decode(b"", final=True))
        except UnicodeDecodeError:
            parts = None
    return h.hexdigest(), None if parts is None else "".join(parts)


def is_text_file(path: Path) -> bool:
    """is_text for a file on disk, streamed in chunks (a NUL anywhere makes it binary)."""
    return _read_stream(os.fspath(path))[1] is not None


def _text_lines(text: str, skill_md: bool) -> tuple[str, ...]:
    """Lines of a text file as compared for containment.

    CRLF becomes LF, a final newline does not make an extra empty line (its
    presence is tracked apart, see _File.final_newline), and only trailing
    spaces, tabs and CRs are stripped: indentation, blank lines, short lines
    and code-fence tags all count. In SKILL.md only, lines of the leading ---
    block whose key is version:, updated: or last_updated: are dropped, since
    a version bump alone loses nothing.
    """
    text = text.replace("\r\n", "\n")
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
    kind: Literal["text", "binary", "link", "dir"]
    executable: bool
    lines: tuple[str, ...] = ()  # text files only
    digest: str = ""  # sha256 of the bytes (files) or of the link target (link)
    final_newline: bool = False  # text files only: the bytes end with a newline


_DIR = _File("dir", False)


def _is_executable(full: str) -> bool:
    return bool(os.stat(full).st_mode & 0o111)


def _read_opaque(full: str) -> _File:
    h = hashlib.sha256()
    with open(full, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return _File("binary", _is_executable(full), digest=h.hexdigest())


def _read_file(rel: str, full: str) -> _File:
    digest, text = _read_stream(full)
    executable = _is_executable(full)
    if text is None:
        return _File("binary", executable, digest=digest)
    lines = _text_lines(text, rel == SKILL_FILE)
    return _File("text", executable, lines=lines, digest=digest, final_newline=text.endswith("\n"))


def _is_runtime_bytecode(full: str) -> bool:
    """A *.pyc/*.pyo next to its .py source, as Hermes _is_runtime_cache; source-less bytecode is content."""
    root, ext = os.path.splitext(full)
    return ext in _BYTECODE_SUFFIXES and os.path.isfile(root + ".py")


def _content_entries(top: str) -> list[tuple[str, str, str]]:
    """(rel, kind, path) of every file ('F'), folder ('D') and inner symlink ('L') but runtime caches.

    Unlike scan._hash_entries, which mirrors Hermes hashing, this keeps every
    EXCLUDED_SKILL_DIRS entry except _RUNTIME_CACHE_DIRS and keeps source-less
    bytecode. A symlink named like a cache dir is kept (as a link): only real
    cache dirs are regenerable. Raises OSError on any walk error.
    """
    entries: list[tuple[str, str, str]] = []
    errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(top, onerror=errors.append, followlinks=False):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                entries.append((os.path.relpath(full, top).replace(os.sep, "/"), "L", full))
            elif d not in _RUNTIME_CACHE_DIRS:
                entries.append((os.path.relpath(full, top).replace(os.sep, "/") + "/", "D", full))
                keep.append(d)
        dirnames[:] = keep
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            if os.path.islink(full):
                entries.append((os.path.relpath(full, top).replace(os.sep, "/"), "L", full))
            elif not _is_runtime_bytecode(full):
                entries.append((os.path.relpath(full, top).replace(os.sep, "/"), "F", full))
    if errors:
        raise errors[0]
    return entries


def _content(directory: Path) -> dict[str, _File]:
    """The grouping view of a skill tree: every entry of _content_entries by POSIX rel path.

    Folders are keyed by their rel path plus a trailing '/', so they never
    collide with a file of the same name.
    A file under an _OPAQUE_DIRS dir (.git, node_modules, ...) is read
    as binary whatever its bytes, so it only counts as contained when the
    bytes match exactly. Non-regular files (FIFOs, sockets) are skipped as in
    scan.tree_hash. Raises OSError when any part of the tree cannot be read.
    """
    out: dict[str, _File] = {}
    for rel, kind, full in sorted(_content_entries(os.fspath(directory))):
        if kind == "L":
            target = os.readlink(full).encode("utf-8", "surrogateescape")
            out[rel] = _File("link", False, digest=hashlib.sha256(target).hexdigest())
        elif kind == "D":
            out[rel] = _DIR
        elif stat.S_ISREG(os.lstat(full).st_mode):
            opaque = any(part in _OPAQUE_DIRS for part in rel.split("/")[:-1])
            out[rel] = _read_opaque(full) if opaque else _read_file(rel, full)
    return out


def _identity(content: dict[str, _File]) -> frozenset[tuple[str, str, bool, str]]:
    """Exact identity of a content view: rel path, kind, executable bit and raw-byte digest per entry.

    Stricter than scan.tree_hash, which skips EXCLUDED_SKILL_DIRS and ignores
    file modes, so two copies differing only in .git or an executable bit are
    not 'identical' here.
    """
    return frozenset((rel, f.kind, f.executable, f.digest) for rel, f in content.items())


def file_lines(path: Path) -> Counter[str]:
    """The compared lines of one text file as a multiset (see _text_lines).

    Kept for the merge phase: merge validation checks a merged skill file by
    file against every source copy and needs these per-file lines. A file
    named SKILL.md gets the frontmatter exemption. Raises ValueError
    for a binary file and OSError when it cannot be read.
    """
    path = Path(path)
    _, text = _read_stream(os.fspath(path))
    if text is None:
        raise ValueError(f"not a text file: {path}")
    return Counter(_text_lines(text, path.name == SKILL_FILE))


def _is_subsequence(small: tuple[str, ...], big: tuple[str, ...]) -> bool:
    it = iter(big)
    return all(line in it for line in small)


def _file_contained(a: _File, b: _File) -> bool:
    if a.kind != b.kind or a.executable != b.executable:
        return False
    if a.kind == "text":
        # An empty file has no final newline to lose; any other difference in it counts.
        if a.lines and a.final_newline != b.final_newline:
            return False
        return _is_subsequence(a.lines, b.lines)
    return a.digest == b.digest


def _contained(a: dict[str, _File], b: dict[str, _File]) -> bool:
    """True when copy *a* loses nothing if only copy *b* is kept.

    Per entry: every rel path of *a* exists in *b* (empty files and folders
    too, so a renamed or added file or folder is never contained), with the
    same kind and the same executable bit; binaries and inner symlinks must
    be identical; a non-empty text file must agree on ending with a newline,
    and its lines must appear in *b*'s file of the same path in the same
    order, with any lines inserted around them. Requiring order is a
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
    # A folder is listed only when nothing listed lies below it (the deepest entry says more).
    only = [
        rel for rel in only if not (rel.endswith("/") and any(o != rel and o.startswith(rel) for o in only))
    ]
    return lines, sorted(only)


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
    try:
        contents = {c.id: _content(c.path) for c in movable}
    except OSError:
        result.status = "unreadable"
        return result
    identity = {c.id: _identity(contents[c.id]) for c in movable}
    by_profile: dict[str, set[frozenset]] = {}
    for c in movable:
        by_profile.setdefault(c.profile, set()).add(identity[c.id])
    if any(len(ids) > 1 for ids in by_profile.values()):
        result.status = "in-profile-dup"
        return result
    # Equal content views, not just equal tree_hash: tree_hash skips .git and
    # friends and ignores file modes (see _identity).
    if len({c.tree_hash for c in movable}) == 1 and len(set(identity.values())) == 1:
        result.status = "identical"
        result.keep = movable[0]
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
    content views (see _content) in one profile -> in-profile-dup; one tree
    hash and one exact content view (bytes, executable bits, excluded dirs
    such as .git included) -> identical (keep the first copy in profile
    order); one copy left
    after dropping every copy contained in another (see _contained) ->
    contained (keep it); otherwise conflict, with per surviving copy the count
    of lines no other survivor has in the same file (multiset) and the files
    no other survivor has.
    """
    by_name: dict[str, list[SkillCopy]] = {}
    for c in copies:
        by_name.setdefault(c.name, []).append(c)
    return [_decide(name, members) for name, members in sorted(by_name.items())]
