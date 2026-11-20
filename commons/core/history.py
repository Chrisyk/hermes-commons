"""Per-skill history and undo in the shared root's git repo.

log() lists commits touching one skill's folder. It uses a plain path
filter (git log -- <folder>), not --follow: git supports --follow for a
single file only, never a directory. A skill moved to a new folder is
therefore followed only from its current folder on; commons records moves
as their own commits, so the move itself shows up in the log.

undo(name) with no target reverts the newest commit touching the folder,
but only when that commit touched nothing else (git diff-tree, the same
list git show --name-only prints); otherwise it refuses and asks for
--to <sha>. undo(name, to=sha) puts the folder back as it was at sha: files
added since (whole new folders as one) are moved into a backup (never deleted), then
git checkout <sha> -- <folder>, then one commit 'undo <name> to <short sha>'.

Both undo forms refuse while the folder has uncommitted changes (a checkout
or revert would overwrite them), and run through journal.operation (lock,
backup, journal). Only the shared root changes, so the backup holds no
profile; earlier versions stay in git history. Names are looked up among
the shared root's current skills: a skill whose folder is gone has no
history here. Stdlib only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from commons.core.gitrepo import GitError, GitRepo
from commons.core.journal import operation
from commons.core.layout import Layout
from commons.core.proposal import SHARED
from commons.core.scan import SkillCopy, scan_root

SET_ASIDE = "set-aside"


class HistoryError(RuntimeError):
    """Refused before anything was changed."""


@dataclass
class UndoResult:
    backup_id: str
    sha: str | None
    reverted: str | None = None  # the commit reverted (undo without to)
    set_aside: list[Path] = field(default_factory=list)  # files added since 'to', now in the backup


def _no_hash(_path: Path) -> str:
    return ""


def skill_folder(layout: Layout, name: str) -> SkillCopy:
    """The shared root's top-level copy of *name*; HistoryError when there is none or several."""
    root = Path(layout.shared_root)
    found = []
    if root.is_dir():
        found = [
            c for c in scan_root(root, SHARED, _no_hash, _no_hash) if c.parent_rel is None and c.name == name
        ]
    if not found:
        raise HistoryError(f"the shared root has no skill named {name}")
    if len(found) > 1:
        raise HistoryError(f"{name} is in the shared root twice ({', '.join(c.rel for c in found)})")
    return found[0]


def log(layout: Layout, name: str, n: int = 20) -> list[tuple[str, str, str]]:
    """Newest first (sha, committer ISO 8601 date, subject) of commits touching *name*'s folder."""
    folder = skill_folder(layout, name)
    return GitRepo(layout.shared_root).log(folder.path, n=n)


def _inside(path: str, folder: str) -> bool:
    return path == folder or PurePosixPath(path).is_relative_to(folder)


def _dirty(repo: GitRepo, rel: str) -> list[str]:
    """Paths under *rel* with uncommitted changes (git status --porcelain -z, so never quoted)."""
    return repo.status_paths(rel)


def _check_clean(repo: GitRepo, name: str, rel: str) -> None:
    if _dirty(repo, rel):
        raise HistoryError(
            f"{name} has uncommitted changes in {rel}; commit them first (commons check --fix-safe)"
        )


def _added_since(root: Path, folder: Path, before: set[str]) -> list[str]:
    """Repo-relative paths under *folder* that commit *before* lacks, as whole folders where possible.

    A folder none of whose paths existed then is listed once (moved as a
    whole), so no empty folder is left behind.
    """
    dirs_then = {str(PurePosixPath(p).parent) for p in before}
    dirs_then |= {str(parent) for p in before for parent in PurePosixPath(p).parents}
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            rel = (here / d).relative_to(root).as_posix()
            if (here / d).is_symlink():
                if rel not in before:
                    out.append(rel)
            elif rel in dirs_then:
                keep.append(d)
            else:
                out.append(rel)
        dirnames[:] = keep
        out += [r for f in filenames if (r := (here / f).relative_to(root).as_posix()) not in before]
    return sorted(out)


def undo(layout: Layout, name: str, to: str | None = None, now: datetime | None = None) -> UndoResult:
    """Undo the latest change to *name*, or put it back as it was at *to* (see module docstring).

    Every check (the folder, uncommitted changes, the commit) runs with the
    repo lock held, the same hold the change itself runs under.
    """
    repo = GitRepo(layout.shared_root)
    with repo.locked():  # the checks and the change under one hold of the lock
        folder = skill_folder(layout, name)
        _check_clean(repo, name, folder.rel)
        if to is None:
            return _undo_latest(layout, repo, name, folder, now)
        return _undo_to(layout, repo, name, folder, to, now)


def _undo_latest(layout: Layout, repo: GitRepo, name: str, folder: SkillCopy, now: datetime | None) -> UndoResult:
    rel = folder.rel
    entries = repo.log(folder.path, n=1)
    if not entries:
        raise HistoryError(f"{name} has no commits to undo")
    sha = entries[0][0]
    touched = repo.files_in_commit(sha)
    others = [p for p in touched if not _inside(p, rel)]
    if others:
        listed = ", ".join(others[:5]) + (" ..." if len(others) > 5 else "")
        raise HistoryError(
            f"the latest commit touching {name} ({sha[:7]}) also changed {listed}; "
            f"pass --to <sha> to put {name} back as it was at that commit"
        )
    with operation(layout, [], f"undo-{name}", "undo", now) as op:

        def action():
            return {"new_sha": repo.revert(sha)}

        entry = op._act("revert", {"sha": sha, "path": rel, "head_before": repo.head()}, action)
        new = entry["args"]["new_sha"]
        op.journal.data["git"]["final_sha"] = new
        op.journal.save()
    return UndoResult(op.backup_id, new, reverted=sha)


def _undo_to(
    layout: Layout, repo: GitRepo, name: str, folder: SkillCopy, to: str, now: datetime | None
) -> UndoResult:
    rel = folder.rel
    try:
        full = repo.resolve_commit(to)
    except GitError:
        raise HistoryError(f"{to} is not a commit in the shared root") from None
    before = set(repo.files_at(full, rel))
    if not before:
        raise HistoryError(f"{name} did not exist at {rel} in {full[:7]}")
    root = Path(layout.shared_root)
    now_files = _added_since(root, folder.path, before)
    with operation(layout, [], f"undo-{name}", "undo", now) as op:
        aside = []
        for p in now_files:
            dst = op.backup_dir / SET_ASIDE / p
            op.move("set_aside", name, root / p, dst)
            aside.append(dst)
        op._act(
            "checkout",
            {"sha": full, "path": rel, "head_before": repo.head()},
            lambda: repo.checkout_path(full, rel),
        )
        sha = op.commit([folder.path], f"undo {name} to {full[:7]}", kind="undo")
    return UndoResult(op.backup_id, sha, set_aside=aside)
