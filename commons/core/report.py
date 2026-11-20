"""Read-only fleet status: where each profile's skills resolve from and what is shadowed.

Hermes is never imported. 'resolvable' counts every declared name that
skill_view would load (see resolve.py). 'indexed' and index_chars mirror the
Hermes v0.23.1 system-prompt skill index, agent/prompt_builder.py
build_skills_system_prompt on its cold path (no disk snapshot), with the
host and config filters it applies:

- frontmatter platforms: against sys.platform (agent/skill_utils.py
  skill_matches_platform_list);
- frontmatter environments: (skill_matches_environment), with the kanban,
  docker and s6 detectors decided as Hermes decides them (see
  environment_active); unknown tags count as active;
- skills.disabled plus skills.platform_disabled[platform] from the profile
  config, minus ESSENTIAL_SKILLS (get_disabled_skill_names), matched against
  the folder name, the declared name and the load name as Hermes does.

Session-dependent filters are NOT applied: requires_tools/requires_toolsets,
fallback_for_tools/fallback_for_toolsets, session_platforms and
requires_apps all depend on the running session or host app declarations.
Plugin-registered skills and the coding-posture [names only] demotion are
also left out. On a warm start Hermes reuses a disk snapshot that skips the
environments: check for profile-local skills; the cold path mirrored here is
what a fresh build lists.

Ambiguous names still take part: Hermes lists a same-tier duplicate under its
relative path (resolve_skill_catalog, _label_visible_entries).
"""

from __future__ import annotations

import ast
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from commons.core.fleet import Profile
from commons.core.resolve import (
    TIER_CREATE_DIR,
    TIER_LOCAL,
    TIER_NAMES,
    TIER_PROJECT,
    Resolution,
    ScanCache,
    SkillIndex,
    build_index,
    candidate_rank,
    config_path,
    is_dir,
    provably_same,
    resolve_index,
)

# Mirror of Hermes agent/skill_utils.py SKILL_PROMPT_DESC_LIMIT, _LAST_WHITESPACE_RE, _CLIP_TRAIL_RE.
SKILL_PROMPT_DESC_LIMIT = 60
_LAST_WHITESPACE_RE = re.compile(r"\s\S*$")
_CLIP_TRAIL_RE = re.compile(r"[\s.,;:\-\u2013\u2014(/]+$")
# Mirror of Hermes agent/skill_utils.py PLATFORM_MAP and ESSENTIAL_SKILLS.
PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}
ESSENTIAL_SKILLS = frozenset({"hermes-agent"})
# Mirror of Hermes hermes_cli/config_defaults.py DEFAULT_CONFIG["toolsets"].
_DEFAULT_TOOLSETS = ["hermes-cli"]
_DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"

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


def description_of(frontmatter: Mapping[str, Any]) -> str:
    """Mirror of Hermes extract_skill_description (_normalize_skill_description then clip)."""
    raw = frontmatter.get("description", "")
    return clip_description(str(raw).strip().strip("'\"") if raw else "")


def index_row_chars(name: str, description: str, indent: str = "    ") -> int:
    """Chars one index row adds, newline included (row format of _render_skills_index)."""
    row = f"{indent}- {name}: {description}" if description else f"{indent}- {name}"
    return len(row) + 1


# ---------------------------------------------------------------- host filters


def platform_matches(platforms: Any, current: str | None = None) -> bool:
    """Mirror of Hermes agent/skill_utils.py skill_matches_platform_list."""
    if not platforms:
        return True
    current = sys.platform if current is None else current
    for platform in platforms if isinstance(platforms, list) else [platforms]:
        normalized = str(platform).lower().strip()
        if current.startswith(PLATFORM_MAP.get(normalized, normalized)):
            return True
    return False


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _is_container(environ: Mapping[str, str]) -> bool:
    """Mirror of Hermes hermes_platform/host/runtime.py _detect_container."""
    markers = ("docker", "podman", "/lxc/", "kubepods", "containerd", "crio")
    if (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or environ.get("KUBERNETES_SERVICE_HOST")
        or any(m in _read("/proc/1/cgroup") for m in markers)
    ):
        return True
    root_lines = [
        line
        for line in _read("/proc/self/mountinfo").splitlines()
        if len(f := line.split()) >= 5 and f[4] == "/"
    ]
    return any(m in line for line in root_lines for m in ("kubepods", "containerd", "crio"))


def _has_kanban_toolset(config: Mapping[str, Any]) -> bool:
    """Mirror of Hermes tools/kanban_tools.py _profile_has_kanban_toolset outside schema assembly.

    'kanban' in the toolsets value (default ['hermes-cli']), or named in any
    platform_toolsets list. Hermes expands each list through
    _get_platform_tools; no built-in composite enables kanban, so naming it is
    the only way in.
    """
    try:
        if "kanban" in (config.get("toolsets", _DEFAULT_TOOLSETS) or []):
            return True
        platforms = config.get("platform_toolsets") or {}
        return any("kanban" in names for names in platforms.values() if isinstance(names, list))
    except (TypeError, AttributeError):
        return False


def environment_active(tag: str, config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> bool:
    """Mirror of Hermes agent/skill_utils.py _detect_environment (unknown tags are active)."""
    environ = os.environ if environ is None else environ
    if tag == "kanban":
        worker = environ.get("HERMES_KANBAN_TASK") or environ.get("HERMES_KANBAN_BOARD")
        if worker and not environ.get(_DELEGATED_CHILD_ENV_MARKER):
            return True
        return _has_kanban_toolset(config)
    if tag == "docker":
        return _is_container(environ)
    if tag == "s6":
        return os.path.isdir("/run/s6") or os.path.isdir("/package/admin/s6-overlay")
    return True


def environments_match(frontmatter: Mapping[str, Any], config: Mapping[str, Any], environ=None) -> bool:
    """Mirror of Hermes agent/skill_utils.py skill_matches_environment."""
    environments = frontmatter.get("environments")
    if not environments:
        return True
    tags = [
        str(e).lower().strip() for e in (environments if isinstance(environments, list) else [environments])
    ]
    return any(environment_active(tag, config, environ) for tag in tags if tag)


def _string_set(value: Any) -> set[str]:
    """Mirror of Hermes agent/skill_utils.py _normalize_string_set over parse_config_string_list."""
    if isinstance(value, str):
        items = [value]
        if value.strip().startswith("["):
            try:
                parsed = ast.literal_eval(value.strip())
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, list):
                items = [str(item) for item in parsed]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item) for item in value]
    else:
        items = []
    return {n.strip() for n in items if n.strip()}


def session_platform(environ: Mapping[str, str] | None = None) -> str | None:
    """Mirror of Hermes prompt_builder _current_session_platform_hint (env only)."""
    environ = os.environ if environ is None else environ
    return environ.get("HERMES_PLATFORM") or environ.get("HERMES_SESSION_PLATFORM") or None


def disabled_names(config: Mapping[str, Any], platform: str | None = None) -> set[str]:
    """Mirror of Hermes agent/skill_utils.py get_disabled_skill_names for an explicit platform."""
    skills = config.get("skills") if isinstance(config, Mapping) else None
    if not isinstance(skills, Mapping):
        return set()
    disabled = _string_set(skills.get("disabled"))
    by_platform = skills.get("platform_disabled") or {}
    if platform and isinstance(by_platform, Mapping) and by_platform.get(platform) is not None:
        disabled |= _string_set(by_platform.get(platform))
    return disabled - ESSENTIAL_SKILLS


# ---------------------------------------------------------------- the index


@dataclass
class _Entry:
    tier: int
    root: Path
    file: Path
    folder: str
    name: str
    rel: str  # skill dir relative to its scanning root
    category: str
    description: str
    visible: bool
    status: str = ""
    load_name: str | None = None


def _category(parts: tuple[str, ...]) -> str:
    """Mirror of Hermes prompt_builder _build_snapshot_entry category (parts of SKILL.md below root)."""
    if len(parts) < 2:
        return "general"
    return "/".join(parts[:-2]) if len(parts) > 2 else parts[0]


def _catalog_entries(
    index: SkillIndex, config: Mapping[str, Any], disabled: set[str], environ
) -> list[_Entry]:
    """Every SKILL.md of every root as prompt_builder _parse_skill_file and _build_snapshot_entry see it."""
    out = []
    for root in index.roots:
        if root.tier != TIER_LOCAL and not root.path.exists():
            continue
        for f in index.found[root.path]:
            fm = index.cache.read(f.file, strict=True)
            ok = platform_matches(fm.get("platforms")) and environments_match(fm, config, environ)
            parts = f.file.relative_to(root.path).parts
            name = str(fm.get("name", f.folder)) or f.folder
            rel = f.file.parent.relative_to(root.path).as_posix()
            visible = ok and f.folder not in disabled
            out.append(
                _Entry(
                    root.tier,
                    root.path,
                    f.file,
                    f.folder,
                    name,
                    rel,
                    _category(parts),
                    description_of(fm),
                    visible,
                )
            )
    return out


def _pick(cands: list[_Entry]) -> _Entry | None:
    """Mirror of Hermes agent/skill_utils.py pick_skill_candidate keyed by the scanning root."""
    top = min(e.tier for e in cands)
    contenders = [e for e in cands if e.tier == top]
    if (
        len(contenders) > 1
        and len({e.root for e in contenders}) == 1
        and provably_same([e.file for e in contenders])
    ):
        ranked = sorted(contenders, key=lambda e: candidate_rank(e.file, e.root))
        if candidate_rank(ranked[0].file, ranked[0].root) != candidate_rank(ranked[1].file, ranked[1].root):
            return ranked[0]
    return contenders[0] if len(contenders) == 1 else None


def _resolve_catalog(entries: list[_Entry]) -> None:
    """Mirror of Hermes agent/skill_utils.py resolve_skill_catalog (status and load_name)."""
    owners: dict[str, list[_Entry]] = {}
    for e in entries:
        for alias in {e.name, e.folder, e.rel}:
            owners.setdefault(alias, []).append(e)
    winner = {alias: _pick(cands) for alias, cands in owners.items()}
    for e in entries:
        higher = any(o.tier < e.tier for o in owners[e.name])
        won = winner[e.name]
        if higher or (won is not None and won is not e):
            e.status, e.load_name = "shadowed", None
        elif won is e:
            e.status, e.load_name = "unique", e.name
        else:
            e.status, e.load_name = "ambiguous", e.rel if winner[e.rel] is e else None


def index_lines(
    index: SkillIndex,
    config: Mapping[str, Any],
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """The <available_skills> lines Hermes renders for this profile (category headers included).

    Mirror of Hermes prompt_builder _build_skills_system_prompt_inner,
    _label_visible_entries and _render_skills_index (see module docstring
    for what is left out).
    """
    if not index.roots:
        return []
    disabled = disabled_names(config, platform if platform is not None else session_platform(environ))
    entries = _catalog_entries(index, config, disabled, environ)
    _resolve_catalog(entries)
    by_category: dict[str, list[tuple[str, str]]] = {}
    for e in entries:
        if not e.visible or e.status == "shadowed" or e.load_name is None:
            continue
        if e.name in disabled or e.load_name in disabled:
            continue
        desc = f"[project] {e.description}".strip() if e.tier == TIER_PROJECT else e.description
        by_category.setdefault(e.category or "general", []).append((e.load_name, desc))
    lines = []
    for category in sorted(by_category):
        rows: dict[str, str] = {}
        for name, desc in sorted(by_category[category], key=lambda x: x[0]):
            rows.setdefault(name, desc)
        indent = "  " if len(rows) == 1 else "    "
        if len(rows) > 1:
            lines.append(f"  {category}:")
        lines += [f"{indent}- {n}: {d}" if d else f"{indent}- {n}" for n, d in rows.items()]
    return lines


# ---------------------------------------------------------------- status


@dataclass
class ProfileStatus:
    profile: str
    resolvable: dict[str, int]
    shadowed_shared: list[str]
    ambiguous: list[str]
    indexed: int
    index_chars: int
    resolutions: dict[str, Resolution] = field(default_factory=dict, repr=False, compare=False)


@dataclass
class StatusReport:
    profiles: list[ProfileStatus]
    shared_root: Path | None


def profile_status(
    name: str, resolutions: Mapping[str, Resolution], lines: Sequence[str] = ()
) -> ProfileStatus:
    resolvable = dict.fromkeys(TIER_ORDER, 0)
    for res in resolutions.values():
        if res.winner_path is not None and res.winner_tier is not None:
            resolvable[TIER_NAMES[res.winner_tier]] += 1
    shadowed = sorted(
        n
        for n, res in resolutions.items()
        if res.winner_path is not None
        and res.winner_tier is not None
        and res.winner_tier < TIER_CREATE_DIR
        and any(tier == TIER_CREATE_DIR for tier, _ in res.shadowed)
    )
    ambiguous = sorted(n for n, res in resolutions.items() if res.is_ambiguous)
    indexed = sum(1 for line in lines if line.lstrip().startswith("- "))
    chars = sum(len(line) + 1 for line in lines)
    return ProfileStatus(name, resolvable, shadowed, ambiguous, indexed, chars, dict(resolutions))


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
    platform: str | None = None,
    cache: ScanCache | None = None,
) -> StatusReport:
    """Resolve every profile (project tier excluded: it depends on the session cwd).

    Each search root is scanned once for the whole fleet (*cache*), so a
    shared or external root used by every profile is walked one time.
    """
    cache = cache if cache is not None else ScanCache()
    rows = []
    for p in profiles:
        index = build_index(p, md5_dir_hash, hub_hash, cache=cache)
        rows.append(profile_status(p.name, resolve_index(index), index_lines(index, p.config, platform)))
    return StatusReport(profiles=rows, shared_root=shared_root_of(profiles))


def _names(names: list[str]) -> str:
    return f"{len(names)} ({', '.join(names)})" if names else "0"


def _skill_lines(skill: str, res: Resolution | None) -> list[str]:
    if res is None or (res.winner_path is None and res.winner_tier is None):
        return [f"  {skill}: not found"]
    if res.winner_path is None:
        tier = TIER_NAMES[res.winner_tier]
        out = [f"  {skill}: ambiguous in {tier}"]
        out += [f"    candidate {c.path}" for c in res.ambiguous]
        out += [f"    candidate {p}" for t, p in res.legacy if t == res.winner_tier]
    elif res.winner is None:
        out = [f"  {skill}: {TIER_NAMES[res.winner_tier]} legacy file {res.winner_path}"]
    else:
        out = [f"  {skill}: {TIER_NAMES[res.winner_tier]} {res.winner.path}"]
    out += [f"    shadows {TIER_NAMES[tier]} {c.path}" for tier, c in res.shadowed]
    if res.winner_path is not None:
        out += [f"    shadows {TIER_NAMES[t]} legacy file {p}" for t, p in res.legacy]
    else:
        out += [f"    shadows {TIER_NAMES[t]} legacy file {p}" for t, p in res.legacy if t != res.winner_tier]
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
        counts = ", ".join(f"{tier}: {ps.resolvable.get(tier, 0)}" for tier in TIER_ORDER)
        lines += [
            f"profile {ps.profile}",
            f"  resolvable: {sum(ps.resolvable.values())} ({counts})",
            f"  indexed: {ps.indexed} (~{ps.index_chars} chars)",
            f"  shadowed: {_names(ps.shadowed_shared)}",
            f"  ambiguous: {_names(ps.ambiguous)}",
        ]
        if skill is not None:
            source = resolutions.get(ps.profile, {}) if resolutions is not None else ps.resolutions
            lines += _skill_lines(skill, source.get(skill))
    return "\n".join(lines) + "\n"
