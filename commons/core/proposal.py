"""The setup proposal: what setup would move, retire, change and leave alone.

build_proposal is pure apart from reading skill trees: it scans every
profile and the shared root, groups the copies (group.py) and turns each
group into a Move, a Conflict or a name that is only reported. Nothing is
written; apply.py carries the proposal out.

Rules that are not obvious from the field names:

- The shared root's own copies join the grouping under the label 'shared'
  (so an existing shared root is adopted, not overwritten). A shared copy is
  never moved or retired. When it is the kept copy, a Move has no copy step
  (source_profile 'shared') and only retires profile copies.
- A Move's retire list holds every movable profile copy of the name,
  including the source copy itself: after the copy into the shared root the
  source would otherwise shadow it (the local tier wins in Hermes). Pinned
  copies (pristine bundled or hub installs) and symlinked copies stay.
- A name whose shared copy differs from the copy grouping would keep is a
  conflict, never a move: setup does not overwrite the shared root.
- Two moves whose targets are equal or nested in each other (or in another
  name's shared copy) cannot both land; the later one by name is 'blocked'.
- cron_rewrites is a preview from the given jobs; apply recomputes them
  from the jobs.json on disk at apply time with the same rule.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path, PurePosixPath
from typing import Any

from commons.core.fleet import Profile
from commons.core.group import Group, compare_variants, group, is_movable, profile_order, same_content
from commons.core.layout import Layout, check_outside
from commons.core.paths import find_stale_paths
from commons.core.resolve import config_path
from commons.core.scan import SkillCopy, scan_profile, scan_root

SHARED = "shared"
PLUGIN = "commons"
CREATE_DIR_KEY = "skills.create_dir"
PLUGINS_KEY = "plugins.enabled"
RESERVED_TOP = frozenset({".commons", ".git"})


@dataclass
class Move:
    name: str
    source_profile: str  # SHARED when the shared copy is kept (nothing to copy)
    source_rel: str
    target_rel: str
    retire: list[tuple[str, str]] = field(default_factory=list)  # (profile, rel)


@dataclass
class Variant:
    profile: str
    rel: str
    unique_lines: int
    unique_files: list[str] = field(default_factory=list)


@dataclass
class CopyRef:
    profile: str
    rel: str
    kind: str
    origin_hash: str | None = None


@dataclass
class Conflict:
    name: str
    variants: list[Variant]
    action: str = "merge"
    copies: list[CopyRef] = field(default_factory=list)  # every movable profile copy
    shared_rel: str | None = None  # the shared root's copy, if any


@dataclass
class ConfigChange:
    key: str
    old: Any
    new: Any


@dataclass
class CronRewrite:
    profile: str
    job_id: str
    field: str
    old_path: str
    new_path: str


@dataclass
class TakenOver:
    name: str
    profile: str
    kind: str
    origin_hash: str | None


@dataclass
class Summary:
    names: int = 0
    differing: int = 0
    contained: int = 0
    conflicts: int = 0
    identical: int = 0
    moves: int = 0
    retires: int = 0


@dataclass
class Proposal:
    shared_root: Path
    state_dir: Path
    moves: list[Move] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    stays_local: list[str] = field(default_factory=list)
    in_profile_dups: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    nested: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    config_changes: dict[str, list[ConfigChange]] = field(default_factory=dict)
    cron_rewrites: list[CronRewrite] = field(default_factory=list)
    taken_over: list[TakenOver] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)
    profile_homes: dict[str, Path] = field(default_factory=dict)

    @property
    def layout(self) -> Layout:
        return Layout(Path(self.shared_root), Path(self.state_dir))

    def to_json(self) -> str:
        data = asdict(self)
        data["shared_root"] = str(self.shared_root)
        data["state_dir"] = str(self.state_dir)
        data["profile_homes"] = {k: str(v) for k, v in self.profile_homes.items()}
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Proposal:
        d = json.loads(text)
        return cls(
            shared_root=Path(d["shared_root"]),
            state_dir=Path(d["state_dir"]),
            moves=[
                Move(m["name"], m["source_profile"], m["source_rel"], m["target_rel"], _pairs(m["retire"]))
                for m in d["moves"]
            ],
            conflicts=[
                Conflict(
                    c["name"],
                    [Variant(**v) for v in c["variants"]],
                    c["action"],
                    [CopyRef(**r) for r in c["copies"]],
                    c["shared_rel"],
                )
                for c in d["conflicts"]
            ],
            stays_local=list(d["stays_local"]),
            in_profile_dups=list(d["in_profile_dups"]),
            unreadable=list(d["unreadable"]),
            nested=list(d["nested"]),
            blocked=list(d["blocked"]),
            config_changes={
                p: [ConfigChange(**c) for c in changes] for p, changes in d["config_changes"].items()
            },
            cron_rewrites=[CronRewrite(**r) for r in d["cron_rewrites"]],
            taken_over=[TakenOver(**t) for t in d["taken_over"]],
            summary=Summary(**{f.name: d["summary"][f.name] for f in fields(Summary)}),
            profile_homes={k: Path(v) for k, v in d["profile_homes"].items()},
        )


def _pairs(items: Iterable) -> list[tuple[str, str]]:
    return [(str(a), str(b)) for a, b in items]


# ------------------------------------------------------------------ building


def _overlaps(a: str, b: str) -> bool:
    pa, pb = PurePosixPath(a), PurePosixPath(b)
    return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)


def _taken(copies: Iterable[SkillCopy | CopyRef], name: str) -> list[TakenOver]:
    return [
        TakenOver(name, c.profile, c.kind, c.origin_hash)
        for c in copies
        if c.kind in ("bundled-edited", "hub-edited")
    ]


def _variants(g: Group, survivors: list[SkillCopy]) -> list[Variant]:
    if g.status == "conflict" and all(c.id in g.unique_lines for c in survivors):
        stats = {c.id: (g.unique_lines[c.id], g.unique_files.get(c.id, [])) for c in survivors}
    else:
        stats = compare_variants(survivors)
    return [Variant(c.profile, c.rel, stats[c.id][0], list(stats[c.id][1])) for c in survivors]


def _differs(g: Group, movable: list[SkillCopy]) -> bool:
    if g.status in ("contained", "conflict", "in-profile-dup"):
        return True
    if g.status == "identical":
        return False
    return len({c.tree_hash for c in movable}) > 1


def _config_changes(profiles: list[Profile], shared_root: Path) -> dict[str, list[ConfigChange]]:
    out: dict[str, list[ConfigChange]] = {}
    target = config_path(Path("/"), shared_root)
    for p in profiles:
        changes: list[ConfigChange] = []
        skills = p.config.get("skills") if isinstance(p.config.get("skills"), dict) else {}
        raw = skills.get("create_dir")
        current = p.create_dir()
        if current is None or config_path(p.home, current) != target:
            changes.append(ConfigChange(CREATE_DIR_KEY, raw, str(shared_root)))
        enabled = p.plugins_enabled()
        if PLUGIN not in enabled:
            plugins = p.config.get("plugins") if isinstance(p.config.get("plugins"), dict) else {}
            old = plugins.get("enabled")
            changes.append(ConfigChange(PLUGINS_KEY, old, [*enabled, PLUGIN]))
        if changes:
            out[p.name] = changes
    return out


def build_proposal(
    profiles: list[Profile],
    layout: Layout,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
    cron_jobs: Mapping[str, list[dict]],
    home: Path | None = None,
) -> Proposal:
    """Propose setup for *profiles* into layout.shared_root (see module docstring).

    *cron_jobs* maps profile name to its already-loaded job dicts (see
    paths.py for the jobs.json schema); *home* anchors ~ paths (default the
    user's home). Raises ValueError for a bad layout, a profile named
    'shared', or a shared root that overlaps a profile skills dir.
    """
    profiles = list(profiles)
    if any(p.name == SHARED for p in profiles):
        raise ValueError(f"a profile named {SHARED!r} collides with the shared root's label")
    layout.check(profiles)
    shared_root = Path(layout.shared_root)
    check_outside(
        shared_root,
        "the shared root",
        [(f"the skills dir of profile {p.name}", p.skills_dir.resolve()) for p in profiles],
    )
    by_name = {p.name: p for p in profiles}
    copies: list[SkillCopy] = []
    for p in profiles:
        copies += scan_profile(p, md5_dir_hash, hub_hash)
    if shared_root.is_dir():
        copies += scan_root(shared_root, SHARED, md5_dir_hash, hub_hash)
    groups = group(copies)

    prop = Proposal(shared_root=shared_root, state_dir=Path(layout.state_dir))
    prop.profile_homes = {p.name: p.home for p in profiles}
    prop.config_changes = _config_changes(profiles, shared_root)
    summary = prop.summary
    summary.names = len(groups)
    # Targets already taken in the shared root, by name.
    claimed: list[tuple[str, str]] = [(c.rel, c.name) for c in copies if c.profile == SHARED]
    pending: list[tuple[Group, Move, str]] = []

    for g in groups:
        movable = [c for c in g.copies if is_movable(c)]
        shared = [c for c in movable if c.profile == SHARED]
        local = sorted(
            (c for c in movable if c.profile != SHARED), key=lambda c: (profile_order(c.profile), c.rel)
        )
        if _differs(g, movable):
            summary.differing += 1
        status = g.status
        if status == "stays-local":
            prop.stays_local.append(g.name)
            continue
        if status == "in-profile-dup":
            prop.in_profile_dups.append(g.name)
            continue
        if status == "unreadable":
            prop.unreadable.append(g.name)
            continue
        if status == "nested":
            prop.nested.append(g.name)
            continue
        refs = [CopyRef(c.profile, c.rel, c.kind, c.origin_hash) for c in local]
        shared_rel = shared[0].rel if shared else None
        if status in ("identical", "contained"):
            keep = g.keep
            assert keep is not None
            if keep.profile != SHARED and shared and not same_content(keep.path, shared[0].path):
                prop.conflicts.append(
                    Conflict(g.name, _variants(g, [keep, shared[0]]), copies=refs, shared_rel=shared_rel)
                )
                continue
            if shared:
                move = Move(g.name, SHARED, shared[0].rel, shared[0].rel, [(c.profile, c.rel) for c in local])
                if not move.retire:
                    continue
            else:
                move = Move(g.name, keep.profile, keep.rel, keep.rel, [(c.profile, c.rel) for c in local])
            pending.append((g, move, status))
            continue
        # conflict
        survivors = [c for c in g.copies if c.id in g.unique_lines]
        prop.conflicts.append(Conflict(g.name, _variants(g, survivors), copies=refs, shared_rel=shared_rel))

    for g, move, status in pending:
        if move.source_profile != SHARED:
            clash = [n for rel, n in claimed if n != g.name and _overlaps(rel, move.target_rel)]
            # Never land a skill in the shared root's own state or git folders.
            if clash or PurePosixPath(move.target_rel).parts[0] in RESERVED_TOP:
                prop.blocked.append(g.name)
                continue
            claimed.append((move.target_rel, g.name))
        prop.moves.append(move)
        if status == "identical":
            summary.identical += 1
        else:
            summary.contained += 1
        local = [c for c in g.copies if is_movable(c) and c.profile != SHARED]
        prop.taken_over += _taken(local, g.name)

    prop.cron_rewrites = _cron_preview(prop, by_name, cron_jobs, home)
    _recount(prop)
    return prop


def _source_dir(prop: Proposal, move: Move) -> Path:
    if move.source_profile == SHARED:
        return Path(prop.shared_root) / move.target_rel
    return Path(prop.profile_homes[move.source_profile]) / "skills" / move.source_rel


def moved_paths(prop: Proposal) -> dict[Path, Path]:
    """Old profile skill folder -> new shared folder for every retire of every move."""
    out: dict[Path, Path] = {}
    for move in prop.moves:
        for profile, rel in move.retire:
            out[Path(prop.profile_homes[profile]) / "skills" / rel] = Path(prop.shared_root) / move.target_rel
    return out


def _cron_preview(
    prop: Proposal, by_name: Mapping[str, Profile], cron_jobs: Mapping[str, list[dict]], home: Path | None
) -> list[CronRewrite]:
    moved = moved_paths(prop)
    if not moved:
        return []
    sources = {Path(prop.shared_root) / m.target_rel: _source_dir(prop, m) for m in prop.moves}

    def exists_in_source(new: Path) -> bool:
        for target, src in sources.items():
            if new == target or new.is_relative_to(target):
                return (src / new.relative_to(target)).exists()
        return False

    found = find_stale_paths(
        list(by_name.values()),
        prop.shared_root,
        moved,
        cron_jobs,
        home=home,
        exists=exists_in_source,
        include_files=False,
    )
    return [
        CronRewrite(str(f.profile), str(f.job_id), str(f.field), f.old, f.new) for f in found if f.new_exists
    ]


def _recount(prop: Proposal) -> None:
    prop.summary.moves = len(prop.moves)
    prop.summary.retires = sum(len(m.retire) for m in prop.moves)
    prop.summary.conflicts = len(prop.conflicts)


# ------------------------------------------------------------------ choices


def apply_choice(prop: Proposal, choice: str) -> Proposal:
    """Return a copy of *prop* with one choice applied: 'S=keep:P' or 'S=merge'.

    keep:P turns conflict S into a Move keeping profile P's variant
    ('shared' keeps the shared root's copy) and retiring every movable
    profile copy of S. merge keeps S a conflict (also undoing an earlier keep
    is not supported: rebuild the proposal). Raises ValueError for an
    unknown name, an unknown profile, a malformed choice, or keep:P where the
    shared root already holds a different copy of S.
    """
    name, sep, action = choice.partition("=")
    name, action = name.strip(), action.strip()
    if not sep or not name:
        raise ValueError(f"bad choice {choice!r}: use S=keep:P or S=merge")
    out = copy.deepcopy(prop)
    conflict = next((c for c in out.conflicts if c.name == name), None)
    if conflict is None:
        raise ValueError(f"no conflict named {name!r} in the proposal")
    if action == "merge":
        conflict.action = "merge"
        return out
    verb, sep, who = action.partition(":")
    if verb != "keep" or not sep or not who:
        raise ValueError(f"bad choice {choice!r}: use S=keep:P or S=merge")
    variant = next((v for v in conflict.variants if v.profile == who), None)
    if variant is None:
        known = ", ".join(v.profile for v in conflict.variants)
        raise ValueError(f"{name}: no variant from profile {who!r} (variants: {known})")
    retire = [(c.profile, c.rel) for c in conflict.copies]
    if who == SHARED:
        move = Move(name, SHARED, variant.rel, variant.rel, retire)
    else:
        if conflict.shared_rel is not None:
            raise ValueError(
                f"{name}: the shared root already has a different copy; choose keep:{SHARED} or merge"
            )
        target = variant.rel
        clash = [m.name for m in out.moves if _overlaps(m.target_rel, target)]
        if clash:
            raise ValueError(f"{name}: target {target} overlaps the move of {', '.join(clash)}")
        move = Move(name, who, variant.rel, target, retire)
    out.conflicts.remove(conflict)
    out.moves.append(move)
    out.moves.sort(key=lambda m: m.name)
    out.taken_over += _taken(conflict.copies, name)
    out.taken_over.sort(key=lambda t: (t.name, profile_order(t.profile)))
    _recount(out)
    return out


# ------------------------------------------------------------------ report


def _fmt(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def render_markdown(p: Proposal) -> str:
    """A plain, readable setup report ending with the three choices."""
    s = p.summary
    out: list[str] = [
        "# commons setup proposal",
        "",
        f"Shared root: {p.shared_root}",
        f"State dir: {p.state_dir}",
        "",
    ]
    out += [
        "## Counts",
        "",
        f"- skill names: {s.names}",
        f"- names whose copies differ: {s.differing}",
        f"- identical everywhere (move): {s.identical}",
        f"- one copy contains the others (move): {s.contained}",
        f"- conflicts: {s.conflicts}",
        f"- moves: {s.moves}",
        f"- local copies retired into the backup: {s.retires}",
        "",
    ]
    out += ["## Moves", ""]
    if not p.moves:
        out.append("None.")
    for m in p.moves:
        if m.source_profile == SHARED:
            out.append(f"- {m.name}: already in the shared root at {m.target_rel}")
        else:
            out.append(f"- {m.name}: copy {m.source_profile}:{m.source_rel} to {m.target_rel}")
        if m.retire:
            out.append("  retire: " + ", ".join(f"{pr}:{rel}" for pr, rel in m.retire))
    out += ["", "## Conflicts", ""]
    if not p.conflicts:
        out.append("None.")
    for c in p.conflicts:
        out.append(f"- {c.name} ({c.action})")
        for v in c.variants:
            files = f", {len(v.unique_files)} unique files" if v.unique_files else ""
            out.append(f"  - {v.profile}:{v.rel}: {v.unique_lines} unique lines{files}")
    out += ["", "## Config changes", ""]
    if not p.config_changes:
        out.append("None.")
    for profile, changes in sorted(p.config_changes.items(), key=lambda kv: profile_order(kv[0])):
        for ch in changes:
            out.append(f"- {profile}: {ch.key}: {_fmt(ch.old)} -> {_fmt(ch.new)}")
    out += ["", "## Cron path rewrites", ""]
    if not p.cron_rewrites:
        out.append("None.")
    for r in p.cron_rewrites:
        out.append(f"- {r.profile} job {r.job_id} {r.field}: {r.old_path} -> {r.new_path}")
    if p.taken_over:
        out += ["", "## Bundled or hub skills taken over", ""]
        for t in p.taken_over:
            out.append(f"- {t.name} ({t.profile}, {t.kind}, origin {t.origin_hash or 'unknown'})")
    out += ["", "## Left alone", ""]
    for label, names in (
        ("stays local (pristine bundled or hub, or symlinked)", p.stays_local),
        ("two different copies in one profile", p.in_profile_dups),
        ("could not be read", p.unreadable),
        ("nested inside another skill", p.nested),
        ("target clashes with another skill in the shared root", p.blocked),
    ):
        out.append(f"- {label}: {', '.join(names) if names else 'none'}")
    out += [
        "",
        "## What next",
        "",
        "1. review: read this file, then change a conflict with",
        "   `hermes commons setup --choose S=keep:P` (keep profile P's copy of S)",
        "   or `--choose S=merge` (leave S for an agent merge). Repeat --choose as needed.",
        "2. apply: `hermes commons setup --apply` backs up every profile, then carries this out.",
        "3. cancel: do nothing; no file has been changed.",
        "",
    ]
    return "\n".join(out)
