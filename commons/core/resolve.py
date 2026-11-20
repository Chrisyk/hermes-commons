"""Which copy of each skill a profile actually loads, mirroring Hermes precedence.

Hermes is never imported. Mirrors Hermes v0.23.1:

Roots. agent/skill_utils.py get_skill_search_roots(local) builds (tier, dir)
roots in this order: every trusted project skills dir (tier 0, TIER_PROJECT),
the profile skills dir as given, unresolved (tier 1, TIER_LOCAL),
skills.create_dir when set and an existing directory (tier 2,
TIER_CREATE_DIR), then every skills.external_dirs entry that exists (tier 3,
TIER_EXTERNAL). create_dir and external entries are stripped, ${VAR} then ~
expanded, anchored at HERMES_HOME when relative and resolved; a create_dir
whose resolved path equals the resolved local skills dir counts as unset, and
an external entry equal to the resolved local dir or to an earlier external
entry is dropped. The final list drops any (tier, dir) whose dir Path equals
one already seen, compared as given (raw local, resolved others), keeping the
first. tools/skills_tool.py skill_search_dirs then drops the local root if it
does not exist.

Lookup. tools/skills_tool.py locate_skill(name, local_category_name, roots)
gathers candidates with _collect_skill_candidates, per root in root order:
the direct path root/<name> (a dir holding SKILL.md that is not under a
support dir of a skill, else the flat root/<name>.md), then every SKILL.md the
root's walk reaches (iter_skill_index_files, sorted by path) whose folder name
or YAML frontmatter name equals the request, then every legacy flat <name>.md
anywhere below the root (rglob, no pruning) that is not under a skill's
support dir and not owned by an ancestor directory skill. Candidates are
deduped by resolved path, first recorded wins. Each candidate belongs to the
most specific root containing it lexically (_owning_search_dir), which gives
its tier and the root its depth is ranked against. agent/skill_utils.py
pick_skill_candidate takes the lowest tier: a lone candidate wins; several
win only when they share one owning root and provably_same_skill holds (one
realpath or byte-identical files), and then only the strictly best
skill_candidate_rank (SKILL.md before a flat file, then fewest path parts
below the root); anything else is refused as ambiguous.

A request containing ':' first goes to the plugin registry, which this
module cannot see; it assumes no plugin answers, so 'ns:bare' falls through
as Hermes does then, with 'ns/bare' as the extra direct path. Names that
skill_lookup_path_error rejects (absolute, drive, '..') find nothing. The
project-tier quarantine gate is not mirrored.

resolve() keys its result by the declared name each SKILL.md carries in the
system-prompt index (prompt_builder _build_snapshot_entry / _entry_name), and
answers each key with locate().
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from commons.core.fleet import Profile
from commons.core.frontmatter import read_frontmatter
from commons.core.scan import SKILL_FILE, SKILL_SUPPORT_DIRS, SkillCopy, scan_root, tree_hash

TIER_PROJECT, TIER_LOCAL, TIER_CREATE_DIR, TIER_EXTERNAL = range(4)
TIER_NAMES = {
    TIER_PROJECT: "project",
    TIER_LOCAL: "local",
    TIER_CREATE_DIR: "shared",
    TIER_EXTERNAL: "external",
}

# Mirror of Hermes agent/skill_utils.py _NAMESPACE_RE.
_NAMESPACE = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class ScanCache:
    """Root scans and frontmatter reads shared between profiles, so each root is scanned once."""

    scans: dict[Path, list[SkillCopy]] = field(default_factory=dict)
    frontmatter: dict[tuple[Path, bool], dict] = field(default_factory=dict)
    mds: dict[Path, list[tuple[str, Path]]] = field(default_factory=dict)
    scan_calls: int = 0

    def md_files(self, root: Path) -> list[tuple[str, Path]]:
        if root not in self.mds:
            self.mds[root] = _md_files(root)
        return self.mds[root]

    def read(self, skill_md: Path, strict: bool = False) -> dict:
        key = (skill_md, strict)
        if key not in self.frontmatter:
            self.frontmatter[key] = read_frontmatter(skill_md, strict=strict)
        return self.frontmatter[key]


@dataclass
class Resolution:
    name: str
    winner: SkillCopy | None  # the winning directory skill; None for a legacy flat file
    winner_tier: int | None  # the deciding tier; set even when that tier is ambiguous
    shadowed: list[tuple[int, SkillCopy]] = field(default_factory=list)
    ambiguous: list[SkillCopy] = field(default_factory=list)
    winner_path: Path | None = None  # the file Hermes loads: a SKILL.md or a legacy <name>.md
    legacy: list[tuple[int, Path]] = field(default_factory=list)  # losing or tied legacy flat files

    @property
    def is_ambiguous(self) -> bool:
        return self.winner_path is None and self.winner_tier is not None


@dataclass(frozen=True)
class Root:
    tier: int
    path: Path
    label: str


def _resolve_path(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def config_path(profile: Path, path: Path) -> Path:
    """Mirror of Hermes _expand_path + _home_relative + resolve for a config entry."""
    expanded = Path(os.path.expandvars(os.fspath(path).strip())).expanduser()
    return _resolve_path(expanded if expanded.is_absolute() else profile / expanded)


def is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def search_roots(profile: Profile, project_dirs: Sequence[Path] = ()) -> list[Root]:
    """Mirror of Hermes get_skill_search_roots plus skill_search_dirs (see module docstring)."""
    local = profile.skills_dir
    local_resolved = _resolve_path(local)
    roots: list[tuple[int, Path]] = [(TIER_PROJECT, _resolve_path(Path(d))) for d in project_dirs]
    roots.append((TIER_LOCAL, local))
    create = profile.create_dir()
    if create is not None:
        create = config_path(profile.home, create)
        if create != local_resolved and is_dir(create):
            roots.append((TIER_CREATE_DIR, create))
    externals: list[Path] = []
    for entry in profile.external_dirs():
        p = config_path(profile.home, entry)
        if p != local_resolved and p not in externals and is_dir(p):
            externals.append(p)
    roots += [(TIER_EXTERNAL, p) for p in externals]

    out: list[Root] = []
    seen: set[Path] = set()
    counts: dict[int, int] = {}
    for tier, path in roots:
        if path in seen:
            continue
        seen.add(path)
        if tier == TIER_LOCAL and not path.exists():
            continue
        counts[tier] = counts.get(tier, 0) + 1
        out.append(Root(tier, path, _label(tier, counts[tier], profile.name)))
    return out


def _label(tier: int, index: int, profile_name: str) -> str:
    if tier == TIER_LOCAL:
        return profile_name
    base = TIER_NAMES[tier]
    return base if index == 1 else f"{base}-{index}"


def provably_same(paths: Sequence[Path]) -> bool:
    """Mirror of Hermes agent/skill_utils.py provably_same_skill."""
    try:
        if len({os.path.realpath(p) for p in paths}) == 1:
            return True
        return len({hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}) == 1
    except OSError:
        return False


def owning_root(path: Path, roots: Sequence[Root]) -> Root | None:
    """Mirror of Hermes tools/skills_tool.py _owning_search_dir: most specific root containing *path*, lexically."""
    owners = [r for r in roots if path.is_relative_to(r.path)]
    return max(owners, key=lambda r: len(r.path.parts), default=None)


def candidate_rank(file: Path, root: Path) -> tuple[bool, int]:
    """Mirror of Hermes agent/skill_utils.py skill_candidate_rank."""
    return (file.name != SKILL_FILE, len(file.relative_to(root).parts))


def is_support_path(path: Path) -> bool:
    """Mirror of Hermes agent/skill_utils.py is_skill_support_path for an absolute path."""
    parts = path.parts
    return any(
        part in SKILL_SUPPORT_DIRS and (Path(*parts[:idx]) / SKILL_FILE).exists()
        for idx, part in enumerate(parts[:-1])
        if idx > 0
    )


def _is_package_owned_markdown(path: Path, root: Path) -> bool:
    """Mirror of Hermes tools/skills_tool.py _is_package_owned_markdown."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(
        (root.joinpath(*relative.parts[:depth]) / SKILL_FILE).is_file()
        for depth in range(1, len(relative.parts))
    )


def _lookup_error(name: str) -> bool:
    """Mirror of Hermes tools/skills_tool.py skill_lookup_path_error (True = rejected)."""
    candidate = name.strip()
    win = PureWindowsPath(candidate)
    if PurePosixPath(candidate).is_absolute() or win.is_absolute() or win.drive:
        return True
    return ".." in Path(candidate).parts


def index_name(skill_md: Path, cache: ScanCache | None = None) -> str:
    """The name a SKILL.md is listed under in the Hermes system-prompt index.

    Mirror of Hermes agent/prompt_builder.py _parse_skill_file (strict UTF-8,
    {} on a read error), _build_snapshot_entry (str of frontmatter name,
    default the folder name) and _entry_name (empty falls back to the folder).
    """
    folder = skill_md.parent.name
    fm = cache.read(skill_md, strict=True) if cache is not None else read_frontmatter(skill_md, strict=True)
    return str(fm.get("name", folder)) or folder


@dataclass(frozen=True)
class _Found:
    """One SKILL.md a root's walk reaches, as the copy of its owning root."""

    file: Path
    copy: SkillCopy
    folder: str
    yaml_name: object


@dataclass(frozen=True)
class _Cand:
    root: Root
    file: Path
    copy: SkillCopy | None


@dataclass
class SkillIndex:
    """Everything locate() needs for one profile: its roots and what each root's walk reaches."""

    roots: list[Root]
    cache: ScanCache
    found: dict[Path, list[_Found]]  # root path -> SKILL.md entries sorted by path string
    mds: dict[Path, list[tuple[str, Path]]]  # root path -> every *.md below it (rglob order)
    _adhoc: dict[Path, SkillCopy] = field(default_factory=dict)

    def copies(self) -> Iterable[_Found]:
        for root in self.roots:
            yield from self.found[root.path]


def _scan(root: Root, md5_dir_hash, hub_hash, cache: ScanCache) -> list[SkillCopy]:
    """scan_root of *root*, once per root path when a *cache* is given (copies relabelled per profile)."""
    if root.path not in cache.scans:
        cache.scan_calls += 1
        cache.scans[root.path] = scan_root(root.path, root.label, md5_dir_hash, hub_hash)
    return [c if c.profile == root.label else replace(c, profile=root.label) for c in cache.scans[root.path]]


def _is_legacy_flat(p: Path, root: Path) -> bool:
    return p.name != SKILL_FILE and not is_support_path(p) and not _is_package_owned_markdown(p, root)


def _md_files(root: Path) -> list[tuple[str, Path]]:
    """(POSIX rel path, path) of every *.md below *root*, walked as pathlib rglob does."""
    try:
        return [(p.relative_to(root).as_posix(), p) for p in root.rglob("*.md")]
    except OSError:
        return []


def _flat_candidates(index: SkillIndex, root: Path, name: str) -> list[Path]:
    """Mirror of the rglob(f"{name}.md") step of _collect_skill_candidates.

    rglob is run once per root and filtered here; a name holding glob
    metacharacters runs the real rglob, so the pattern semantics match.
    """
    if any(ch in name for ch in "*?["):
        try:
            hits = list(root.rglob(f"{name}.md"))
        except (OSError, ValueError):
            return []
    else:
        tail = f"{name}.md"
        hits = [p for rel, p in index.mds[root] if rel == tail or rel.endswith("/" + tail)]
    return [p for p in hits if _is_legacy_flat(p, root)]


def build_index(
    profile: Profile,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
    project_dirs: Sequence[Path] = (),
    cache: ScanCache | None = None,
) -> SkillIndex:
    """Scan every search root of *profile* once; *cache* shares root scans between profiles."""
    cache = cache if cache is not None else ScanCache()
    roots = search_roots(profile, project_dirs)
    scans = {r.path: {c.path: c for c in _scan(r, md5_dir_hash, hub_hash, cache)} for r in roots}
    found: dict[Path, list[_Found]] = {}
    for root in roots:
        entries = []
        for path, copy in scans[root.path].items():
            owner = owning_root(path, roots)
            own = scans[owner.path].get(path, copy) if owner is not None else copy
            md = path / SKILL_FILE
            entries.append(_Found(md, own, path.name, cache.read(md).get("name")))
        found[root.path] = sorted(entries, key=lambda f: str(f.file))
    mds = {r.path: cache.md_files(r.path) for r in roots}
    return SkillIndex(roots, cache, found, mds)


def _adhoc_copy(index: SkillIndex, root: Root, path: Path) -> SkillCopy:
    """A SkillCopy for a direct-path hit no walk reaches (inside an excluded dir)."""
    if path not in index._adhoc:
        rel = path.relative_to(root.path).as_posix()
        index._adhoc[path] = SkillCopy(
            profile=root.label,
            name=path.name,
            rel=rel,
            path=path,
            kind="own",
            tree_hash=tree_hash(path),
            origin_hash=None,
        )
    return index._adhoc[path]


def _collect(index: SkillIndex, name: str, category: str | None) -> list[_Cand]:
    """Mirror of Hermes tools/skills_tool.py _collect_skill_candidates."""
    cands: list[_Cand] = []
    seen: set[str] = set()

    def record(root: Root, file: Path, copy: SkillCopy | None) -> None:
        key = os.path.realpath(file)
        if key not in seen:
            seen.add(key)
            owner = owning_root(file, index.roots) or root
            cands.append(_Cand(owner, file, copy))

    by_path = {f.copy.path: f.copy for f in index.copies()}
    for root in index.roots:
        for direct in filter(None, (name, category)):
            target = root.path / direct
            flat = target.with_suffix(".md")  # ValueError (an empty name) is Hermes' error path too
            if not is_support_path(target) and is_dir(target) and (target / SKILL_FILE).exists():
                owner = owning_root(target, index.roots) or root
                record(root, target / SKILL_FILE, by_path.get(target) or _adhoc_copy(index, owner, target))
            elif (
                flat.exists()
                and not is_support_path(flat)
                and not _is_package_owned_markdown(flat, root.path)
            ):
                record(root, flat, None)
        for f in index.found[root.path]:
            if f.folder == name or f.yaml_name == name:
                record(root, f.file, f.copy)
        for flat in _flat_candidates(index, root.path, name):
            record(root, flat, None)
    return cands


def _pick(name: str, cands: list[_Cand]) -> Resolution:
    """Mirror of Hermes agent/skill_utils.py pick_skill_candidate, as locate_skill applies it."""
    if not cands:
        return Resolution(name=name, winner=None, winner_tier=None)
    top = min(c.root.tier for c in cands)
    contenders = [c for c in cands if c.root.tier == top]
    won: _Cand | None = contenders[0] if len(contenders) == 1 else None
    one_root = len({c.root.path for c in contenders}) == 1
    if len(contenders) > 1 and one_root and provably_same([c.file for c in contenders]):
        ranks = sorted((candidate_rank(c.file, c.root.path), i) for i, c in enumerate(contenders))
        if ranks[0][0] != ranks[1][0]:
            won = contenders[ranks[0][1]]
    if won is not None and not won.file.exists():
        won = None
        contenders = []
    res = Resolution(name=name, winner=None, winner_tier=top if contenders else None)
    if won is not None:
        res.winner, res.winner_path = won.copy, won.file
    for c in cands:
        if c is won:
            continue
        if c.copy is None:
            res.legacy.append((c.root.tier, c.file))
        elif won is None and c.root.tier == top:
            res.ambiguous.append(c.copy)
        else:
            res.shadowed.append((c.root.tier, c.copy))
    return res


def locate(index: SkillIndex, name: str) -> Resolution:
    """Mirror of Hermes tools/skills_tool.py locate_skill for any request name (see module docstring)."""
    if not isinstance(name, str) or _lookup_error(name) or not index.roots:
        return Resolution(name=name, winner=None, winner_tier=None)
    category = None
    if ":" in name:
        namespace, _, bare = name.partition(":")
        if namespace == "plugin" and ":" in bare:
            namespace, _, bare = bare.partition(":")
        if not _NAMESPACE.match(namespace):
            return Resolution(name=name, winner=None, winner_tier=None)
        category = f"{namespace}/{bare}" if bare else None
        if category and _lookup_error(category):
            return Resolution(name=name, winner=None, winner_tier=None)
    try:
        cands = _collect(index, name, category)
    except ValueError:  # Hermes skill_view turns the same exception into an error
        return Resolution(name=name, winner=None, winner_tier=None)
    return _pick(name, cands)


def resolve_index(index: SkillIndex) -> dict[str, Resolution]:
    """Every declared skill name in *index*, answered as Hermes skill_view(name) would."""
    names = {index_name(f.file, index.cache) for f in index.copies()}
    return {name: locate(index, name) for name in sorted(names)}


def resolve(
    profile: Profile,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
    project_dirs: Sequence[Path] = (),
    cache: ScanCache | None = None,
) -> dict[str, Resolution]:
    """Every declared skill name visible to *profile*, answered as Hermes skill_view(name) would."""
    return resolve_index(build_index(profile, md5_dir_hash, hub_hash, project_dirs, cache))
