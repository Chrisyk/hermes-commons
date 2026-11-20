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
9  verify: check the fleet (verify.py) against the names each profile could
   load before setup; problems go to result.problems and journal
   'problems' (result.error stays None: setup did finish).

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

A resumed apply (apply(..., resume=<backup id>)) reuses that backup,
its journal and the proposal saved with it (backups/<id>/proposal.json,
load_setup_proposal): phases listed in 'steps' are skipped, done intents are skipped,
and each pending intent is checked against the file system first (done
when its effect is there, redone when it is not). A pending copy whose
target exists is moved into backups/<id>/partial/ (never deleted) and
copied again. Without resume, apply refuses while any setup journal is
neither complete nor restored.

Other journal keys: version, proposal_sha256, names_before, resumed, backup_id, created, shared_root, state_dir,
steps, error, complete, problems, profiles {name: {home, config_before_sha256}},
git {initialized, snapshot_sha, base_sha, final_sha}. Stdlib + PyYAML only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
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
from commons.core.moves import move_noreplace, retire, retired_path
from commons.core.paths import (
    CRON_DIR,
    JOBS_FILE,
    PlannedWrite,
    find_stale_paths,
    load_jobs_file,
    rewrite,
    write_atomic,
)
from commons.core.proposal import CREATE_DIR_KEY, PLUGIN, PLUGINS_KEY, SHARED, Proposal, moved_paths
from commons.core.verify import resolvable_names, verify

JOURNAL_VERSION = 2
JOURNAL = "journal.json"
RESTORE_LOG = "restore.json"
PARTIAL = "partial"
PROPOSAL_FILE = "proposal.json"
SNAPSHOT_MESSAGE = "commons: snapshot before setup"
REWRITE_MESSAGE = "commons: rewrite stale skill paths"
CONFIG_MESSAGE = "commons: record setup"
PENDING = "pending"
DONE = "done"


class ApplyError(RuntimeError):
    """A step refused to go on; nothing after it ran."""


def unfinished_setups(layout: Layout) -> list[str]:
    """Backup ids whose setup journal is neither complete nor restored, oldest first."""
    out = []
    root = Path(layout.backups_dir)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        try:
            data = json.loads((d / JOURNAL).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and not data.get("complete") and not (d / RESTORE_LOG).exists():
            out.append(d.name)
    return out


def pending_copies(layout: Layout) -> dict[str, str]:
    """Copy target -> backup id, for every copy any setup journal left pending (oldest first wins)."""
    out: dict[str, str] = {}
    root = Path(layout.backups_dir)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        try:
            data = json.loads((d / JOURNAL).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for i in data.get("intents", []) if isinstance(data, dict) else []:
            if i.get("op") == "copy" and i.get("state") == PENDING:
                out.setdefault(i["args"]["target"], d.name)
    return out


def load_setup_proposal(layout: Layout, backup_id: str) -> Proposal:
    """The proposal a setup started from (saved in its backup), to resume it with."""
    return Proposal.from_json((layout.backup_dir(backup_id) / PROPOSAL_FILE).read_text(encoding="ascii"))


def proposal_sha256(prop: Proposal) -> str:
    return hashlib.sha256(prop.to_json().encode("ascii")).hexdigest()


@dataclass
class ApplyResult:
    backup_id: str | None
    steps_done: list[str] = field(default_factory=list)
    error: str | None = None
    rewritten: list[str] = field(default_factory=list)
    not_rewritten: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # verify() after setup; empty = clean


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

    def find(self, op: str, **match: Any) -> dict[str, Any] | None:
        """The last intent of *op* whose args hold every key=value in *match*."""
        for entry in reversed(self.data["intents"]):
            if entry["op"] == op and all(entry["args"].get(k) == v for k, v in match.items()):
                return entry
        return None

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


def _skill_dir(prop: Proposal, label: str, rel: str) -> Path:
    return prop.source_root(label) / rel


def _no_hash(_path: Path) -> str:
    return ""


def _check_profiles(proposal: Proposal, profiles: list[Profile]) -> None:
    """Raise ApplyError unless the fleet is the one the proposal was made for."""
    by_name = {p.name: p for p in profiles}
    missing = sorted(set(proposal.profile_homes) - set(by_name))
    if missing:
        raise ApplyError(f"profiles named in the proposal are missing: {', '.join(missing)}")
    extra = sorted(set(by_name) - set(proposal.profile_homes))
    if extra:
        raise ApplyError(f"profiles not in the proposal: {', '.join(extra)}; build a new proposal")
    moved = sorted(n for n, home in proposal.profile_homes.items() if Path(home) != Path(by_name[n].home))
    if moved:
        raise ApplyError(f"profile homes differ from the proposal: {', '.join(moved)}; build a new proposal")


def _check_fresh(prop: Proposal, retired: Iterable[str] = ()) -> None:
    """Raise ApplyError when a copy the proposal judged has changed since.

    *retired* holds the paths a resumed apply already retired; they are not
    where the proposal saw them any more, by design.
    """
    gone = set(retired)
    for move in prop.moves:
        for key, expected in sorted(move.fingerprints.items()):
            label, rel = prop.split_key(key)
            path = _skill_dir(prop, label, rel)
            if str(path) in gone:
                continue
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
    md5_dir_hash: Callable[[Path], str] = _no_hash,
    hub_hash: Callable[[Path], str] = _no_hash,
    resume: str | None = None,
) -> ApplyResult:
    """Carry out *proposal* (see module docstring); a failed step is reported, not raised.

    *profiles* must be exactly the profiles the proposal names, at the same
    homes. *home* anchors ~ paths in cron prompts (default the user's home).
    The hash functions only feed provenance in the final verify, which does
    not change what resolves; they default to a no-op. *resume* continues
    the interrupted setup with that backup id. Any exception is recorded in
    the result and the journal; a BaseException that is not an Exception
    (KeyboardInterrupt, SystemExit) is recorded and then re-raised.
    """
    now = now or datetime.now(UTC)
    result = ApplyResult(backup_id=resume)
    holder: list[_Journal] = []
    try:
        _check_profiles(proposal, profiles)
        if Path(proposal.shared_root) != Path(layout.shared_root):
            raise ApplyError("the proposal was made for another shared root")
        layout.check(profiles)
        check_outside(
            layout.shared_root,
            "the shared root",
            [(f"the skills dir of profile {p.name}", p.skills_dir.resolve()) for p in profiles],
        )
        Path(layout.shared_root).mkdir(parents=True, exist_ok=True)
        repo = GitRepo(layout.shared_root)
        with repo.locked():
            run = _Run(proposal, profiles, layout, writer, now, home, repo, result, holder)
            if resume is None:
                run.start(md5_dir_hash, hub_hash)
            else:
                run.resume(resume)
            run.phases()
            journal = holder[0]
            after = [replace(p, config=load_config(p.home / CONFIG_FILE)) for p in profiles]
            names_before = {k: set(v) for k, v in journal.data["names_before"].items()}
            overrides = _commons_config_data(layout, proposal, []).get("intentional_overrides", [])
            result.problems = verify(names_before, after, layout, md5_dir_hash, hub_hash, overrides)
            journal.data["problems"] = result.problems
            journal.data["complete"] = True
            journal.step("verify")
            result.steps_done.append("verify")
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
    """paths.rewrite WriteHook: journal each cron/markdown write ahead of it.

    A pending intent for the same file (from an interrupted run) is reused.
    """

    def __init__(self, journal: _Journal):
        self.journal = journal
        self.entry: dict[str, Any] | None = None

    def before(self, write: PlannedWrite) -> None:
        op = "cron_write" if write.kind == "cron" else "md_write"
        args: dict[str, Any] = {
            "path": str(write.path),
            "backup_copy": str(write.backup),
            "sha_before": write.sha_before,
            "sha_after": write.sha_after,
        }
        if write.kind == "cron":
            args = {"profile": write.profile, **args}
        entry = self.journal.find(op, path=str(write.path))
        if entry is not None and entry["state"] == PENDING:
            # Keep the original sha_before: backup_copy still holds that version.
            entry["args"]["sha_after"] = write.sha_after
            self.journal.save()
            self.entry = entry
        else:
            self.entry = self.journal.begin(op, args)

    def after(self, write: PlannedWrite) -> None:
        assert self.entry is not None
        self.journal.done(self.entry)
        self.entry = None


def _next_free(path: Path) -> Path:
    if not (path.exists() or path.is_symlink()):
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.name}-{n}")
        if not (cand.exists() or cand.is_symlink()):
            return cand
        n += 1


class _Run:
    """One apply run, fresh or resumed (see module docstring)."""

    def __init__(
        self,
        prop: Proposal,
        profiles: list[Profile],
        layout: Layout,
        writer: ConfigWriter,
        now: datetime,
        home: Path | None,
        repo: GitRepo,
        result: ApplyResult,
        holder: list[_Journal],
    ):
        self.prop = prop
        self.profiles = profiles
        self.by_name = {p.name: p for p in profiles}
        self.layout = layout
        self.writer = writer
        self.now = now
        self.home = home
        self.repo = repo
        self.result = result
        self.holder = holder
        self.journal: _Journal
        self.backup_dir: Path

    # ---------------------------------------------------------------- start

    def start(self, md5_dir_hash: Callable[[Path], str], hub_hash: Callable[[Path], str]) -> None:
        left = unfinished_setups(self.layout)
        if left:
            bid = left[-1]
            raise ApplyError(
                f"setup {bid} did not finish; resume it (apply with resume={bid!r}) "
                f"or restore it (restore {bid}) before running setup again"
            )
        _check_fresh(self.prop)
        names_before = resolvable_names(self.profiles, md5_dir_hash, hub_hash)
        bid = create_backup(self.layout, self.profiles, "setup", now=self.now)
        self.result.backup_id = bid
        manifest = load_manifest(self.layout, bid)
        self.backup_dir = self.layout.backup_dir(bid)
        self.journal = _Journal(
            self.layout.journal_path(bid),
            {
                "version": JOURNAL_VERSION,
                "backup_id": bid,
                "created": _stamp(self.now),
                "shared_root": str(self.layout.shared_root),
                "state_dir": str(self.layout.state_dir),
                "proposal_sha256": proposal_sha256(self.prop),
                "steps": [],
                "error": None,
                "complete": False,
                "problems": [],
                "resumed": [],
                "names_before": {k: sorted(v) for k, v in names_before.items()},
                "profiles": {
                    p.name: {
                        "home": str(p.home),
                        "config_before_sha256": manifest["profiles"][p.name]["config_sha256"],
                    }
                    for p in self.profiles
                },
                "git": {"initialized": False, "snapshot_sha": None, "base_sha": None, "final_sha": None},
                "intents": [],
            },
        )
        write_atomic(self.backup_dir / PROPOSAL_FILE, self.prop.to_json().encode("ascii"))
        self.holder.append(self.journal)
        self.done_step("backup")

    def resume(self, bid: str) -> None:
        self.backup_dir = self.layout.backup_dir(bid)
        path = self.layout.journal_path(bid)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ApplyError(f"no setup journal for backup {bid!r}; nothing to resume") from None
        if data.get("version") != JOURNAL_VERSION:
            raise ApplyError(f"backup {bid!r}: journal version {data.get('version')!r} cannot be resumed")
        if data.get("complete"):
            raise ApplyError(f"setup {bid} already finished; nothing to resume")
        if (self.backup_dir / RESTORE_LOG).exists():
            raise ApplyError(f"setup {bid} was restored; build a new proposal and run setup again")
        if Path(data["shared_root"]) != Path(self.layout.shared_root):
            raise ApplyError(f"setup {bid} was for another shared root")
        if data.get("proposal_sha256") != proposal_sha256(self.prop):
            raise ApplyError(
                f"setup {bid} was started from another proposal; resume it with the one saved in "
                f"{self.backup_dir / PROPOSAL_FILE}"
            )
        if set(data["profiles"]) != set(self.by_name):
            raise ApplyError(f"setup {bid} was for other profiles")
        self.journal = _Journal(path, data)
        self.holder.append(self.journal)
        data["resumed"].append({"at": _stamp(self.now), "previous_error": data.get("error")})
        data["error"] = None
        self.journal.save()
        self.result.steps_done = list(data["steps"])
        retired = [i["args"]["src"] for i in intents(data, "retire") if self._retire_happened(i["args"])]
        _check_fresh(self.prop, retired)

    # ---------------------------------------------------------------- helpers

    def done_step(self, name: str) -> None:
        self.journal.step(name)
        self.result.steps_done.append(name)

    def skip(self, name: str) -> bool:
        return name in self.journal.data["steps"]

    def act(
        self, op: str, key: Mapping[str, Any], args: Mapping[str, Any], action, happened
    ) -> dict[str, Any]:
        """Journal *op* ahead of *action*; on resume, settle an earlier intent with the same *key* first.

        happened(args) says whether the effect of a pending intent is already
        on disk. action(entry) does the work and returns updates for the done
        intent.
        """
        entry = self.journal.find(op, **key)
        if entry is not None and entry["state"] == DONE:
            return entry
        if entry is not None and happened(entry["args"]):
            self.journal.done(entry)
            return entry
        if entry is None:
            entry = self.journal.begin(op, {**key, **args})
        updates = action(entry) or {}
        self.journal.done(entry, **updates)
        return entry

    def commit(self, kind: str, message: str, paths: list[Path] | None, exclude: Iterable[Path] = ()):
        def happened(args):
            head = self.repo.head()
            if head is None or head == args["head_before"]:
                return False
            if self.repo.log(n=1)[0][2] != args["message"]:
                return False
            args["sha"] = head
            return True

        def action(entry):
            if paths is None:
                sha = self.repo.commit_all(message, exclude=exclude)
            else:
                sha = self.repo.commit_paths(paths, message)
            return {"sha": sha}

        entry = self.act(
            "commit", {"kind": kind}, {"message": message, "head_before": self.repo.head()}, action, happened
        )
        sha = entry["args"].get("sha")
        if sha:
            self.journal.data["git"]["final_sha"] = sha
        return sha

    def _retire_happened(self, args: Mapping[str, Any]) -> bool:
        src, dst = Path(args["src"]), Path(args["dst"])
        return not (src.exists() or src.is_symlink()) and (dst.exists() or dst.is_symlink())

    # ---------------------------------------------------------------- phases

    def phases(self) -> None:
        for name, phase in (
            ("git", self.phase_git),
            ("copy", self.phase_copy),
            ("commit", self.phase_commit),
            ("config", self.phase_config),
            ("retire", self.phase_retire),
            ("paths", self.phase_paths),
            ("commons-config", self.phase_commons_config),
        ):
            if not self.skip(name):
                phase()
                self.done_step(name)

    def phase_git(self) -> None:
        data = self.journal.data
        if not self.repo.is_repo() or self.journal.find("git_init") is not None:

            def action(entry):
                self.repo.init()
                data["git"]["initialized"] = True

            self.act(
                "git_init", {"path": str(self.layout.shared_root)}, {}, action, lambda a: self.repo.is_repo()
            )
        else:
            self.repo.init()  # idempotent: makes sure the state excludes are in place
        # A copy some setup left pending may be partial: never in the snapshot.
        exclude = [Path(t) for t in pending_copies(self.layout)]
        if self.repo.status_porcelain() or self.journal.find("commit", kind="snapshot") is not None:
            data["git"]["snapshot_sha"] = self.commit("snapshot", SNAPSHOT_MESSAGE, None, exclude)
        data["git"]["base_sha"] = self.repo.head()
        data["git"]["final_sha"] = data["git"]["base_sha"]

    def phase_copy(self) -> None:
        for move in self.prop.moves:
            if move.source_profile == SHARED:
                continue
            src = _skill_dir(self.prop, move.source_profile, move.source_rel)
            dst = Path(self.layout.shared_root) / move.target_rel
            entry = self.journal.find("copy", target=str(dst))
            if entry is not None and entry["state"] == DONE:
                continue
            identical = dst.is_dir() and not dst.is_symlink() and same_content(src, dst)
            if entry is not None:  # pending: an interrupted copy
                if identical:
                    self.journal.done(entry)
                    continue
                if dst.exists() or dst.is_symlink():
                    self._set_aside(entry, dst, self.backup_dir, move.target_rel)
            else:
                earlier = pending_copies(self.layout).get(str(dst))
                if earlier is not None and not identical and (dst.exists() or dst.is_symlink()):
                    # Left by a setup that was interrupted (and restored): not anyone's work.
                    self._set_aside(None, dst, self.layout.backup_dir(earlier), move.target_rel)
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
                    self.journal.record("copy", {**args, "skipped": True})
                    continue
                entry = self.journal.begin("copy", args)
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(src, dst)
            _fsync_dir(dst.parent)
            if not same_content(src, dst):
                raise ApplyError(f"{move.name}: the copy at {dst} does not match {src}")
            self.journal.done(entry)

    def _set_aside(self, entry: dict[str, Any] | None, dst: Path, backup_dir: Path, rel: str) -> None:
        """Move a partial copy into <backup>/partial/ (never deleted) and note it."""
        aside = _next_free(backup_dir / PARTIAL / rel)
        move_noreplace(dst, aside)
        if entry is not None:
            entry["args"].setdefault("partial", []).append(str(aside))
        else:
            self.journal.data.setdefault("partials_set_aside", []).append(str(aside))
        self.journal.save()

    def phase_commit(self) -> None:
        targets = [
            Path(i["args"]["target"]) for i in intents(self.journal.data, "copy") if not i["args"]["skipped"]
        ]
        if targets:
            self.commit("setup", f"commons setup {self.journal.data['created']}", targets)

    def phase_config(self) -> None:
        shared = str(self.layout.shared_root)
        writer = self.writer
        for p in self.profiles:
            changes = {c.key for c in self.prop.config_changes.get(p.name, [])}
            config_file = p.home / CONFIG_FILE

            def after(entry, p=p, config_file=config_file):
                return {"config_sha256_after": file_sha256(config_file)}

            if CREATE_DIR_KEY in changes:
                key = {"profile": p.name, "key": CREATE_DIR_KEY}
                args = {}
                if self.journal.find("config_set", **key) is None:
                    args = {
                        "old_value_present": has_key(load_config(config_file), CREATE_DIR_KEY),
                        "old_value": writer.get(p, CREATE_DIR_KEY),
                        "new_value": shared,
                    }

                def set_it(entry, p=p, after=after):
                    writer.set(p, CREATE_DIR_KEY, shared)
                    return after(entry)

                self.act(
                    "config_set", key, args, set_it, lambda a, p=p: writer.get(p, CREATE_DIR_KEY) == shared
                )
            if PLUGINS_KEY in changes:
                key = {"profile": p.name, "key": PLUGINS_KEY, "item": PLUGIN}

                def listed(p=p) -> bool:
                    current = writer.get(p, PLUGINS_KEY)
                    return isinstance(current, list) and PLUGIN in current

                if self.journal.find("config_list_add", **key) is None and listed():
                    self.journal.record("config_list_add", {**key, "was_present": True})
                    continue

                def add_it(entry, p=p, after=after):
                    writer.add_to_list(p, PLUGINS_KEY, PLUGIN)
                    return after(entry)

                self.act(
                    "config_list_add", key, {"was_present": False}, add_it, lambda a, listed=listed: listed()
                )

    def phase_retire(self) -> None:
        for move in self.prop.moves:
            for profile, rel in move.retire:
                path = self.by_name[profile].skills_dir / rel
                dst = retired_path(self.backup_dir, profile, rel)
                entry = self.journal.find("retire", src=str(path))
                if entry is not None and entry["state"] == DONE:
                    continue
                if entry is not None:
                    here, there = path.exists() or path.is_symlink(), dst.exists() or dst.is_symlink()
                    if there and not here:
                        self.journal.done(entry)
                        continue
                    if here and there:
                        raise ApplyError(
                            f"{move.name}: {path} and its retired copy {dst} both exist; restore"
                        )
                    if not here:
                        raise ApplyError(
                            f"{move.name}: {path} and its retired copy are both missing; restore"
                        )
                key = f"{profile}:{rel}"
                if key in move.fingerprints and fingerprint(path) != move.fingerprints[key]:
                    raise ApplyError(f"{move.name}: {key} changed during setup; not retired")
                if entry is None:
                    entry = self.journal.begin(
                        "retire",
                        {
                            "name": move.name,
                            "profile": profile,
                            "rel": rel,
                            "src": str(path),
                            "dst": str(dst),
                            "fingerprint": tree_sha256(path),
                        },
                    )
                retire(path, self.backup_dir, profile, rel)
                self.journal.done(entry)

    def phase_paths(self) -> None:
        for op in ("cron_write", "md_write"):
            for entry in intents(self.journal.data, op):
                if (
                    entry["state"] == PENDING
                    and file_sha256(Path(entry["args"]["path"])) == entry["args"]["sha_after"]
                ):
                    self.journal.done(entry)
        profiles = self.profiles
        cron_paths = {p.name: p.home / CRON_DIR / JOBS_FILE for p in profiles}
        cron_jobs = {name: load_jobs_file(path)[1] for name, path in cron_paths.items() if path.is_file()}
        findings = find_stale_paths(
            profiles, self.layout.shared_root, moved_paths(self.prop), cron_jobs, home=self.home
        )
        rewritten, skipped = rewrite(findings, self.backup_dir, cron_paths, hook=_RewriteHook(self.journal))
        self.result.rewritten = [f"{f.location}: {f.old} -> {f.new}" for f in rewritten]
        self.result.not_rewritten = [f"{f.location}: {f.old} (no file at {f.new})" for f in skipped]
        files = {Path(f.path) for f in rewritten if f.kind == "skill-file" and f.path is not None}
        files |= {Path(i["args"]["path"]) for i in intents(self.journal.data, "md_write")}
        if files or self.journal.find("commit", kind="rewrite") is not None:
            self.commit("rewrite", REWRITE_MESSAGE, sorted(files))

    def phase_commons_config(self) -> None:
        path = self.layout.config_path

        def action(entry):
            config_data = _commons_config_data(self.layout, self.prop, [p.name for p in self.profiles])
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, config_data)
            return {"sha_after": file_sha256(path)}

        self.act(
            "config_json", {"path": str(path)}, {"sha_before": file_sha256(path)}, action, lambda a: False
        )
        self.commit("config", CONFIG_MESSAGE, [path])
