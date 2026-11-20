"""Drift check for a fleet that shares one skill library, and the one fix that is always safe.

run_check() reads, never writes. Findings, by code:

  plugin-off           warn  'commons' is not in the profile's plugins.enabled
  create-dir-missing   warn  skills.create_dir is unset (or not an existing folder)
  create-dir-elsewhere warn  skills.create_dir points at another folder
  shadowed             warn  a shared skill loses to a local or project copy that
                             is not in config.json intentional_overrides
  ambiguous            warn  Hermes refuses the name: two different copies tie
  uncommitted          warn  git status of the shared root is not clean
                             (commons state such as .commons/lock is excluded)
  stale-path           warn  a cron prompt or shared-root markdown names a skill
                             path some commons journal retired
  bad-frontmatter      warn  a shared SKILL.md has no frontmatter block, or YAML
                             that does not parse to a mapping
  name-mismatch        info  a shared SKILL.md declares a name other than its folder
  missing-ref          warn  a shared SKILL.md names references/, scripts/,
                             templates/ or assets/ files that are not there
  oversize             warn  a shared SKILL.md is over 40,000 characters
  upstream-newer       warn  a taken-over bundled or hub skill whose upstream hash
                             is no longer the origin hash recorded at take-over
  unfinished-apply     warn  a journal in the state dir is neither complete nor
                             restored (setup or any Phase 4 operation)

Resolution mirrors Hermes through resolve.py (see its docstring). The
per-skill content codes (bad-frontmatter, name-mismatch, missing-ref,
oversize) look at the shared root only: profile-local skills are the
profile's business. bad-frontmatter mirrors the parse step of Hermes
tools/skill_manager_tool.py _validate_frontmatter (block split as
agent/skill_utils.py parse_frontmatter, then yaml.safe_load, mapping
required). The 40,000 character line is commons' own budget, well under
Hermes' MAX_SKILL_CONTENT_CHARS (100,000) so a warning comes before Hermes
refuses an edit.

upstream-newer: the hub side reads <profile>/skills/.hub/lock.json
content_hash (scan.read_hub_lock). The bundled side needs the hash of the
bundled skill Hermes ships now, which only the Hermes adapter can compute;
it passes bundled_index, a dict or callable name -> hash. Without one the
bundled checks are skipped with one info finding.

fix_safe() only commits uncommitted shared-root changes, through
journal.operation (lock, backup, journal). Stdlib + PyYAML only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from commons.core.apply import intents, unfinished_setups
from commons.core.frontmatter import frontmatter_block
from commons.core.gitrepo import GitError, GitRepo
from commons.core.journal import operation, read_commons_config
from commons.core.layout import Layout
from commons.core.merge import referenced_files
from commons.core.paths import CRON_DIR, JOBS_FILE, CronStoreError, find_stale_paths, load_jobs_file
from commons.core.proposal import PLUGIN, SHARED
from commons.core.resolve import TIER_CREATE_DIR, TIER_NAMES, ScanCache, config_path, resolve
from commons.core.scan import SKILL_FILE, read_hub_lock, scan_root
from commons.core.verify import _override_keys

Severity = Literal["warn", "info"]
OVERSIZE_CHARS = 40_000
FIX_MESSAGE = "commons: commit edits made outside Hermes"
BundledIndex = Mapping[str, str] | Callable[[str], str | None]


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    profile: str | None
    skill: str | None
    message: str


def _no_hash(_path: Path) -> str:
    return ""


def _warn(code: str, profile: str | None, skill: str | None, message: str) -> Finding:
    return Finding("warn", code, profile, skill, message)


def _info(code: str, profile: str | None, skill: str | None, message: str) -> Finding:
    return Finding("info", code, profile, skill, message)


# ------------------------------------------------------------------ config


def _config_findings(profiles, shared: Path) -> list[Finding]:
    out = []
    for p in profiles:
        if PLUGIN not in p.plugins_enabled():
            out.append(_warn("plugin-off", p.name, None, f"{PLUGIN} is not in plugins.enabled"))
        create = p.create_dir()
        if create is None:
            out.append(_warn("create-dir-missing", p.name, None, "skills.create_dir is not set"))
            continue
        where = config_path(p.home, create)
        if where != shared:
            out.append(
                _warn("create-dir-elsewhere", p.name, None, f"skills.create_dir is {where}, not {shared}")
            )
        elif not where.is_dir():
            out.append(_warn("create-dir-missing", p.name, None, f"skills.create_dir {where} does not exist"))
    return out


# ------------------------------------------------------------------ resolution


def _resolution_findings(profiles, shared: Path, overrides, md5, hub, project_dirs) -> list[Finding]:
    allowed = _override_keys(overrides)
    cache = ScanCache()
    out = []
    for p in profiles:
        for name, r in sorted(resolve(p, md5, hub, project_dirs, cache).items()):
            if r.is_ambiguous:
                where = ", ".join(sorted(str(c.path) for c in r.ambiguous))
                tier = TIER_NAMES.get(r.winner_tier, str(r.winner_tier))
                out.append(_warn("ambiguous", p.name, name, f"two different {tier} copies tie: {where}"))
                continue
            hidden = [c for t, c in r.shadowed if t == TIER_CREATE_DIR and c.path.is_relative_to(shared)]
            if r.winner is not None and hidden and (p.name, name) not in allowed:
                tier = TIER_NAMES.get(r.winner_tier, str(r.winner_tier))
                out.append(
                    _warn(
                        "shadowed",
                        p.name,
                        name,
                        f"the shared copy is shadowed by a {tier} copy at {r.winner.path} "
                        f"(merge it, promote it, or demote to keep it on purpose)",
                    )
                )
    return out


# ------------------------------------------------------------------ git


def _git_findings(layout: Layout) -> list[Finding]:
    repo = GitRepo(layout.shared_root)
    if not repo.is_repo():
        return [_warn("uncommitted", None, None, f"{layout.shared_root} is not a git repository")]
    try:
        lines = repo.status_porcelain()
    except GitError as exc:
        return [_warn("uncommitted", None, None, f"git status failed: {exc}")]
    if not lines:
        return []
    paths = ", ".join(line[3:] for line in lines[:5]) + (" ..." if len(lines) > 5 else "")
    return [_warn("uncommitted", None, None, f"{len(lines)} uncommitted changes in the shared root: {paths}")]


# ------------------------------------------------------------------ stale paths


def _journals(layout: Layout) -> Iterable[dict[str, Any]]:
    root = Path(layout.backups_dir)
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        try:
            data = json.loads((d / "journal.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield data


def _retired_paths(layout: Layout, shared_by_name: Mapping[str, Path]) -> dict[Path, Path]:
    """Old profile skill folder -> its shared copy now, for every retire any journal did that still holds."""
    moved: dict[Path, Path] = {}
    for data in _journals(layout):
        for i in intents(data, "retire"):
            args = i["args"]
            src = Path(args["src"])
            new = shared_by_name.get(args.get("name", ""))
            if new is None or src.exists() or src.is_symlink():
                continue
            moved[src] = new
    return moved


def _stale_findings(profiles, layout: Layout, shared_by_name, home: Path | None) -> list[Finding]:
    moved = _retired_paths(layout, shared_by_name)
    if not moved:
        return []
    cron: dict[str, list[dict]] = {}
    for p in profiles:
        path = p.home / CRON_DIR / JOBS_FILE
        if path.is_file():
            try:
                cron[p.name] = load_jobs_file(path)[1]
            except (CronStoreError, OSError, ValueError):
                continue
    out = []
    for f in find_stale_paths(profiles, layout.shared_root, moved, cron, home=home):
        fix = "" if f.new_exists else f" (and {f.new} does not exist)"
        out.append(_warn("stale-path", f.profile, None, f"{f.location}: {f.old} is now {f.new}{fix}"))
    return out


# ------------------------------------------------------------------ shared skills


def _skill_findings(shared_root: Path, copies) -> list[Finding]:
    out = []
    for c in copies:
        md = c.path / SKILL_FILE
        folder = c.path.name
        try:
            text = md.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            out.append(_warn("bad-frontmatter", None, c.name, f"{c.rel}/{SKILL_FILE} cannot be read ({exc})"))
            continue
        block = frontmatter_block(text)
        if block is None:
            out.append(
                _warn("bad-frontmatter", None, c.name, f"{c.rel}/{SKILL_FILE} has no frontmatter block")
            )
        else:
            try:
                parsed = yaml.safe_load(block[0])
            except yaml.YAMLError as exc:
                problem = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                out.append(
                    _warn("bad-frontmatter", None, c.name, f"{c.rel}/{SKILL_FILE} frontmatter: {problem}")
                )
            else:
                if not isinstance(parsed, dict):
                    out.append(
                        _warn(
                            "bad-frontmatter",
                            None,
                            c.name,
                            f"{c.rel}/{SKILL_FILE} frontmatter is not a mapping",
                        )
                    )
                elif c.name != folder:
                    out.append(
                        _info(
                            "name-mismatch",
                            None,
                            c.name,
                            f"{c.rel} declares name {c.name!r}, folder is {folder!r}",
                        )
                    )
        missing = [r for r in referenced_files(text) if ".." in Path(r).parts or not (c.path / r).exists()]
        if missing:
            out.append(
                _warn("missing-ref", None, c.name, f"{c.rel}/{SKILL_FILE} names missing {', '.join(missing)}")
            )
        if len(text) > OVERSIZE_CHARS:
            out.append(
                _warn(
                    "oversize",
                    None,
                    c.name,
                    f"{c.rel}/{SKILL_FILE} is {len(text):,} characters (budget {OVERSIZE_CHARS:,}); "
                    f"move detail into references/",
                )
            )
    return out


# ------------------------------------------------------------------ upstream


def _bundled_hash(index: BundledIndex, name: str) -> str | None:
    if callable(index):
        return index(name)
    return index.get(name)


def _upstream_findings(profiles, config: dict[str, Any], bundled_index: BundledIndex | None) -> list[Finding]:
    taken = [t for t in config.get("taken_over", []) if isinstance(t, dict) and t.get("name")]
    homes = {p.name: p for p in profiles}
    out = []
    bundled = [t for t in taken if str(t.get("kind", "")).startswith("bundled")]
    if bundled and bundled_index is None:
        names = ", ".join(sorted({t["name"] for t in bundled}))
        out.append(_info("upstream-newer", None, None, f"no bundled index given; not checked: {names}"))
    elif bundled_index is not None:
        for t in bundled:
            now = _bundled_hash(bundled_index, t["name"])
            if now is not None and now != t.get("origin_hash"):
                out.append(
                    _warn(
                        "upstream-newer",
                        t.get("profile"),
                        t["name"],
                        "Hermes ships a newer bundled version than the one this shared copy was taken over from",
                    )
                )
    for t in taken:
        if not str(t.get("kind", "")).startswith("hub"):
            continue
        p = homes.get(t.get("profile"))
        if p is None:
            continue
        entry = read_hub_lock(p.skills_dir).get(t["name"])
        now = entry.get("content_hash") if isinstance(entry, dict) else None
        if isinstance(now, str) and now != t.get("origin_hash"):
            out.append(
                _warn(
                    "upstream-newer",
                    p.name,
                    t["name"],
                    "the hub lock records a newer install than the one this shared copy was taken over from",
                )
            )
    return out


# ------------------------------------------------------------------ entry points


def run_check(
    profiles: Sequence,
    layout: Layout,
    md5_dir_hash: Callable[[Path], str] = _no_hash,
    hub_hash: Callable[[Path], str] = _no_hash,
    bundled_index: BundledIndex | None = None,
    home: Path | None = None,
    project_dirs: Sequence[Path] = (),
) -> list[Finding]:
    """Every finding for *profiles* sharing layout.shared_root (see module docstring), warnings first.

    *home* anchors ~ paths in cron prompts (default the user's home);
    *project_dirs* are trusted project skills dirs, as Hermes tier 0.
    """
    profiles = list(profiles)
    shared = config_path(Path("/"), Path(layout.shared_root))
    config = read_commons_config(layout)
    found: list[Finding] = []
    found += _config_findings(profiles, shared)
    found += _resolution_findings(
        profiles, shared, config.get("intentional_overrides", []), md5_dir_hash, hub_hash, project_dirs
    )
    copies = []
    if Path(layout.shared_root).is_dir():
        copies = [
            c for c in scan_root(Path(layout.shared_root), SHARED, _no_hash, _no_hash) if c.parent_rel is None
        ]
        found += _git_findings(layout)
    shared_by_name = {c.name: c.path for c in copies}
    found += _stale_findings(profiles, layout, shared_by_name, home)
    found += _skill_findings(Path(layout.shared_root), copies)
    found += _upstream_findings(profiles, config, bundled_index)
    for bid in unfinished_setups(layout):
        found.append(
            _warn(
                "unfinished-apply",
                None,
                None,
                f"operation {bid} did not finish; restore it (restore {bid}) or resume it",
            )
        )
    return sorted(found, key=lambda f: f.severity != "warn")


def _finish_hint(layout: Layout, bid: str) -> str:
    return f"restore it (restore {bid}) or resume it"


def fix_safe(findings: Iterable[Finding], layout: Layout) -> str | Finding | None:
    """Commit uncommitted shared-root changes, if a finding says there are any; return the sha.

    Nothing else is ever fixed here: every other finding needs a decision.
    While any journal is unfinished nothing is committed and a finding
    saying why (restore or resume first) is returned instead.
    """
    if not any(f.code == "uncommitted" for f in findings):
        return None
    left = unfinished_setups(layout)
    if left:
        return Finding(
            "warn",
            "unfinished-apply",
            None,
            None,
            f"not fixed: operation {left[-1]} did not finish; {_finish_hint(layout, left[-1])} first, "
            f"so its half-done changes are not committed as edits",
        )
    repo = GitRepo(layout.shared_root)
    if not repo.is_repo() or not repo.status_porcelain():
        return None
    with operation(layout, [], "fix-safe", "fix-safe") as op:
        return op.commit_all(FIX_MESSAGE)


def _line(f: Finding) -> str:
    where = " ".join(x for x in (f.profile, f.skill) if x)
    return f"{f.severity:4}  {f.code}  {where + ': ' if where else ''}{f.message}"


def render_findings(findings: Sequence[Finding], quiet: bool = False) -> str:
    """Plain text, one line per finding; quiet shows warnings only and nothing at all without any."""
    warns = [f for f in findings if f.severity == "warn"]
    infos = [f for f in findings if f.severity != "warn"]
    if quiet:
        return "\n".join(_line(f) for f in warns) + ("\n" if warns else "")
    if not findings:
        return "commons check: no problems\n"
    lines = [_line(f) for f in [*warns, *infos]]
    w = f"{len(warns)} warning" + ("" if len(warns) == 1 else "s")
    lines.append(f"commons check: {w}, {len(infos)} info")
    return "\n".join(lines) + "\n"
