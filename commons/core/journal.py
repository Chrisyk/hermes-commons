"""The safety pattern every Phase 4 write shares with setup: backup, journal, lock, retire.

operation() is a context manager for one mutating command (merge accept,
promote, demote, undo, check --fix-safe). It runs, in order:

1  Layout.check for the profiles it was given (the state dir stays apart
   from every skills root);
2  the shared root's git lock (GitRepo.locked) for the whole operation;
3  create_backup(layout, profiles, label), so every profile config and
   skills dir is snapshotted first (an operation that only touches the
   shared root may pass no profiles: git already holds every version);
4  a write-ahead journal at backups/<id>/journal.json in the schema apply.py
   uses (version JOURNAL_VERSION), so restore.plan_restore can read it: each
   change is first written as a pending intent, then done, then flipped.

The journal reuses apply._Journal (importable here without a cycle: apply
imports nothing from this module). Intents written here, all paths absolute. Every copy or move records the
tree hash of what it moves BEFORE the action ('fingerprint'), so restore can
tell a complete copy from a half-written one; every copy into a live skills
root is written to moves.copy_staging(dst) next to the target, fsynced, then
renamed into place (moves.copy_noreplace), so a live target is never
half-written ('staging' names that folder):

  commit {kind, message, head_before}, done adds sha
  retire {name, profile, rel, src, dst, fingerprint}   restore puts it back
  retire_shared {name, rel, src, dst, fingerprint}      restore puts it back
  place {name, src, dst, staging, moved_to, fingerprint}
                                     the merge staging copied into the shared
                                     root, then moved into the backup;
                                     restore moves the placed tree aside
  shared_copy {name, src, dst, staging, fingerprint}    promote; restore moves
                                                        it aside
  profile_copy {name, profile, rel, src, dst, staging, fingerprint}
                                     demote; restore moves it aside
  set_aside {name, src, dst, fingerprint}   undo; restore puts it back
  revert {sha, path, head_before}, done adds new_sha    restore checks out
                                                        head_before
  checkout {sha, path, head_before}  restore checks out head_before
  config_json {path, sha_before, backup_copy, sha_after}  restore writes the
                                     backup copy back while the file still
                                     holds sha_after
  task_file {src, dst}               the merge task moved into the backup

The journal's 'kind' names the command. Nothing is ever deleted: a copy
that must leave a skills root is retired into the backup (moves.retire),
and a profile copy is only retired when its group.fingerprint still equals
the one the caller recorded earlier. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commons.core.apply import JOURNAL_VERSION, _Journal
from commons.core.backup import (
    create_backup,
    file_sha256,
    json_bytes,
    load_manifest,
    path_sha256,
    tree_sha256,
    write_json_atomic,
)
from commons.core.fleet import CONFIG_FILE, Profile, load_config
from commons.core.gitrepo import GitRepo
from commons.core.group import fingerprint
from commons.core.layout import Layout
from commons.core.moves import copy_noreplace, copy_staging, move_noreplace, retire, retired_path
from commons.core.paths import write_atomic

SHARED_LABEL = "shared"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

Journal = _Journal


def safe_label(text: str) -> str:
    """*text* made into a backup label (letters, digits, '.', '_', '-')."""
    out = _UNSAFE.sub("_", text).strip("._-")
    return out or "skill"


def profiles_from_homes(homes: Mapping[str, Path | str]) -> list[Profile]:
    """Profiles rebuilt from name -> home, with their config.yaml as it is now."""
    return [Profile(n, Path(h), load_config(Path(h) / CONFIG_FILE)) for n, h in homes.items()]


def read_commons_config(layout: Layout) -> dict[str, Any]:
    """shared_root/.commons/config.json, {} when missing or unreadable."""
    try:
        data = json.loads(layout.config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stamp(now: datetime) -> str:
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


@dataclass
class Operation:
    """One running operation: its backup, journal and the locked shared-root repo."""

    layout: Layout
    repo: GitRepo
    backup_id: str
    backup_dir: Path
    journal: _Journal
    profiles: dict[str, Profile]
    done: list[str] = field(default_factory=list)

    def _act(self, op: str, args: Mapping[str, Any], action) -> dict[str, Any]:
        entry = self.journal.begin(op, args)
        updates = action() or {}
        self.journal.done(entry, **updates)
        return entry

    def commit(self, paths: Sequence[Path], message: str, kind: str = "commit") -> str | None:
        """commit_paths, journaled; returns the sha (None when nothing changed)."""
        args = {"kind": kind, "message": message, "head_before": self.repo.head()}
        entry = self._act("commit", args, lambda: {"sha": self.repo.commit_paths(list(paths), message)})
        sha = entry["args"]["sha"]
        if sha:
            self.journal.data["git"]["final_sha"] = sha
            self.journal.save()
        return sha

    def commit_all(self, message: str, kind: str = "commit") -> str | None:
        args = {"kind": kind, "message": message, "head_before": self.repo.head()}
        entry = self._act("commit", args, lambda: {"sha": self.repo.commit_all(message)})
        sha = entry["args"]["sha"]
        if sha:
            self.journal.data["git"]["final_sha"] = sha
            self.journal.save()
        return sha

    def retire_profile_copy(self, name: str, profile: str, rel: str, expected: str | None) -> str | None:
        """Retire a profile's copy into the backup; return None, or why it was kept.

        *expected* is the copy's group.fingerprint when the caller first saw
        it; a copy that changed since (or vanished) is kept, never retired.
        """
        path = self.profiles[profile].skills_dir / rel
        if not _exists(path):
            return f"{profile}:{rel} is gone; nothing to retire"
        try:
            current = fingerprint(path)
        except OSError as exc:
            return f"{profile}:{rel} cannot be read ({exc}); kept"
        if expected is not None and current != expected:
            return f"{profile}:{rel} changed since the task was created; kept"
        dst = retired_path(self.backup_dir, profile, rel)
        args = {
            "name": name,
            "profile": profile,
            "rel": rel,
            "src": str(path),
            "dst": str(dst),
            "fingerprint": tree_sha256(path),
        }

        def action():
            retire(path, self.backup_dir, profile, rel)

        self._act("retire", args, action)
        self.done.append(f"{profile}: retired {rel} into backup {self.backup_id}")
        return None

    def retire_shared(self, name: str, rel: str) -> Path:
        """Move the shared root's copy at *rel* into backups/<id>/retired/shared/<rel>."""
        src = Path(self.layout.shared_root) / rel
        dst = retired_path(self.backup_dir, SHARED_LABEL, rel)
        args = {"name": name, "rel": rel, "src": str(src), "dst": str(dst), "fingerprint": tree_sha256(src)}

        def action():
            retire(src, self.backup_dir, SHARED_LABEL, rel)

        self._act("retire_shared", args, action)
        self.done.append(f"shared: retired {rel} into backup {self.backup_id}")
        return dst

    def move(self, op: str, name: str, src: Path, dst: Path) -> None:
        """move_noreplace(src, dst), journaled as *op* (set_aside) with the hash of what moves."""

        def action():
            move_noreplace(src, dst)

        self._act(op, {"name": name, "src": str(src), "dst": str(dst), "fingerprint": path_sha256(src)}, action)

    def copy_in(self, op: str, name: str, src: Path, dst: Path, **extra: Any) -> dict[str, Any]:
        """copy_noreplace(src, dst), journaled as *op* (shared_copy or profile_copy)."""
        src, dst = Path(src), Path(dst)
        args = {
            "name": name,
            **extra,
            "src": str(src),
            "dst": str(dst),
            "staging": str(copy_staging(dst)),
            "fingerprint": tree_sha256(src),
        }
        return self._act(op, args, lambda: copy_noreplace(src, dst) and None)

    def place(self, name: str, src: Path, dst: Path) -> None:
        """Land the folder *src* (merge staging) at *dst* in the shared root, journaled as 'place'.

        The tree is copied next to the target and renamed into place
        (copy_noreplace), then *src* itself is moved into the backup
        (never deleted).
        """
        src, dst = Path(src), Path(dst)
        moved_to = self.backup_dir / "staging" / src.name
        args = {
            "name": name,
            "src": str(src),
            "dst": str(dst),
            "staging": str(copy_staging(dst)),
            "moved_to": str(moved_to),
            "fingerprint": tree_sha256(src),
        }

        def action():
            copy_noreplace(src, dst)
            move_noreplace(src, moved_to)

        self._act("place", args, action)

    def write_config(self, data: dict[str, Any], message: str) -> str | None:
        """Write shared_root/.commons/config.json atomically (journaled) and commit it.

        The old file is copied into the backup first and the sha256 of the
        new bytes is journaled before the write, so restore can tell whether
        the write happened and put the old file back.
        """
        path = self.layout.config_path
        copy = _next_free(self.backup_dir / "config-json" / path.name)
        before = file_sha256(path)
        if before is not None:
            copy.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(copy, path.read_bytes())
        args = {
            "path": str(path),
            "sha_before": before,
            "backup_copy": str(copy),
            "sha_after": hashlib.sha256(json_bytes(data)).hexdigest(),
        }

        def action():
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, data)
            return {"sha_after": file_sha256(path)}

        self._act("config_json", args, action)
        return self.commit([path], message, kind="config")

    def move_task_file(self, src: Path, dst: Path) -> None:
        """Move a merge task file into the backup (journaled, restore puts it back)."""
        self._act("task_file", {"src": str(src), "dst": str(dst)}, lambda: move_noreplace(src, dst) and None)


def _next_free(path: Path) -> Path:
    if not _exists(path):
        return path
    n = 2
    while _exists(path.with_name(f"{path.name}-{n}")):
        n += 1
    return path.with_name(f"{path.name}-{n}")


@contextmanager
def operation(
    layout: Layout,
    profiles: Sequence[Profile],
    label: str,
    kind: str,
    now: datetime | None = None,
) -> Iterator[Operation]:
    """Lock, back up, journal (see module docstring); the journal is marked complete on success.

    Any exception is recorded in the journal's 'error' and re-raised; the
    journal then stays incomplete, so check reports it (unfinished-apply) and
    restore can undo what was done.
    """
    now = now or datetime.now(UTC)
    profiles = list(profiles)
    layout.check(profiles)
    Path(layout.shared_root).mkdir(parents=True, exist_ok=True)
    repo = GitRepo(layout.shared_root)
    with repo.locked():
        if not repo.is_repo():
            repo.init()
        bid = create_backup(layout, profiles, safe_label(label), now=now)
        manifest = load_manifest(layout, bid)
        head = repo.head()
        journal = _Journal(
            layout.journal_path(bid),
            {
                "version": JOURNAL_VERSION,
                "kind": kind,
                "backup_id": bid,
                "created": _stamp(now),
                "shared_root": str(layout.shared_root),
                "state_dir": str(layout.state_dir),
                "steps": [],
                "error": None,
                "complete": False,
                "problems": [],
                "resumed": [],
                "names_before": {},
                "profiles": {
                    p.name: {
                        "home": str(p.home),
                        "config_before_sha256": manifest["profiles"][p.name]["config_sha256"],
                    }
                    for p in profiles
                },
                "git": {"initialized": False, "snapshot_sha": None, "base_sha": head, "final_sha": head},
                "intents": [],
            },
        )
        journal.save()
        op = Operation(layout, repo, bid, layout.backup_dir(bid), journal, {p.name: p for p in profiles})
        try:
            yield op
        except BaseException as exc:
            journal.data["error"] = f"{type(exc).__name__}: {exc}"
            journal.save()
            raise
        journal.data["complete"] = True
        journal.step(kind)
