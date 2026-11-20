"""Carry out a setup proposal with a write-ahead journal, so restore can undo any prefix of it.

Every action that changes a file is first written to journal.json as an
intent {op, args, state: 'pending'} (atomic write + fsync), then carried
out, then flipped to state 'done'. A crash at any point therefore leaves at
most one pending intent, and restore decides from the file system whether
that one happened. The phases, in order (each listed in journal 'steps' once
it finished):

1  Layout.check, the shared root apart from every profile skills dir, the
   proposal still current (every copy's fingerprint unchanged since it was
   built), then a 'setup' backup of every profile (config + skills).
2  git: init the shared root if needed (op git_init); commit any
   uncommitted state first as 'commons: snapshot before setup' (op commit),
   so the setup commit holds only setup.
3  copy: each move's source into shared_root/target_rel (op copy, fsynced
   like a backup); a target that already holds identical content is
   recorded as skipped, any other existing target fails the phase.
4  commit 'commons setup <ts>' (op commit) of the copied targets.
5  config: skills.create_dir -> shared root (op config_set) and 'commons'
   into plugins.enabled (op config_list_add), through the ConfigWriter.
6  retire: every copy listed in the moves into backups/<id>/retired/
   (op retire, with the tree_sha256 of the copy so restore can check it).
7  paths: rewrite cron prompts (op cron_write, one per jobs.json, under
   Hermes' cron lock) and shared-root markdown (op md_write) that name a
   retired copy, each file backed up first; commit the markdown rewrites.
8  commons-config: write shared_root/.commons/config.json (op config_json)
   and commit it.

Intent args (all paths absolute strings):
  git_init {path}
  commit {kind: snapshot|setup|rewrite|config, message, head_before}, done adds sha
  copy {name, source_profile, source_rel, target_rel, target, fingerprint, skipped}
  config_set {profile, key, old_value_present, old_value, new_value},
    done adds config_sha256_after
  config_list_add {profile, key, item, was_present}, done adds config_sha256_after
  retire {name, profile, rel, src, dst, fingerprint}  (fingerprint = backup.tree_sha256)
  cron_write {profile, path, backup_copy, sha_before, sha_after}
  md_write {path, backup_copy, sha_before, sha_after}
  config_json {path, sha_before}, done adds sha_after

Other journal keys: version, backup_id, created, shared_root, state_dir,
steps, error, complete, profiles {name: {home, config_before_sha256}},
git {initialized, snapshot_sha, base_sha, final_sha}. Stdlib + PyYAML only.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commons.core.backup import (
    copy_tree,
    create_backup,
    file_sha256,
    load_manifest,
    tree_sha256,
    write_json_atomic,
)
from commons.core.configio import ConfigWriter, has_key
from commons.core.fleet import CONFIG_FILE, Profile, load_config
from commons.core.gitrepo import GitRepo
from commons.core.group import fingerprint, same_content
from commons.core.layout import Layout, check_outside
from commons.core.moves import retire, retired_path
from commons.core.paths import (
    CRON_DIR,
    JOBS_FILE,
    PlannedWrite,
    find_stale_paths,
    load_jobs_file,
    rewrite,
)
from commons.core.proposal import CREATE_DIR_KEY, PLUGIN, PLUGINS_KEY, SHARED, Proposal, moved_paths

JOURNAL_VERSION = 2
SNAPSHOT_MESSAGE = "commons: snapshot before setup"
REWRITE_MESSAGE = "commons: rewrite stale skill paths"
CONFIG_MESSAGE = "commons: record setup"
PENDING = "pending"
DONE = "done"


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
    """journal.json in the backup dir (schema in the module docstring)."""

    def __init__(self, path: Path, data: dict[str, Any]):
        self.path = path
        self.data = data

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    def step(self, name: str) -> None:
        self.data["steps"].append(name)
        self.save()

    def begin(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Record an intent as pending, durably, before its action runs."""
        entry = {"op": op, "args": dict(args), "state": PENDING}
        self.data["intents"].append(entry)
        self.save()
        return entry

    def done(self, entry: dict[str, Any], **updates: Any) -> None:
        """Flip an intent to done once its action finished."""
        entry["args"].update(updates)
        entry["state"] = DONE
        self.save()

    def record(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """An intent whose action needs no change on disk (already in place)."""
        entry = {"op": op, "args": dict(args), "state": DONE}
        self.data["intents"].append(entry)
        self.save()
        return entry


def load_journal(layout: Layout, backup_id: str) -> dict[str, Any]:
    return json.loads(layout.journal_path(backup_id).read_text(encoding="utf-8"))


def intents(journal: Mapping[str, Any], op: str | None = None) -> list[dict[str, Any]]:
    """The journal's intents, optionally only those of one op, in the order they were written."""
    return [i for i in journal.get("intents", []) if op is None or i["op"] == op]


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


def _commons_config_data(layout: Layout, prop: Proposal, profiles: Iterable[str]) -> dict[str, Any]:
    try:
        current = json.loads(layout.config_path.read_text(encoding="utf-8"))
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
    return {
        **current,
        "profiles": names,
        "taken_over": taken,
        "intentional_overrides": list(current.get("intentional_overrides", [])),
    }


def apply(
    proposal: Proposal,
    profiles: list[Profile],
    layout: Layout,
    writer: ConfigWriter,
    now: datetime | None = None,
    home: Path | None = None,
) -> ApplyResult:
    """Carry out *proposal* (see module docstring); a failed step is reported, not raised.

    *profiles* must include every profile the proposal names. *home* anchors
    ~ paths in cron prompts (default the user's home). Any exception is
    recorded in the result and the journal; a BaseException that is not an
    Exception (KeyboardInterrupt, SystemExit) is recorded and then re-raised.
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
    except BaseException as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        if holder:
            holder[0].data["error"] = result.error
            # The journal must never hide the original failure.
            with contextlib.suppress(Exception):
                holder[0].save()
        if not isinstance(exc, Exception):
            raise
    return result


class _RewriteHook:
    """paths.rewrite WriteHook: journal each cron/markdown write ahead of it."""

    def __init__(self, journal: _Journal):
        self.journal = journal
        self.entry: dict[str, Any] | None = None

    def before(self, write: PlannedWrite) -> None:
        args: dict[str, Any] = {
            "path": str(write.path),
            "backup_copy": str(write.backup),
            "sha_before": write.sha_before,
            "sha_after": write.sha_after,
        }
        if write.kind == "cron":
            self.entry = self.journal.begin("cron_write", {"profile": write.profile, **args})
        else:
            self.entry = self.journal.begin("md_write", args)

    def after(self, write: PlannedWrite) -> None:
        assert self.entry is not None
        self.journal.done(self.entry)
        self.entry = None


def _commit(
    journal: _Journal, repo: GitRepo, kind: str, message: str, paths: list[Path] | None
) -> str | None:
    entry = journal.begin("commit", {"kind": kind, "message": message, "head_before": repo.head()})
    sha = repo.commit_all(message) if paths is None else repo.commit_paths(paths, message)
    journal.done(entry, sha=sha)
    if sha:
        journal.data["git"]["final_sha"] = sha
    return sha


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
                }
                for p in profiles
            },
            "git": {"initialized": False, "snapshot_sha": None, "base_sha": None, "final_sha": None},
            "intents": [],
        },
    )
    holder.append(journal)
    data = journal.data

    def done(name: str) -> None:
        journal.step(name)
        result.steps_done.append(name)

    done("backup")

    # 2 git
    if not repo.is_repo():
        entry = journal.begin("git_init", {"path": str(layout.shared_root)})
        repo.init()
        data["git"]["initialized"] = True
        journal.done(entry)
    else:
        repo.init()  # idempotent: makes sure the state excludes are in place
    if repo.status_porcelain():
        data["git"]["snapshot_sha"] = _commit(journal, repo, "snapshot", SNAPSHOT_MESSAGE, None)
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
        args = {
            "name": move.name,
            "source_profile": move.source_profile,
            "source_rel": move.source_rel,
            "target_rel": move.target_rel,
            "target": str(dst),
            "fingerprint": move.fingerprints.get(f"{move.source_profile}:{move.source_rel}"),
            "skipped": False,
        }
        if dst.exists() or dst.is_symlink():
            if not (dst.is_dir() and not dst.is_symlink() and same_content(src, dst)):
                raise ApplyError(f"{move.name}: {dst} already exists with different content")
            journal.record("copy", {**args, "skipped": True})
            continue
        entry = journal.begin("copy", args)
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy_tree(src, dst)
        _fsync_dir(dst.parent)
        if not same_content(src, dst):
            raise ApplyError(f"{move.name}: the copy at {dst} does not match {src}")
        targets.append(dst)
        journal.done(entry)
    done("copy")

    # 4 commit
    if targets:
        _commit(journal, repo, "setup", f"commons setup {_stamp(now)}", targets)
    done("commit")

    # 5 config
    shared = str(layout.shared_root)
    for p in profiles:
        changes = {c.key for c in prop.config_changes.get(p.name, [])}
        config_file = p.home / CONFIG_FILE
        if CREATE_DIR_KEY in changes:
            entry = journal.begin(
                "config_set",
                {
                    "profile": p.name,
                    "key": CREATE_DIR_KEY,
                    "old_value_present": has_key(load_config(config_file), CREATE_DIR_KEY),
                    "old_value": writer.get(p, CREATE_DIR_KEY),
                    "new_value": shared,
                },
            )
            writer.set(p, CREATE_DIR_KEY, shared)
            journal.done(entry, config_sha256_after=file_sha256(config_file))
        if PLUGINS_KEY in changes:
            current = writer.get(p, PLUGINS_KEY)
            args = {"profile": p.name, "key": PLUGINS_KEY, "item": PLUGIN}
            if isinstance(current, list) and PLUGIN in current:
                journal.record("config_list_add", {**args, "was_present": True})
            else:
                entry = journal.begin("config_list_add", {**args, "was_present": False})
                writer.add_to_list(p, PLUGINS_KEY, PLUGIN)
                journal.done(entry, config_sha256_after=file_sha256(config_file))
    done("config")

    # 6 retire
    for move in prop.moves:
        for profile, rel in move.retire:
            path = by_name[profile].skills_dir / rel
            key = f"{profile}:{rel}"
            if key in move.fingerprints and fingerprint(path) != move.fingerprints[key]:
                raise ApplyError(f"{move.name}: {key} changed during setup; not retired")
            entry = journal.begin(
                "retire",
                {
                    "name": move.name,
                    "profile": profile,
                    "rel": rel,
                    "src": str(path),
                    "dst": str(retired_path(backup_dir, profile, rel)),
                    "fingerprint": tree_sha256(path),
                },
            )
            retire(path, backup_dir, profile, rel)
            journal.done(entry)
    done("retire")

    # 7 stale paths
    cron_paths = {p.name: p.home / CRON_DIR / JOBS_FILE for p in profiles}
    cron_jobs = {name: load_jobs_file(path)[1] for name, path in cron_paths.items() if path.is_file()}
    findings = find_stale_paths(profiles, layout.shared_root, moved_paths(prop), cron_jobs, home=home)
    rewritten, skipped = rewrite(findings, backup_dir, cron_paths, hook=_RewriteHook(journal))
    result.rewritten = [f"{f.location}: {f.old} -> {f.new}" for f in rewritten]
    result.not_rewritten = [f"{f.location}: {f.old} (no file at {f.new})" for f in skipped]
    files = sorted({Path(f.path) for f in rewritten if f.kind == "skill-file" and f.path is not None})
    if files:
        _commit(journal, repo, "rewrite", REWRITE_MESSAGE, files)
    done("paths")

    # 8 commons config
    path = layout.config_path
    entry = journal.begin("config_json", {"path": str(path), "sha_before": file_sha256(path)})
    config_data = _commons_config_data(layout, prop, [p.name for p in profiles])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, config_data)
    journal.done(entry, sha_after=file_sha256(path))
    _commit(journal, repo, "config", CONFIG_MESSAGE, [path])
    done("commons-config")
    data["complete"] = True
    journal.save()
