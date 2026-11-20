"""Undo a setup surgically, from its write-ahead journal, without losing work done since.

plan_restore reads journal.json and turns every intent apply wrote, pending
or done, into something restore can undo. It never trusts the intent state
alone: for each one it looks at the file system to decide whether the action
actually happened (see _retire_state, _config_state, _sha_state), so a crash
between an action and its journal entry is undone exactly. Whatever cannot
be decided is reported, never guessed.

restore() takes the shared root's git lock for its whole run (it does not
modify the shared root, but must not race a hook commit or an apply), checks
the setup backup with verify_backup (problems are reported, restore goes
on), takes a 'pre-restore' backup, then:

- config.yaml: if it is as setup left it (its sha256 is the one commons
  last wrote, or, when the newest config change is still pending, its
  parsed content equals the pre-setup config plus the journaled changes,
  which covers a crash mid-change) and apply never recorded
  config_changed_externally for the profile, the backed-up file is written
  back byte for byte; otherwise only the journaled keys that still hold
  setup's value are reverted, and that is reported;
- copy_new_to: the named shared skills are copied into the chosen profiles
  (before the put-back below, so the user's choice wins);
- retired copies are moved back after their tree_sha256 is checked against
  the journal; a missing retired copy is taken from the backup's skills/
  snapshot (when its hash matches), a changed one is reported and left;
  if something is at the original place now, the copy stays in the backup;
- cron jobs.json: written back the sha-or-report way, under Hermes' cron lock,
  from the newest cron_write intent of each file (superseded ones, whose
  file was edited between a crash and a resume, are never used), and only
  when the file still holds the sha256 commons wrote;
- profile_copy intents (a shared skill copied into a profile by demote,
  journal.py): the copy is moved aside into the pre-restore backup's
  displaced/ folder, never deleted, when its tree_sha256 still matches.

The shared root is never modified: copies into it, commits and markdown
rewrites there are kept (markdown rewrites, partial copies and the partial
copies a resume set aside into the backup are listed).
Every run is appended to backups/<id>/restore.json. Stdlib + PyYAML only.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from commons.core.apply import CONFIG_OPS, JOURNAL_VERSION, PENDING, config_states, intents, newest_writes
from commons.core.backup import (
    MANIFEST,
    SKILLS,
    copy_tree,
    create_backup,
    file_sha256,
    tree_sha256,
    verify_backup,
    write_json_atomic,
)
from commons.core.configio import ConfigWriter, lookup_key
from commons.core.fleet import CONFIG_FILE, Profile, load_config
from commons.core.gitrepo import GitRepo
from commons.core.layout import Layout
from commons.core.moves import move_noreplace
from commons.core.paths import cron_lock, write_atomic
from commons.core.proposal import CREATE_DIR_KEY, SHARED
from commons.core.resolve import config_path
from commons.core.scan import SkillCopy, scan_root

REPORT_KEYS_ONLY = "config changed after setup; reverted commons keys only"
RESTORE_LOG = "restore.json"


class RestoreError(RuntimeError):
    """A backup that cannot be restored at all (missing, unreadable, unknown journal)."""


@dataclass
class ConfigPlan:
    profile: str
    home: Path
    backup_path: Path
    before_sha256: str | None
    intents: list[dict[str, Any]] = field(default_factory=list)  # config_set / config_list_add args+state
    changed_externally: bool = False  # apply saw an edit not its own (journal config_changed_externally)

    @property
    def last_written_sha256(self) -> str | None:
        """The sha256 of config.yaml as commons itself last wrote it (before setup if never)."""
        written = [
            i["args"]["config_sha256_after"] for i in self.intents if i["args"].get("config_sha256_after")
        ]
        return written[-1] if written else self.before_sha256

    def create_dir_intent(self) -> dict[str, Any] | None:
        return next(
            (i for i in self.intents if i["op"] == "config_set" and i["args"]["key"] == CREATE_DIR_KEY), None
        )


@dataclass
class RetiredPlan:
    name: str
    profile: str
    rel: str
    path: Path
    retired_path: Path
    fingerprint: str | None
    snapshot_path: Path
    state: str  # 'done' or 'pending' in the journal
    found: str  # what plan_restore saw: 'retired', 'both', 'neither'


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
    md_rewrites: list[str] = field(default_factory=list)  # shared-root markdown setup rewrote (kept)
    partial_copies: list[str] = field(default_factory=list)  # pending copies into the shared root (kept)
    set_aside: list[tuple[str, str]] = field(default_factory=list)  # (partial copy, where a resume moved it)
    undecided: list[str] = field(default_factory=list)
    shared_changes: list[SharedChange] = field(default_factory=list)
    shadowed_after: list[tuple[str, str]] = field(default_factory=list)  # (profile, name)
    homes: dict[str, Path] = field(default_factory=dict)  # profile -> home, from the journal
    profile_copies: list[dict[str, Any]] = field(default_factory=list)  # demote copies to move aside

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


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


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


def load_restore_journal(layout: Layout, backup_id: str) -> dict[str, Any]:
    """The journal of *backup_id*, or RestoreError when there is none to restore from."""
    try:
        path = layout.journal_path(backup_id)
    except ValueError as exc:
        raise RestoreError(str(exc)) from None
    if not layout.backup_dir(backup_id).is_dir():
        raise RestoreError(f"no backup {backup_id!r} in {layout.backups_dir}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RestoreError(
            f"backup {backup_id!r} has no setup journal ({path}); nothing to restore"
        ) from None
    except (OSError, ValueError) as exc:
        raise RestoreError(f"cannot read the journal of backup {backup_id!r}: {exc}") from None
    if not isinstance(data, dict) or data.get("version") != JOURNAL_VERSION:
        raise RestoreError(f"backup {backup_id!r}: journal version {data.get('version')!r} is not supported")
    return data


def _retire_found(path: Path, retired: Path) -> str:
    src, dst = _exists(path), _exists(retired)
    if dst and not src:
        return "retired"
    if src and not dst:
        return "in-place"
    return "both" if src else "neither"


def plan_restore(layout: Layout, backup_id: str) -> RestorePlan:
    """Read the journal of *backup_id* and list what restore would do (see module docstring)."""
    j = load_restore_journal(layout, backup_id)
    backup_dir = layout.backup_dir(backup_id)
    since = j["git"].get("final_sha") or j["git"].get("base_sha")
    plan = RestorePlan(
        backup_id=backup_id,
        shared_root=Path(j["shared_root"]),
        state_dir=Path(j["state_dir"]),
        complete=bool(j.get("complete")),
        since_sha=since,
        homes={name: Path(rec["home"]) for name, rec in j["profiles"].items()},
    )
    configs: dict[str, ConfigPlan] = {}
    newest = {id(i) for op in ("cron_write", "md_write") for i in newest_writes(j, op)}
    for i in intents(j):
        op, args = i["op"], i["args"]
        if op in ("cron_write", "md_write") and id(i) not in newest:
            continue  # superseded by a newer intent for the same file
        if op in CONFIG_OPS:
            name = args["profile"]
            if name not in configs:
                rec = j["profiles"][name]
                configs[name] = ConfigPlan(
                    name,
                    Path(rec["home"]),
                    backup_dir / name / CONFIG_FILE,
                    rec["config_before_sha256"],
                    changed_externally=bool(rec.get("config_changed_externally")),
                )
            configs[name].intents.append(i)
        elif op == "retire":
            src, dst = Path(args["src"]), Path(args["dst"])
            found = _retire_found(src, dst)
            if found == "in-place":
                continue  # it never happened: nothing to undo
            plan.retired.append(
                RetiredPlan(
                    args["name"],
                    args["profile"],
                    args["rel"],
                    src,
                    dst,
                    args.get("fingerprint"),
                    backup_dir / args["profile"] / SKILLS / args["rel"],
                    i["state"],
                    found,
                )
            )
        elif op == "cron_write":
            plan.cron.append(
                CronPlan(
                    args["profile"],
                    Path(args["path"]),
                    Path(args["backup_copy"]),
                    args["sha_before"],
                    args["sha_after"],
                )
            )
        elif op == "md_write":
            current = file_sha256(Path(args["path"]))
            if current == args["sha_after"]:
                plan.md_rewrites.append(
                    f"{args['path']} was rewritten by setup; kept (original at {args['backup_copy']})"
                )
            elif current != args["sha_before"]:
                plan.undecided.append(f"shared root: {args['path']} changed since setup rewrote it; kept")
        elif op == "profile_copy" and _exists(Path(args["dst"])):
            plan.profile_copies.append(dict(args))
        elif op == "copy" and i["state"] == PENDING and _exists(Path(args["target"])):
            plan.partial_copies.append(args["target"])
        elif op == "partial_set_aside" and (i["state"] != PENDING or _exists(Path(args["dst"]))):
            plan.set_aside.append((args["src"], args["dst"]))
    for aside in j.get("partials_set_aside", []):  # journals written before partial_set_aside intents
        plan.set_aside.append(("", aside))
    plan.configs = list(configs.values())
    plan.shared_changes = _shared_changes(plan.shared_root, since)
    plan.shadowed_after = _shadowed_after(plan)
    return plan


def _create_dir_after(plan: RestorePlan, profile: str, home: Path) -> Path | None:
    """Where this profile's skills.create_dir points once restore has run."""
    cfg = next((c for c in plan.configs if c.profile == profile), None)
    intent = cfg.create_dir_intent() if cfg is not None else None
    if intent is not None:
        value = intent["args"]["old_value"]
    else:
        value = lookup_key(load_config(home / CONFIG_FILE), CREATE_DIR_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    return config_path(home, Path(value))


def _shadowed_after(plan: RestorePlan) -> list[tuple[str, str]]:
    shared = config_path(Path("/"), plan.shared_root)
    names = {c.name for c in _shared_copies(plan.shared_root)}
    out: list[tuple[str, str]] = []
    for r in plan.retired:
        if r.name in names and _create_dir_after(plan, r.profile, plan.homes[r.profile]) == shared:
            out.append((r.profile, r.name))
    return sorted(set(out))


# ------------------------------------------------------------------ config


def _as_setup_left_it(cfg: ConfigPlan, path: Path) -> bool:
    """True when config.yaml is byte for byte what commons last wrote, and nobody else wrote it."""
    if cfg.changed_externally:
        return False
    current = file_sha256(path)
    if current is None:
        return False
    if current == cfg.last_written_sha256:
        return True
    if not cfg.intents or cfg.intents[-1]["state"] != PENDING:
        return False
    # A crash right after commons' newest write, before its sha256 was journaled.
    base = load_config(cfg.backup_path) if cfg.before_sha256 is not None else {}
    return load_config(path) == config_states(base, cfg.intents)[-1]


def _set_happened(args: Mapping[str, Any], value: Any, home: Path, shared: Path) -> bool:
    if args["key"] == CREATE_DIR_KEY:
        return isinstance(value, str) and bool(value.strip()) and config_path(home, Path(value)) == shared
    return value == args["new_value"]


def _restore_config(
    cfg: ConfigPlan, profile: Profile, writer: ConfigWriter, shared: Path, result: RestoreResult
) -> None:
    path = profile.home / CONFIG_FILE
    current = file_sha256(path)
    if current == cfg.before_sha256:
        if not any(i["state"] == PENDING for i in cfg.intents):
            result.reports.append(f"{cfg.profile}: config.yaml already as before setup")
        return
    if _as_setup_left_it(cfg, path):
        if cfg.before_sha256 is None:
            # There was no config before setup: move the one setup made aside, never delete it.
            dest = result.pre_restore_dir / "displaced" / cfg.profile / CONFIG_FILE
            move_noreplace(path, dest)
            result.done.append(f"{cfg.profile}: config.yaml moved aside to {dest} (none before setup)")
            return
        if file_sha256(cfg.backup_path) != cfg.before_sha256:
            result.reports.append(f"{cfg.profile}: backed-up config.yaml does not match its hash; left alone")
            return
        write_atomic(path, cfg.backup_path.read_bytes())
        result.done.append(f"{cfg.profile}: config.yaml restored")
        return
    reverted = []
    for i in reversed(cfg.intents):
        args = i["args"]
        value = writer.get(profile, args["key"])
        if i["op"] == "config_set":
            if not _set_happened(args, value, profile.home, shared):
                if i["state"] != PENDING:
                    result.reports.append(
                        f"{cfg.profile}: {args['key']} no longer holds setup's value; left alone"
                    )
                continue
            if args["old_value_present"]:
                writer.set(profile, args["key"], args["old_value"])
            else:
                writer.unset(profile, args["key"])
            reverted.append(args["key"])
        elif not args.get("was_present") and isinstance(value, list) and args["item"] in value:
            writer.remove_from_list(profile, args["key"], args["item"])
            reverted.append(args["key"])
    keys = ", ".join(reverted) if reverted else "none"
    result.reports.append(f"{cfg.profile}: {REPORT_KEYS_ONLY} ({keys})")


# ------------------------------------------------------------------ retire


def _restore_retired(r: RetiredPlan, result: RestoreResult) -> None:
    src_here, dst_here = _exists(r.path), _exists(r.retired_path)
    if src_here:
        if dst_here:
            result.reports.append(
                f"{r.profile}: {r.path} exists now; the retired copy stays at {r.retired_path}"
            )
        elif r.state == PENDING:
            pass  # the retire never happened after all
        else:
            result.reports.append(
                f"{r.profile}: {r.path} exists now and its retired copy is missing; left alone"
            )
        return
    if dst_here:
        actual = tree_sha256(r.retired_path)
        if actual != r.fingerprint:
            result.reports.append(
                f"{r.profile}: retired copy {r.retired_path} does not match the journal; "
                f"{r.rel} not restored (backup snapshot: {r.snapshot_path})"
            )
            return
        move_noreplace(r.retired_path, r.path)
        result.done.append(f"{r.profile}: {r.rel} put back")
        return
    # Neither place has it: the retired copy is missing.
    if _exists(r.snapshot_path) and tree_sha256(r.snapshot_path) == r.fingerprint:
        r.path.parent.mkdir(parents=True, exist_ok=True)
        copy_tree(r.snapshot_path, r.path)
        result.done.append(f"{r.profile}: {r.rel} put back from the backup snapshot")
        result.reports.append(
            f"{r.profile}: retired copy {r.retired_path} is missing; restored {r.rel} from {r.snapshot_path}"
        )
        return
    result.reports.append(f"{r.profile}: retired copy {r.retired_path} is missing; {r.rel} not restored")


# ------------------------------------------------------------------ cron


def _restore_cron(c: CronPlan, result: RestoreResult) -> None:
    with cron_lock(c.jobs_path):
        current = file_sha256(c.jobs_path)
        if current == c.before_sha256:
            return
        if current != c.after_sha256:
            result.reports.append(f"{c.profile}: {c.jobs_path} changed after setup; left alone")
            return
        if file_sha256(c.backup_path) != c.before_sha256:
            result.reports.append(
                f"{c.profile}: backup of {c.jobs_path} does not match the journal; left alone"
            )
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
            if _exists(dest):
                result.reports.append(f"{profile}: {dest} already exists; {name} not copied")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(c.path, dest)
            result.done.append(f"{profile}: copied shared {name} to {dest}")


def _undo_profile_copy(args: Mapping[str, Any], result: RestoreResult) -> None:
    """Move a copy demote wrote into a profile aside (never deleted), unless it changed since."""
    dst = Path(args["dst"])
    if not _exists(dst):
        return
    if args.get("fingerprint") is None or tree_sha256(dst) != args["fingerprint"]:
        result.reports.append(f"{args['profile']}: {dst} changed since it was copied in; left alone")
        return
    aside = result.pre_restore_dir / "displaced" / args["profile"] / args["rel"]
    move_noreplace(dst, aside)
    result.done.append(f"{args['profile']}: {args['rel']} moved aside to {aside}")


@contextlib.contextmanager
def _shared_lock(shared_root: Path) -> Iterator[None]:
    """The shared root's git lock, when there is a shared root to lock (never created here)."""
    if Path(shared_root).is_dir():
        with GitRepo(shared_root).locked():
            yield
    else:
        yield


def _append_run(path: Path, run: dict[str, Any]) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    runs = existing.get("runs") if isinstance(existing, dict) else None
    if not isinstance(runs, list):
        runs = [existing] if isinstance(existing, dict) and existing else []
    write_json_atomic(path, {"runs": [*runs, run]})


def _backup_problems(layout: Layout, backup_id: str) -> list[str]:
    if not (layout.backup_dir(backup_id) / MANIFEST).is_file():
        return [f"backup {backup_id}: no manifest; config and snapshot checks rely on the journal only"]
    return [f"backup {backup_id}: {p}" for p in verify_backup(layout, backup_id)]


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
    with _shared_lock(plan.shared_root):
        problems = _backup_problems(layout, plan.backup_id)
        bid = create_backup(layout, profiles, "pre-restore", now=now)
        result = RestoreResult(pre_restore_backup_id=bid, pre_restore_dir=layout.backup_dir(bid))
        result.reports += problems
        _restore_all(plan, by_name, writer, copy_new_to, result)
        _append_run(
            layout.backup_dir(plan.backup_id) / RESTORE_LOG,
            {"pre_restore_backup_id": bid, "done": result.done, "reports": result.reports},
        )
    return result


def _restore_all(
    plan: RestorePlan,
    by_name: Mapping[str, Profile],
    writer: ConfigWriter,
    copy_new_to: Mapping[str, Sequence[str]],
    result: RestoreResult,
) -> None:
    shared = config_path(Path("/"), plan.shared_root)
    for cfg in plan.configs:
        profile = by_name.get(cfg.profile)
        if profile is None:
            result.reports.append(f"{cfg.profile}: profile not found; config left alone")
            continue
        _restore_config(cfg, profile, writer, shared, result)

    _copy_new(plan, by_name, copy_new_to, result)

    for pc in reversed(plan.profile_copies):
        _undo_profile_copy(pc, result)

    for r in reversed(plan.retired):
        _restore_retired(r, result)

    for c in reversed(plan.cron):
        _restore_cron(c, result)

    result.reports += plan.undecided
    for src, aside in plan.set_aside:
        what = f"partial copy {src}" if src else "a partial copy"
        result.reports.append(
            f"shared root: {what} from the interrupted setup was set aside to {aside}; kept"
        )
    for target in plan.partial_copies:
        result.reports.append(f"shared root: {target} may be a partial copy from the interrupted setup; kept")
    for md in plan.md_rewrites:
        result.reports.append(f"shared root: {md}")
    for profile, name in plan.shadowed_after:
        result.reports.append(f"{profile}: the restored {name} shadows the shared root's copy again")
    for ch in plan.shared_changes:
        result.reports.append(f"shared root: {ch.name} ({ch.rel}) is {ch.status} since setup and was kept")
