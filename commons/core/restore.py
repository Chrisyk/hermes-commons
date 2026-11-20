"""Undo a setup surgically, from its journal, without losing work done since.

plan_restore reads journal.json and lists what apply recorded: the profile
configs it changed, the copies it retired, the cron files it rewrote, plus
what happened in the shared root since setup (skills created or changed
after setup's last commit) and which restored copies would shadow a shared
copy again (a profile that still reads the shared root after restore).

restore() first takes a 'pre-restore' backup, then:

- config.yaml: if its sha256 still equals the one apply recorded after its
  change, the backed-up file is written back byte for byte (atomic, fsync,
  mode kept); otherwise only the keys commons changed are reverted
  (skills.create_dir back to its old value or unset, if it still points at
  the shared root; 'commons' out of plugins.enabled, if commons added it)
  and that is reported;
- copy_new_to: the named shared skills are copied into the chosen
  profiles (before the put-back below, so the user's choice wins);
- retired copies are moved back to where they were; if something is there
  now, the copy stays in the backup and that is reported;
- cron jobs.json: written back the same sha-or-report way, under Hermes'
  cron lock; if it changed since, it is left alone and reported.

The shared root is never modified. Only recorded steps are undone, so the
journal of a partial apply restores exactly what that apply did. Stdlib +
PyYAML only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from commons.core.apply import load_journal
from commons.core.backup import copy_tree, create_backup, file_sha256
from commons.core.configio import ConfigWriter
from commons.core.fleet import CONFIG_FILE, Profile, load_config
from commons.core.gitrepo import GitRepo
from commons.core.layout import Layout
from commons.core.moves import move_noreplace
from commons.core.paths import cron_lock, write_atomic
from commons.core.proposal import CREATE_DIR_KEY, PLUGIN, PLUGINS_KEY, SHARED
from commons.core.resolve import config_path
from commons.core.scan import SkillCopy, scan_root

REPORT_KEYS_ONLY = "config changed after setup; reverted commons keys only"


@dataclass
class ConfigPlan:
    profile: str
    home: Path
    backup_path: Path
    before_sha256: str | None
    after_sha256: str | None
    create_dir_set: bool
    create_dir_old: Any
    plugin_added: bool


@dataclass
class RetiredPlan:
    name: str
    profile: str
    rel: str
    path: Path
    retired_path: Path


@dataclass
class CronPlan:
    profile: str
    jobs_path: Path
    backup_path: Path
    before_sha256: str | None
    after_sha256: str | None


@dataclass
class SharedChange:
    name: str
    rel: str
    status: str  # 'new' or 'changed'


@dataclass
class RestorePlan:
    backup_id: str
    shared_root: Path
    state_dir: Path
    complete: bool
    since_sha: str | None
    configs: list[ConfigPlan] = field(default_factory=list)
    retired: list[RetiredPlan] = field(default_factory=list)
    cron: list[CronPlan] = field(default_factory=list)
    shared_changes: list[SharedChange] = field(default_factory=list)
    shadowed_after: list[tuple[str, str]] = field(default_factory=list)  # (profile, name)

    @property
    def layout(self) -> Layout:
        return Layout(Path(self.shared_root), Path(self.state_dir))


@dataclass
class RestoreResult:
    pre_restore_backup_id: str
    pre_restore_dir: Path
    done: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)


def _no_hash(_path: Path) -> str:
    return ""


def _shared_copies(shared_root: Path) -> list[SkillCopy]:
    if not Path(shared_root).is_dir():
        return []
    return [c for c in scan_root(Path(shared_root), SHARED, _no_hash, _no_hash) if c.parent_rel is None]


def _shared_changes(shared_root: Path, since: str | None) -> list[SharedChange]:
    repo = GitRepo(shared_root)
    if not Path(shared_root).is_dir() or not repo.is_repo():
        return []
    changed = repo.changed_since(since)
    out: list[SharedChange] = []
    for c in _shared_copies(shared_root):
        prefix = c.rel + "/"
        if any(p == c.rel or p.startswith(prefix) for p in changed):
            status = "changed" if since is not None and repo.exists_at(since, c.rel) else "new"
            out.append(SharedChange(c.name, c.rel, status))
    return out


def plan_restore(layout: Layout, backup_id: str) -> RestorePlan:
    """Read the journal of *backup_id* and list what restore would do (see module docstring)."""
    j = load_journal(layout, backup_id)
    backup_dir = layout.backup_dir(backup_id)
    steps = set(j["steps"])
    since = j["git"].get("final_sha") or j["git"].get("base_sha")
    plan = RestorePlan(
        backup_id=backup_id,
        shared_root=Path(j["shared_root"]),
        state_dir=Path(j["state_dir"]),
        complete=bool(j.get("complete")),
        since_sha=since,
    )
    for name, rec in j["profiles"].items():
        if f"config {name}" not in steps:
            continue
        plan.configs.append(
            ConfigPlan(
                profile=name,
                home=Path(rec["home"]),
                backup_path=backup_dir / name / CONFIG_FILE,
                before_sha256=rec["config_before_sha256"],
                after_sha256=rec["config_after_sha256"],
                create_dir_set=bool(rec["create_dir_set"]),
                create_dir_old=rec["create_dir_old"],
                plugin_added=bool(rec["plugin_added"]),
            )
        )
    plan.retired = [
        RetiredPlan(r["name"], r["profile"], r["rel"], Path(r["path"]), Path(r["retired_path"]))
        for r in j["retired"]
    ]
    plan.cron = [
        CronPlan(
            c["profile"], Path(c["jobs_path"]), Path(c["backup_path"]), c["before_sha256"], c["after_sha256"]
        )
        for c in j["cron"]
    ]
    plan.shared_changes = _shared_changes(plan.shared_root, since)
    plan.shadowed_after = _shadowed_after(plan, j)
    return plan


def _create_dir_after(plan: RestorePlan, profile: str, home: Path) -> Path | None:
    """Where this profile's skills.create_dir points once restore has run."""
    cfg = next((c for c in plan.configs if c.profile == profile), None)
    if cfg is not None and cfg.create_dir_set:
        value = cfg.create_dir_old
    else:
        skills = load_config(home / CONFIG_FILE).get("skills")
        value = skills.get("create_dir") if isinstance(skills, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return config_path(home, Path(value))


def _shadowed_after(plan: RestorePlan, journal: Mapping[str, Any]) -> list[tuple[str, str]]:
    shared = config_path(Path("/"), plan.shared_root)
    names = {c.name for c in _shared_copies(plan.shared_root)}
    out: list[tuple[str, str]] = []
    for r in plan.retired:
        home = Path(journal["profiles"][r.profile]["home"])
        if r.name in names and _create_dir_after(plan, r.profile, home) == shared:
            out.append((r.profile, r.name))
    return sorted(set(out))


# ------------------------------------------------------------------ restore


def _restore_config(
    cfg: ConfigPlan, profile: Profile, writer: ConfigWriter, shared: Path, result: RestoreResult
) -> None:
    path = profile.home / CONFIG_FILE
    current = file_sha256(path)
    if current == cfg.before_sha256:
        result.reports.append(f"{cfg.profile}: config.yaml already as before setup")
        return
    if current is not None and current == cfg.after_sha256:
        if cfg.before_sha256 is None:
            # There was no config before setup: move the one setup made aside, never delete it.
            dest = result.pre_restore_dir / "displaced" / cfg.profile / CONFIG_FILE
            move_noreplace(path, dest)
            result.done.append(f"{cfg.profile}: config.yaml moved aside to {dest} (none before setup)")
            return
        data = cfg.backup_path.read_bytes()
        if file_sha256(cfg.backup_path) != cfg.before_sha256:
            result.reports.append(f"{cfg.profile}: backed-up config.yaml does not match its hash; left alone")
            return
        write_atomic(path, data)
        result.done.append(f"{cfg.profile}: config.yaml restored")
        return
    reverted = []
    if cfg.create_dir_set:
        value = writer.get(profile, CREATE_DIR_KEY)
        if isinstance(value, str) and value.strip() and config_path(profile.home, Path(value)) == shared:
            if cfg.create_dir_old is None:
                writer.unset(profile, CREATE_DIR_KEY)
            else:
                writer.set(profile, CREATE_DIR_KEY, cfg.create_dir_old)
            reverted.append(CREATE_DIR_KEY)
        else:
            result.reports.append(
                f"{cfg.profile}: {CREATE_DIR_KEY} no longer points at the shared root; left alone"
            )
    if cfg.plugin_added:
        enabled = writer.get(profile, PLUGINS_KEY)
        if isinstance(enabled, list) and PLUGIN in enabled:
            writer.remove_from_list(profile, PLUGINS_KEY, PLUGIN)
            reverted.append(PLUGINS_KEY)
    keys = ", ".join(reverted) if reverted else "none"
    result.reports.append(f"{cfg.profile}: {REPORT_KEYS_ONLY} ({keys})")


def _restore_cron(c: CronPlan, result: RestoreResult) -> None:
    if not c.backup_path.is_file():
        result.reports.append(f"{c.profile}: no backup of {c.jobs_path}; left alone")
        return
    with cron_lock(c.jobs_path):
        current = file_sha256(c.jobs_path)
        if current == c.before_sha256:
            result.reports.append(f"{c.profile}: {c.jobs_path} already as before setup")
            return
        if current != c.after_sha256:
            result.reports.append(f"{c.profile}: {c.jobs_path} changed after setup; left alone")
            return
        write_atomic(c.jobs_path, c.backup_path.read_bytes())
    result.done.append(f"{c.profile}: {c.jobs_path} restored")


def _copy_new(
    plan: RestorePlan,
    by_name: Mapping[str, Profile],
    copy_new_to: Mapping[str, Sequence[str]],
    result: RestoreResult,
) -> None:
    shared = {c.name: c for c in _shared_copies(plan.shared_root)}
    for name in sorted(copy_new_to):
        for profile in copy_new_to[name]:
            c = shared[name]
            dest = by_name[profile].skills_dir / c.rel
            if dest.exists() or dest.is_symlink():
                result.reports.append(f"{profile}: {dest} already exists; {name} not copied")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(c.path, dest)
            result.done.append(f"{profile}: copied shared {name} to {dest}")


def restore(
    plan: RestorePlan,
    profiles: list[Profile],
    writer: ConfigWriter,
    copy_new_to: Mapping[str, Sequence[str]] | None = None,
    now: datetime | None = None,
) -> RestoreResult:
    """Carry out *plan* (see module docstring). Raises ValueError for a bad copy_new_to.

    *profiles* are the fleet as it is now; journal profiles missing from it
    are reported and skipped.
    """
    copy_new_to = dict(copy_new_to or {})
    by_name = {p.name: p for p in profiles}
    shared_names = {c.name for c in _shared_copies(plan.shared_root)}
    for name, targets in copy_new_to.items():
        if name not in shared_names:
            raise ValueError(f"copy_new_to: no skill named {name!r} in the shared root")
        unknown = [t for t in targets if t not in by_name]
        if unknown:
            raise ValueError(f"copy_new_to: unknown profiles {unknown} for {name!r}")
    layout = plan.layout
    bid = create_backup(layout, profiles, "pre-restore", now=now)
    result = RestoreResult(pre_restore_backup_id=bid, pre_restore_dir=layout.backup_dir(bid))
    shared = config_path(Path("/"), plan.shared_root)

    for cfg in plan.configs:
        profile = by_name.get(cfg.profile)
        if profile is None:
            result.reports.append(f"{cfg.profile}: profile not found; config left alone")
            continue
        _restore_config(cfg, profile, writer, shared, result)

    _copy_new(plan, by_name, copy_new_to, result)

    for r in reversed(plan.retired):
        if r.path.exists() or r.path.is_symlink():
            result.reports.append(
                f"{r.profile}: {r.path} exists now; the retired copy stays at {r.retired_path}"
            )
            continue
        if not (r.retired_path.exists() or r.retired_path.is_symlink()):
            result.reports.append(
                f"{r.profile}: retired copy {r.retired_path} is missing; {r.rel} not restored"
            )
            continue
        move_noreplace(r.retired_path, r.path)
        result.done.append(f"{r.profile}: {r.rel} put back")

    for c in plan.cron:
        _restore_cron(c, result)

    for profile, name in plan.shadowed_after:
        result.reports.append(f"{profile}: the restored {name} shadows the shared root's copy again")
    for ch in plan.shared_changes:
        result.reports.append(f"shared root: {ch.name} ({ch.rel}) is {ch.status} since setup and was kept")
    journal_note = layout.backup_dir(plan.backup_id) / "restore.json"
    journal_note.write_text(
        json.dumps({"pre_restore_backup_id": bid, "done": result.done, "reports": result.reports}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return result
