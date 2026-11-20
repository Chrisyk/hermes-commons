"""Carry out a setup proposal, journaling every step so restore can undo any prefix of it.

apply() runs the steps below in order and stops at the first failure,
returning what it did. After every completed step (each copy, each config,
each retire counts as one) it rewrites journal.json in the backup dir, so
the journal always lists exactly the work that happened.

1  Layout.check, the shared root apart from every profile skills dir, the
   proposal still current (every copy's fingerprint unchanged since it was
   built), then a 'setup' backup of every profile (config + skills).
2  git init the shared root if needed; commit any uncommitted state first
   as 'commons: snapshot before setup', so the setup commit holds only setup.
3  copy each move's source into shared_root/target_rel (fsynced like a
   backup); a target that already holds identical content is skipped, any
   other existing target fails the step.
4  commit 'commons setup <ts>' (the copied targets).
5  config: skills.create_dir -> shared root, 'commons' into plugins.enabled,
   through the ConfigWriter; config.yaml sha256 recorded before and after.
6  retire every copy listed in the moves into backups/<id>/retired/.
7  stale paths: rewrite cron prompts and shared-root markdown that name a
   retired copy (paths.py), backing each file up first; commit the
   markdown rewrites.
8  write shared_root/.commons/config.json and commit it.

The journal schema is in _Journal. Stdlib + PyYAML only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commons.core.backup import copy_tree, create_backup, file_sha256, load_manifest, write_json_atomic
from commons.core.configio import ConfigWriter
from commons.core.fleet import CONFIG_FILE, Profile
from commons.core.gitrepo import GitRepo
from commons.core.group import fingerprint, same_content
from commons.core.layout import Layout, check_outside
from commons.core.moves import retire
from commons.core.paths import (
    CRON_DIR,
    JOBS_FILE,
    find_stale_paths,
    load_jobs_file,
    rewrite,
    rewrite_backup_path,
)
from commons.core.proposal import CREATE_DIR_KEY, PLUGIN, PLUGINS_KEY, SHARED, Proposal, moved_paths

JOURNAL_VERSION = 1
SNAPSHOT_MESSAGE = "commons: snapshot before setup"
REWRITE_MESSAGE = "commons: rewrite stale skill paths"
CONFIG_MESSAGE = "commons: record setup"


class ApplyError(RuntimeError):
    """A step refused to go on; nothing after it ran."""


@dataclass
class ApplyResult:
    backup_id: str | None
    steps_done: list[str] = field(default_factory=list)
    error: str | None = None
    rewritten: list[str] = field(default_factory=list)
    not_rewritten: list[str] = field(default_factory=list)


class _Journal:
    """journal.json in the backup dir. Keys:

    version, backup_id, created, shared_root, state_dir, steps (names of
    completed steps, in order), error, complete;
    profiles: {name: {home, config_before_sha256, config_after_sha256,
      create_dir_set, create_dir_old, plugin_added}};
    git: {initialized, snapshot_sha, base_sha, setup_sha, final_sha} where
      base_sha is HEAD before setup touched the repo (after the snapshot) and
      final_sha the last commit setup made;
    copied: [{name, source_profile, source_rel, target_rel, skipped}];
    retired: [{name, profile, rel, path, retired_path}];
    cron: [{profile, jobs_path, backup_path, before_sha256, after_sha256}];
    file_rewrites: [{path, backup_path, old, new}];
    commons_config: {path, sha256} or null.
    """

    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = path
        self.data = data

    def step(self, name: str) -> None:
        self.data["steps"].append(name)
        self.save()

    def save(self) -> None:
        write_json_atomic(self.path, self.data)


def load_journal(layout: Layout, backup_id: str) -> dict[str, Any]:
    return json.loads(layout.journal_path(backup_id).read_text(encoding="utf-8"))


def _stamp(now: datetime) -> str:
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _skill_dir(prop: Proposal, profile: str, rel: str) -> Path:
    if profile == SHARED:
        return Path(prop.shared_root) / rel
    return Path(prop.profile_homes[profile]) / "skills" / rel


def _check_fresh(prop: Proposal) -> None:
    """Raise ApplyError when a copy the proposal judged has changed since."""
    for move in prop.moves:
        for key, expected in sorted(move.fingerprints.items()):
            profile, _, rel = key.partition(":")
            path = _skill_dir(prop, profile, rel)
            try:
                current = fingerprint(path)
            except OSError:
                current = None
            if current != expected:
                raise ApplyError(
                    f"{move.name}: {key} changed since the proposal was made; build a new proposal"
                )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_commons_config(layout: Layout, prop: Proposal, profiles: Iterable[str]) -> Path:
    path = layout.config_path
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    names = list(dict.fromkeys([*current.get("profiles", []), *profiles]))
    taken = list(current.get("taken_over", []))
    for t in prop.taken_over:
        entry = {"name": t.name, "profile": t.profile, "kind": t.kind, "origin_hash": t.origin_hash}
        if entry not in taken:
            taken.append(entry)
    data = {
        **current,
        "profiles": names,
        "taken_over": taken,
        "intentional_overrides": list(current.get("intentional_overrides", [])),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, data)
    return path


def apply(
    proposal: Proposal,
    profiles: list[Profile],
    layout: Layout,
    writer: ConfigWriter,
    now: datetime | None = None,
    home: Path | None = None,
) -> ApplyResult:
    """Carry out *proposal* (see module docstring); never raises for a failed step.

    *profiles* must include every profile the proposal names. *home* anchors
    ~ paths in cron prompts (default the user's home).
    """
    now = now or datetime.now(UTC)
    result = ApplyResult(backup_id=None)
    holder: list[_Journal] = []
    try:
        by_name = {p.name: p for p in profiles}
        missing = sorted(set(proposal.profile_homes) - set(by_name))
        if missing:
            raise ApplyError(f"profiles named in the proposal are missing: {', '.join(missing)}")
        if Path(proposal.shared_root) != Path(layout.shared_root):
            raise ApplyError("the proposal was made for another shared root")
        layout.check(profiles)
        check_outside(
            layout.shared_root,
            "the shared root",
            [(f"the skills dir of profile {p.name}", p.skills_dir.resolve()) for p in profiles],
        )
        _check_fresh(proposal)
        repo = GitRepo(layout.shared_root)
        with repo.locked():
            _run(proposal, profiles, by_name, layout, writer, now, home, repo, result, holder)
    except Exception as exc:  # noqa: BLE001  report, never raise: the journal says what happened
        result.error = f"{type(exc).__name__}: {exc}"
        if holder:
            holder[0].data["error"] = result.error
            holder[0].save()
    return result


def _run(
    prop: Proposal,
    profiles: list[Profile],
    by_name: dict[str, Profile],
    layout: Layout,
    writer: ConfigWriter,
    now: datetime,
    home: Path | None,
    repo: GitRepo,
    result: ApplyResult,
    holder: list[_Journal],
) -> None:
    # 1 backup
    bid = create_backup(layout, profiles, "setup", now=now)
    result.backup_id = bid
    manifest = load_manifest(layout, bid)
    backup_dir = layout.backup_dir(bid)
    journal = _Journal(
        layout.journal_path(bid),
        {
            "version": JOURNAL_VERSION,
            "backup_id": bid,
            "created": _stamp(now),
            "shared_root": str(layout.shared_root),
            "state_dir": str(layout.state_dir),
            "steps": [],
            "error": None,
            "complete": False,
            "profiles": {
                p.name: {
                    "home": str(p.home),
                    "config_before_sha256": manifest["profiles"][p.name]["config_sha256"],
                    "config_after_sha256": None,
                    "create_dir_set": False,
                    "create_dir_old": None,
                    "plugin_added": False,
                }
                for p in profiles
            },
            "git": {
                "initialized": False,
                "snapshot_sha": None,
                "base_sha": None,
                "setup_sha": None,
                "final_sha": None,
            },
            "copied": [],
            "retired": [],
            "cron": [],
            "file_rewrites": [],
            "commons_config": None,
        },
    )
    holder.append(journal)
    journal.step("backup")
    result.steps_done.append("backup")
    data = journal.data

    def done(name: str) -> None:
        journal.step(name)
        result.steps_done.append(name)

    # 2 git
    if not repo.is_repo():
        repo.init()
        data["git"]["initialized"] = True
    else:
        repo.init()  # idempotent: makes sure the state excludes are in place
    if repo.status_porcelain():
        data["git"]["snapshot_sha"] = repo.commit_all(SNAPSHOT_MESSAGE)
    data["git"]["base_sha"] = repo.head()
    data["git"]["final_sha"] = data["git"]["base_sha"]
    done("git")

    # 3 copy
    targets: list[Path] = []
    for move in prop.moves:
        if move.source_profile == SHARED:
            continue
        src = _skill_dir(prop, move.source_profile, move.source_rel)
        dst = Path(layout.shared_root) / move.target_rel
        entry = {
            "name": move.name,
            "source_profile": move.source_profile,
            "source_rel": move.source_rel,
            "target_rel": move.target_rel,
            "skipped": False,
        }
        if dst.exists() or dst.is_symlink():
            if not (dst.is_dir() and not dst.is_symlink() and same_content(src, dst)):
                raise ApplyError(f"{move.name}: {dst} already exists with different content")
            entry["skipped"] = True
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(src, dst)
            _fsync_dir(dst.parent)
            if not same_content(src, dst):
                raise ApplyError(f"{move.name}: the copy at {dst} does not match {src}")
            targets.append(dst)
        data["copied"].append(entry)
        done(f"copy {move.name}")

    # 4 commit
    if targets:
        sha = repo.commit_paths(targets, f"commons setup {_stamp(now)}")
        data["git"]["setup_sha"] = sha
        if sha:
            data["git"]["final_sha"] = sha
    done("commit")

    # 5 config
    shared = str(layout.shared_root)
    for p in profiles:
        changes = {c.key for c in prop.config_changes.get(p.name, [])}
        record = data["profiles"][p.name]
        if CREATE_DIR_KEY in changes:
            record["create_dir_old"] = writer.get(p, CREATE_DIR_KEY)
            writer.set(p, CREATE_DIR_KEY, shared)
            record["create_dir_set"] = True
        if PLUGINS_KEY in changes:
            current = writer.get(p, PLUGINS_KEY)
            if not (isinstance(current, list) and PLUGIN in current):
                writer.add_to_list(p, PLUGINS_KEY, PLUGIN)
                record["plugin_added"] = True
        record["config_after_sha256"] = file_sha256(p.home / CONFIG_FILE)
        done(f"config {p.name}")

    # 6 retire
    for move in prop.moves:
        for profile, rel in move.retire:
            path = by_name[profile].skills_dir / rel
            key = f"{profile}:{rel}"
            if key in move.fingerprints and fingerprint(path) != move.fingerprints[key]:
                raise ApplyError(f"{move.name}: {key} changed during setup; not retired")
            dest = retire(path, backup_dir, profile, rel)
            data["retired"].append(
                {
                    "name": move.name,
                    "profile": profile,
                    "rel": rel,
                    "path": str(path),
                    "retired_path": str(dest),
                }
            )
            done(f"retire {profile}:{rel}")

    # 7 stale paths
    cron_paths = {p.name: p.home / CRON_DIR / JOBS_FILE for p in profiles}
    cron_jobs = {name: load_jobs_file(path)[1] for name, path in cron_paths.items() if path.is_file()}
    before = {name: file_sha256(path) for name, path in cron_paths.items()}
    findings = find_stale_paths(profiles, layout.shared_root, moved_paths(prop), cron_jobs, home=home)
    rewritten, skipped = rewrite(findings, backup_dir, cron_paths)
    result.rewritten = [f"{f.location}: {f.old} -> {f.new}" for f in rewritten]
    result.not_rewritten = [f"{f.location}: {f.old} (no file at {f.new})" for f in skipped]
    files: list[Path] = []
    for f in rewritten:
        if f.kind == "cron":
            assert f.profile is not None
            if not any(c["profile"] == f.profile for c in data["cron"]):
                data["cron"].append(
                    {
                        "profile": f.profile,
                        "jobs_path": str(cron_paths[f.profile]),
                        "backup_path": str(rewrite_backup_path(backup_dir, f)),
                        "before_sha256": before[f.profile],
                        "after_sha256": file_sha256(cron_paths[f.profile]),
                    }
                )
        else:
            assert f.path is not None
            files.append(Path(f.path))
            data["file_rewrites"].append(
                {
                    "path": str(f.path),
                    "backup_path": str(rewrite_backup_path(backup_dir, f)),
                    "old": f.old,
                    "new": f.new,
                }
            )
    if files:
        sha = repo.commit_paths(sorted(set(files)), REWRITE_MESSAGE)
        if sha:
            data["git"]["final_sha"] = sha
    done("paths")

    # 8 commons config
    path = _write_commons_config(layout, prop, [p.name for p in profiles])
    sha = repo.commit_paths([path], CONFIG_MESSAGE)
    if sha:
        data["git"]["final_sha"] = sha
    data["commons_config"] = {"path": str(path), "sha256": file_sha256(path)}
    done("commons-config")
    data["complete"] = True
    journal.save()
