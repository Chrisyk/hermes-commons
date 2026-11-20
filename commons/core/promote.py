"""Promote one profile's skill into the shared root, or demote a shared skill into one profile.

Both follow journal.operation (lock, backup of the touched profile, write-
ahead journal), so restore(plan_restore(layout, backup_id)) undoes either:

- promote: the profile copy is retired into the backup (intent 'retire'),
  so restore puts it back, and so is every pristine bundled or hub copy of
  the name in a profile that reads the shared root (left in place it would
  shadow the promoted copy; it is recorded in taken_over, see proposal.py); the copy into the shared root ('shared_copy')
  is moved aside into the pre-restore backup when it is unchanged, and
  restore commits that (git history keeps the promoted version).
- demote: the copy written into the profile is journaled as 'profile_copy'
  with the source's tree_sha256, so restore moves it aside into the
  pre-restore backup (never deleted) when it is unchanged. The
  intentional_overrides entry in .commons/config.json stays (restore
  reports it).

Both write their copy next to the target first and rename it into place
(moves.copy_noreplace), so a live target is never half-written.

Which copy a profile loads mirrors Hermes through resolve.py. Stdlib only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from commons.core.fleet import Profile
from commons.core.gitrepo import GitRepo
from commons.core.group import TAKEN_OVER_KINDS, fingerprint, is_movable, same_content
from commons.core.journal import operation, read_commons_config
from commons.core.layout import Layout
from commons.core.proposal import RESERVED_TOP, SHARED, is_pinned_profile_copy
from commons.core.resolve import ScanCache, config_path, resolve
from commons.core.scan import SkillCopy, scan_profile, scan_root


class PromoteError(RuntimeError):
    """Refused before anything was changed."""


@dataclass
class PromoteResult:
    backup_id: str
    sha: str | None  # None when the shared root already held identical content
    target: Path
    others: list[str] = field(default_factory=list)  # profiles still loading their own copy
    kept: list[str] = field(default_factory=list)  # why the profile's own copy was left in place


@dataclass
class DemoteResult:
    backup_id: str
    sha: str | None
    path: Path


def _no_hash(_path: Path) -> str:
    return ""


def _profile(profiles: Sequence[Profile], name: str) -> Profile:
    for p in profiles:
        if p.name == name:
            return p
    raise PromoteError(f"no profile named {name!r}")


def _shared_copies(layout: Layout) -> list[SkillCopy]:
    root = Path(layout.shared_root)
    if not root.is_dir():
        return []
    return [c for c in scan_root(root, SHARED, _no_hash, _no_hash) if c.parent_rel is None]


def _overlaps(a: str, b: str) -> bool:
    pa, pb = PurePosixPath(a), PurePosixPath(b)
    return pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa)


def _taken_entry(name: str, profile: str, kind: str, origin_hash: str | None) -> dict:
    return {"name": name, "profile": profile, "kind": kind, "origin_hash": origin_hash}


def promote(
    name: str,
    from_profile: str,
    layout: Layout,
    profiles: Sequence[Profile],
    md5_dir_hash: Callable[[Path], str] = _no_hash,
    hub_hash: Callable[[Path], str] = _no_hash,
    now: datetime | None = None,
) -> PromoteResult:
    """Copy *from_profile*'s copy of *name* into the shared root, commit, retire the profile copy.

    Refuses when the profile has no own (movable, top-level) copy of the
    name or several different ones, when the shared root holds a different
    copy of the name (merge instead), or when the target folder overlaps
    another shared skill. The other profiles that still load their own copy
    are returned (check reports them as shadowed). Every check runs with
    the repo lock held, the same hold the change runs under.
    """
    with GitRepo(layout.shared_root).locked():
        return _promote(name, from_profile, layout, list(profiles), md5_dir_hash, hub_hash, now)


def _promote(
    name: str,
    from_profile: str,
    layout: Layout,
    profiles: list[Profile],
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
    now: datetime | None,
) -> PromoteResult:
    source_profile = _profile(profiles, from_profile)
    mine = [
        c
        for c in scan_profile(source_profile, md5_dir_hash, hub_hash)
        if c.name == name and is_movable(c) and c.parent_rel is None
    ]
    if not mine:
        raise PromoteError(f"{from_profile} has no own copy of {name} to promote")
    if len(mine) > 1 and len({fingerprint(c.path) for c in mine}) > 1:
        where = ", ".join(c.rel for c in mine)
        raise PromoteError(f"{from_profile} has different copies of {name} ({where}); merge them first")
    copy = mine[0]
    before = fingerprint(copy.path)
    shared = _shared_copies(layout)
    existing = next((c for c in shared if c.name == name), None)
    if existing is not None and not same_content(existing.path, copy.path):
        raise PromoteError(
            f"the shared root already has a different {name} at {existing.rel}; "
            f"merge them (commons merge) instead of promoting"
        )
    rel = existing.rel if existing is not None else copy.rel
    if existing is None:
        if PurePosixPath(rel).parts[0] in RESERVED_TOP:
            raise PromoteError(f"{name}: {rel} is not a place a skill can land in the shared root")
        clash = sorted({c.name for c in shared if _overlaps(c.rel, rel)})
        if clash or (Path(layout.shared_root) / rel).exists():
            raise PromoteError(f"{name}: {rel} overlaps {', '.join(clash) or 'a folder'} in the shared root")
    target = Path(layout.shared_root) / rel
    pinned = [
        (p, c, fingerprint(c.path))
        for p in profiles
        for c in scan_profile(p, md5_dir_hash, hub_hash)
        if c.name == name and is_pinned_profile_copy(c)
    ]
    touched = [source_profile, *dict.fromkeys(p for p, _c, _f in pinned if p.name != from_profile)]
    with operation(layout, touched, f"promote-{name}", "promote", now) as op:
        sha = None
        if existing is None:

            op.copy_in("shared_copy", name, copy.path, target)
            if not same_content(copy.path, target):
                raise PromoteError(f"{name}: the copy at {target} does not match {copy.path}")
            sha = op.commit([target], f"promote {name} from {from_profile}", kind="promote")
        entries = []
        if copy.kind in TAKEN_OVER_KINDS:
            entries.append(_taken_entry(name, from_profile, copy.kind, copy.origin_hash))
        kept = op.shared_use(name, from_profile, [c.rel for c in mine])
        for c in mine if kept is None else ():
            why = op.retire_profile_copy(name, from_profile, c.rel, before)
            if why is not None:
                raise PromoteError(f"{name}: {why}; the shared copy is committed, the local one stays")
        pinned_kept = []
        for p, c, seen in pinned:
            if not op.reads_shared(p.name):
                continue
            why = op.retire_profile_copy(name, p.name, c.rel, seen)
            if why is None:
                entries.append(_taken_entry(name, p.name, c.kind, c.origin_hash))
            else:
                pinned_kept.append(why)
        config = read_commons_config(layout)
        taken = list(config.get("taken_over", []))
        new = [e for e in entries if e not in taken]
        if new:
            config["taken_over"] = [*taken, *new]
            op.write_config(config, f"commons: record take-over of {name}")
        problems = ([kept] if kept is not None else []) + pinned_kept
        if problems:
            op.journal.data["problems"] = problems
    others = []
    cache = ScanCache()
    shared_root = config_path(Path("/"), Path(layout.shared_root))
    for p in profiles:
        if p.name == from_profile:
            continue
        r = resolve(p, md5_dir_hash, hub_hash, cache=cache).get(name)
        if r is not None and r.winner_path is not None and not r.winner_path.is_relative_to(shared_root):
            others.append(p.name)
    return PromoteResult(op.backup_id, sha, target, others, problems)


def demote(
    name: str,
    to_profile: str,
    layout: Layout,
    profiles: Sequence[Profile],
    now: datetime | None = None,
) -> DemoteResult:
    """Copy the shared *name* into *to_profile*'s skills dir (same rel) and record it as intentional.

    The local copy then wins in that profile (Hermes local tier beats
    create_dir), and the {name, profile} entry in .commons/config.json
    intentional_overrides keeps check from reporting it as shadowed.
    Refuses when the shared root has no such skill, or the profile already
    has a copy of the name or something at the target folder. The checks
    run with the repo lock held.
    """
    with GitRepo(layout.shared_root).locked():
        return _demote(name, to_profile, layout, profiles, now)


def _demote(
    name: str, to_profile: str, layout: Layout, profiles: Sequence[Profile], now: datetime | None
) -> DemoteResult:
    profile = _profile(profiles, to_profile)
    source = next((c for c in _shared_copies(layout) if c.name == name), None)
    if source is None:
        raise PromoteError(f"the shared root has no skill named {name}")
    dest = profile.skills_dir / source.rel
    own = [c for c in scan_profile(profile, _no_hash, _no_hash) if c.name == name]
    if own or dest.exists() or dest.is_symlink():
        where = ", ".join(c.rel for c in own) or source.rel
        raise PromoteError(f"{to_profile} already has {name} at {where}; nothing demoted")
    with operation(layout, [profile], f"demote-{name}", "demote", now) as op:
        op.copy_in("profile_copy", name, source.path, dest, profile=to_profile, rel=source.rel)
        config = read_commons_config(layout)
        overrides = [
            o for o in config.get("intentional_overrides", []) if o != {"name": name, "profile": to_profile}
        ]
        config["intentional_overrides"] = [*overrides, {"name": name, "profile": to_profile}]
        sha = op.write_config(config, f"commons: demote {name} to {to_profile}")
    return DemoteResult(op.backup_id, sha, dest)
