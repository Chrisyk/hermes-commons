"""Read-only fleet status: where each profile's skills resolve from and what is shadowed.

Stdlib only; Hermes is never imported. The index size estimate mirrors the
Hermes v0.23.1 system-prompt skill index (see index_row_chars).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from commons.core.fleet import Profile
from commons.core.resolve import (
    TIER_CREATE_DIR,
    TIER_NAMES,
    TIER_PROJECT,
    Resolution,
    config_path,
    is_dir,
    resolve,
)
from commons.core.scan import SKILL_FILE, read_frontmatter_value

# Mirror of Hermes agent/skill_utils.py SKILL_PROMPT_DESC_LIMIT, _LAST_WHITESPACE_RE, _CLIP_TRAIL_RE.
SKILL_PROMPT_DESC_LIMIT = 60
_LAST_WHITESPACE_RE = re.compile(r"\s\S*$")
_CLIP_TRAIL_RE = re.compile(r"[\s.,;:\-\u2013\u2014(/]+$")

TIER_ORDER = tuple(TIER_NAMES[t] for t in sorted(TIER_NAMES))


def clip_description(desc: str) -> str:
    """Mirror of Hermes agent/skill_utils.py clip_skill_description.

    Descriptions over 60 chars are cut at the last whole word inside the first
    58 chars, trailing punctuation and whitespace stripped, then '...' added;
    a single over-long word is cut to 57 chars plus '...'.
    """
    if len(desc) <= SKILL_PROMPT_DESC_LIMIT:
        return desc
    head = desc[: SKILL_PROMPT_DESC_LIMIT - 2]
    boundary = _LAST_WHITESPACE_RE.search(head)
    if not boundary or boundary.start() == 0:
        return desc[: SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return _CLIP_TRAIL_RE.sub("", head[: boundary.start()]) + "..."


def read_description(skill_md: Path) -> str:
    """Frontmatter description, line-based like read_skill_name, unclipped; '' if missing."""
    return read_frontmatter_value(Path(skill_md), "description")


def index_row_chars(name: str, description: str) -> int:
    """Chars one skill adds to the Hermes system-prompt index, newline included.

    Mirror of Hermes agent/prompt_builder.py _render_skills_index (row
    f"{indent}- {name}: {desc}", or f"{indent}- {name}" when desc is empty, rows
    joined by newlines). The indent is taken as the 4-space one used inside a
    category with more than one skill.
    """
    row = f"    - {name}: {description}" if description else f"    - {name}"
    return len(row) + 1


@dataclass
class ProfileStatus:
    profile: str
    counts: dict[str, int]
    shadowed_shared: list[str]
    ambiguous: list[str]
    index_chars: int
    resolutions: dict[str, Resolution] = field(default_factory=dict, repr=False, compare=False)


@dataclass
class StatusReport:
    profiles: list[ProfileStatus]
    shared_root: Path | None


def _index_chars(resolutions: Mapping[str, Resolution]) -> int:
    total = 0
    for name, res in resolutions.items():
        if res.winner is None:
            continue
        desc = clip_description(read_description(res.winner.path / SKILL_FILE))
        if res.winner_tier == TIER_PROJECT:
            # Mirror of Hermes _label_visible_entries: project rows are tagged.
            desc = f"[project] {desc}".strip()
        total += index_row_chars(name, desc)
    return total


def profile_status(name: str, resolutions: Mapping[str, Resolution]) -> ProfileStatus:
    counts = dict.fromkeys(TIER_ORDER, 0)
    for res in resolutions.values():
        if res.winner is not None and res.winner_tier is not None:
            counts[TIER_NAMES[res.winner_tier]] += 1
    shadowed = sorted(
        n
        for n, res in resolutions.items()
        if res.winner_tier is not None
        and res.winner_tier < TIER_CREATE_DIR
        and any(tier == TIER_CREATE_DIR for tier, _ in res.shadowed)
    )
    ambiguous = sorted(n for n, res in resolutions.items() if res.ambiguous)
    return ProfileStatus(name, counts, shadowed, ambiguous, _index_chars(resolutions), dict(resolutions))


def shared_root_of(profiles: Sequence[Profile]) -> Path | None:
    """The existing create_dir most profiles point at (ties: earliest profile), or None."""
    found = []
    for p in profiles:
        create = p.create_dir()
        if create is not None:
            path = config_path(p.home, create)
            if is_dir(path):
                found.append(path)
    if not found:
        return None
    counts = Counter(found)
    return max(found, key=lambda path: (counts[path], -found.index(path)))


def status(
    profiles: Sequence[Profile],
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
) -> StatusReport:
    """Resolve every profile (project tier excluded: it depends on the session cwd)."""
    rows = [profile_status(p.name, resolve(p, md5_dir_hash, hub_hash)) for p in profiles]
    return StatusReport(profiles=rows, shared_root=shared_root_of(profiles))


def _names(names: list[str]) -> str:
    return f"{len(names)} ({', '.join(names)})" if names else "0"


def _skill_lines(skill: str, res: Resolution | None) -> list[str]:
    if res is None:
        return [f"  {skill}: not found"]
    if res.winner is None:
        tier = TIER_NAMES[res.winner_tier] if res.winner_tier is not None else "?"
        out = [f"  {skill}: ambiguous in {tier}"]
        out += [f"    candidate {c.path}" for c in res.ambiguous]
    else:
        out = [f"  {skill}: {TIER_NAMES[res.winner_tier]} {res.winner.path}"]
    out += [f"    shadows {TIER_NAMES[tier]} {c.path}" for tier, c in res.shadowed]
    return out


def render_status(
    report: StatusReport,
    profile: str | None = None,
    skill: str | None = None,
    resolutions: Mapping[str, Mapping[str, Resolution]] | None = None,
) -> str:
    """Plain-text report, one section per profile.

    *resolutions* (profile name -> name -> Resolution) overrides the ones the
    report was built from, for the *skill* lines only.
    """
    root = str(report.shared_root) if report.shared_root is not None else "none"
    lines = [f"shared root: {root}"]
    for ps in report.profiles:
        if profile is not None and ps.profile != profile:
            continue
        counts = ", ".join(f"{tier}: {ps.counts.get(tier, 0)}" for tier in TIER_ORDER)
        lines += [
            f"profile {ps.profile}",
            f"  skills: {sum(ps.counts.values())} ({counts})",
            f"  shadowed: {_names(ps.shadowed_shared)}",
            f"  ambiguous: {_names(ps.ambiguous)}",
            f"  index: ~{ps.index_chars} chars",
        ]
        if skill is not None:
            source = resolutions.get(ps.profile, {}) if resolutions is not None else ps.resolutions
            lines += _skill_lines(skill, source.get(skill))
    return "\n".join(lines) + "\n"
