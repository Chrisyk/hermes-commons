"""Which copy of each skill a profile actually loads, mirroring Hermes precedence.

Stdlib only; Hermes is never imported. Mirrors Hermes v0.23.1:

agent/skill_utils.py get_skill_search_roots(local) builds (tier, dir) roots in
this order: every trusted project skills dir (tier 0, TIER_PROJECT), the
profile skills dir (tier 1, TIER_LOCAL), skills.create_dir when set and an
existing directory (tier 2, TIER_CREATE_DIR), then every skills.external_dirs
entry that exists (tier 3, TIER_EXTERNAL). create_dir and external entries are
~ and ${VAR} expanded, anchored at HERMES_HOME when relative and resolved; a
create_dir equal to the local skills dir counts as unset, and an external entry
equal to the local skills dir or to an earlier external entry is dropped. The
final list drops any dir already seen, keeping the first occurrence.
tools/skills_tool.py skill_search_dirs then drops the local root if it does
not exist.

tools/skills_tool.py locate_skill(name, ...) collects every candidate
SKILL.md across those roots in root order (_collect_skill_candidates), deduped
by resolved SKILL.md path so the first root to expose a file owns it; each
candidate's tier is that of the most specific root containing it lexically
(_owning_search_dir). agent/skill_utils.py pick_skill_candidate then takes the
lowest tier present. A lone candidate there wins. Several candidates win only
when they all sit under ONE root and provably_same_skill holds (one realpath
or byte-identical SKILL.md files); the strictly best skill_candidate_rank (the
shallowest SKILL.md) wins and a tie stays ambiguous. Anything else is refused
as ambiguous. Lower tiers are shadowed.

Differences, on purpose: copies are keyed by the declared name only (scan's
read_skill_name), not also by folder name or relative path as Hermes aliases
do, and legacy flat <name>.md files are not skills here.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from commons.core.fleet import Profile
from commons.core.scan import SKILL_FILE, SkillCopy, scan_root

TIER_PROJECT, TIER_LOCAL, TIER_CREATE_DIR, TIER_EXTERNAL = range(4)
TIER_NAMES = {
    TIER_PROJECT: "project",
    TIER_LOCAL: "local",
    TIER_CREATE_DIR: "shared",
    TIER_EXTERNAL: "external",
}


@dataclass
class Resolution:
    name: str
    winner: SkillCopy | None
    winner_tier: int | None  # the deciding tier; set even when that tier is ambiguous
    shadowed: list[tuple[int, SkillCopy]] = field(default_factory=list)
    ambiguous: list[SkillCopy] = field(default_factory=list)


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


def _config_path(profile: Path, path: Path) -> Path:
    """Mirror of Hermes _expand_path + _home_relative + resolve for a config entry."""
    expanded = Path(os.path.expandvars(os.fspath(path))).expanduser()
    return _resolve_path(expanded if expanded.is_absolute() else profile / expanded)


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def search_roots(profile: Profile, project_dirs: Sequence[Path] = ()) -> list[Root]:
    """Mirror of Hermes get_skill_search_roots plus skill_search_dirs (see module docstring)."""
    local = profile.skills_dir
    local_resolved = _resolve_path(local)
    roots: list[tuple[int, Path]] = [(TIER_PROJECT, _resolve_path(Path(d))) for d in project_dirs]
    if _is_dir(local):
        roots.append((TIER_LOCAL, local))
    create = profile.create_dir()
    if create is not None:
        create = _config_path(profile.home, create)
        if create != local_resolved and _is_dir(create):
            roots.append((TIER_CREATE_DIR, create))
    externals: list[Path] = []
    for entry in profile.external_dirs():
        p = _config_path(profile.home, entry)
        if p != local_resolved and p not in externals and _is_dir(p):
            externals.append(p)
    roots += [(TIER_EXTERNAL, p) for p in externals]

    out: list[Root] = []
    seen: set[Path] = set()
    counts: dict[int, int] = {}
    for tier, path in roots:
        key = _resolve_path(path)
        if key in seen:
            continue
        seen.add(key)
        counts[tier] = counts.get(tier, 0) + 1
        out.append(Root(tier, path, _label(tier, counts[tier], profile.name)))
    return out


def _label(tier: int, index: int, profile_name: str) -> str:
    if tier == TIER_LOCAL:
        return profile_name
    base = TIER_NAMES[tier]
    return base if index == 1 else f"{base}-{index}"


def provably_same(copies: Sequence[SkillCopy]) -> bool:
    """Mirror of Hermes agent/skill_utils.py provably_same_skill over the copies' SKILL.md files."""
    paths = [c.path / SKILL_FILE for c in copies]
    try:
        if len({os.path.realpath(p) for p in paths}) == 1:
            return True
        return len({hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}) == 1
    except OSError:
        return False


def _rank(copy: SkillCopy) -> int:
    """Mirror of Hermes skill_candidate_rank: path parts of SKILL.md below its root."""
    return 1 if copy.rel == "." else copy.rel.count("/") + 2


def _owner(path: Path, roots: list[Root]) -> Root:
    """Mirror of Hermes _owning_search_dir: the most specific root containing *path* lexically."""
    owners = [r for r in roots if path.is_relative_to(r.path)]
    return max(owners, key=lambda r: len(r.path.parts))


def _pick(name: str, cands: list[tuple[Root, SkillCopy]]) -> Resolution:
    """Mirror of Hermes pick_skill_candidate."""
    top = min(root.tier for root, _ in cands)
    contenders = [(root, c) for root, c in cands if root.tier == top]
    lower = [(root.tier, c) for root, c in cands if root.tier != top]
    res = Resolution(name=name, winner=None, winner_tier=top, shadowed=lower)
    if len(contenders) == 1:
        res.winner = contenders[0][1]
        return res
    same_root = len({root.path for root, _ in contenders}) == 1
    if same_root and provably_same([c for _, c in contenders]):
        ranked = sorted(contenders, key=lambda rc: _rank(rc[1]))
        if _rank(ranked[0][1]) != _rank(ranked[1][1]):
            res.winner = ranked[0][1]
            res.shadowed = [(top, c) for _, c in ranked[1:]] + lower
            return res
    res.ambiguous = [c for _, c in contenders]
    return res


def resolve(
    profile: Profile,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
    project_dirs: Sequence[Path] = (),
) -> dict[str, Resolution]:
    """Resolve every skill name visible to *profile*, keyed by name, as Hermes locate_skill does."""
    roots = search_roots(profile, project_dirs)
    by_name: dict[str, list[tuple[Root, SkillCopy]]] = {}
    seen_md: set[str] = set()
    for root in roots:
        for copy in scan_root(root.path, root.label, md5_dir_hash, hub_hash):
            key = os.path.realpath(copy.path / SKILL_FILE)
            if key in seen_md:
                continue
            seen_md.add(key)
            by_name.setdefault(copy.name, []).append((_owner(copy.path, roots), copy))
    return {name: _pick(name, cands) for name, cands in sorted(by_name.items())}
