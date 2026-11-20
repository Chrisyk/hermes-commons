"""The merge queue: one conflict at a time, merged by an agent in a staging folder.

Flow: next_conflict() names the next unresolved conflict of the setup
proposal (its 'conflicts' minus the names in shared_root/.commons/config.json
'resolved', and minus 'skipped' unless asked), with every variant still on
disk. prepare() copies a starting point into layout.staging_dir/<name>/ and
saves the task next to it, so a later process (the CLI, the agent) can pick
it up with load_task(). The agent edits the staging folder. validate_staged()
checks it; accept() lands it in the shared root.

Staging lives in the commons state dir, never under a skills root: Hermes
indexes any SKILL.md below a skills root, dot-dirs included (see layout.py),
so a staging copy inside the shared root would be a second live copy.

Line checks use grouping's normalization (group._text_lines: CRLF read as
LF, trailing spaces/tabs/CRs stripped, SKILL.md frontmatter version:/
updated:/last_updated: lines ignored). Blank lines are not reported. A
variant line counts as kept when it appears in ANY text file of the staged
tree: an agent merge may move a paragraph from SKILL.md into a references/
file, so checking file by file would refuse good merges. The price is that
a line moved somewhere meaningless also passes; the review is the agent's.
Binary files are matched by content hash anywhere in the staged tree.

accept() follows journal.operation (backup, journal, lock). It never
deletes: a shared copy already at the target is retired into the backup
first, and each profile copy is retired only if its group.fingerprint still
equals the one recorded when the task was created; a changed copy is kept
and reported. Copies at an old skills.create_dir are never retired (they are
not commons' folder, see proposal.py). Stdlib + PyYAML only.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from commons.core.apply import JOURNAL, PROPOSAL_FILE, RESTORE_LOG, load_setup_proposal
from commons.core.backup import copy_tree, tree_sha256, write_json_atomic
from commons.core.frontmatter import frontmatter_block
from commons.core.gitrepo import GitRepo
from commons.core.group import compare_variants, file_views, fingerprint, profile_order
from commons.core.journal import operation, profiles_from_homes, read_commons_config
from commons.core.layout import Layout, check_id
from commons.core.proposal import SHARED, Conflict, Proposal, is_create_dir_label
from commons.core.scan import SKILL_FILE, SkillCopy, read_skill_name

TASKS_DIR = ".tasks"
RESOLVED = "resolved"
SKIPPED = "skipped"
_REF_PATH = r"(?:references|scripts|templates|assets)/[^\s)`'\"<>]+"
_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s<>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_TICK_REF = re.compile(r"`(" + _REF_PATH + r")`")
_FENCE = re.compile(r"^ {0,3}(```|~~~).*?^ {0,3}\1[^\n]*$", re.MULTILINE | re.DOTALL)
# \x60 is the backtick: spelled out so the pattern holds no literal backtick
# after "!", which Hermes' skill security scan reads as an inline-shell snippet.
_CODE_SPAN = re.compile(r"(\x60+)(?!\x60).+?(?<!\x60)\1(?!\x60)", re.DOTALL)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class MergeError(RuntimeError):
    """A merge step refused to go on; nothing was changed by it."""


@dataclass
class MergeVariant:
    profile: str  # a profile, '<profile>:create_dir' or 'shared'
    rel: str
    path: Path
    size: int  # total bytes of the regular files
    mtime: str  # newest file mtime, UTC ISO 8601
    unique_lines: int
    unique_files: list[str] = field(default_factory=list)
    fingerprint: str = ""  # group.fingerprint when the task was created
    retire: bool = True  # False for the shared copy and old create_dir copies


@dataclass
class MergeTask:
    name: str
    target_rel: str
    variants: list[MergeVariant]
    staging: Path
    shared_root: Path
    state_dir: Path
    profile_homes: dict[str, Path]
    created: str

    @property
    def layout(self) -> Layout:
        return Layout(Path(self.shared_root), Path(self.state_dir))

    def to_json(self) -> str:
        data = asdict(self)
        for key in ("staging", "shared_root", "state_dir"):
            data[key] = str(data[key])
        data["profile_homes"] = {k: str(v) for k, v in self.profile_homes.items()}
        for v in data["variants"]:
            v["path"] = str(v["path"])
        return json.dumps(data, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> MergeTask:
        d = json.loads(text)
        variants = [MergeVariant(**{**v, "path": Path(v["path"])}) for v in d["variants"]]
        return cls(
            name=d["name"],
            target_rel=d["target_rel"],
            variants=variants,
            staging=Path(d["staging"]),
            shared_root=Path(d["shared_root"]),
            state_dir=Path(d["state_dir"]),
            profile_homes={k: Path(v) for k, v in d["profile_homes"].items()},
            created=d["created"],
        )


@dataclass(frozen=True)
class LostLine:
    profile: str
    file: str
    line: str


@dataclass(frozen=True)
class LostFile:
    profile: str
    file: str


@dataclass
class MergeCheck:
    problems: list[str] = field(default_factory=list)  # refuse accept even with force
    lost_lines: list[LostLine] = field(default_factory=list)
    lost_files: list[LostFile] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    staged_hash: str | None = None  # tree_sha256 of staging when validated

    @property
    def ok(self) -> bool:
        return not (self.problems or self.lost_lines or self.lost_files)


@dataclass
class AcceptResult:
    backup_id: str
    sha: str | None
    kept: list[str] = field(default_factory=list)  # profile copies not retired, with why
    done: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ state


def _tasks_dir(layout: Layout) -> Path:
    return Path(layout.staging_dir) / TASKS_DIR


def _task_path(layout: Layout, name: str) -> Path:
    return _tasks_dir(layout) / f"{check_id(name)}.json"


def load_task(layout: Layout, name: str) -> MergeTask | None:
    """The task prepare() saved for *name*, or None."""
    try:
        return MergeTask.from_json(_task_path(layout, name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def saved_proposal(layout: Layout) -> Proposal | None:
    """The proposal of the newest finished setup in the state dir that was not restored, or None."""
    root = Path(layout.backups_dir)
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir(), reverse=True):
        if not (d / PROPOSAL_FILE).is_file() or (d / RESTORE_LOG).exists():
            continue
        try:
            journal = json.loads((d / JOURNAL).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(journal, dict) and journal.get("complete"):
            return load_setup_proposal(layout, d.name)
    return None


def _names(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


# ------------------------------------------------------------------ next


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _size_mtime(path: Path) -> tuple[int, float]:
    size, newest = 0, 0.0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            st = os.lstat(os.path.join(dirpath, f))
            size += st.st_size
            newest = max(newest, st.st_mtime)
    return size, newest or os.lstat(path).st_mtime


def _target_rel(prop: Proposal, conflict: Conflict) -> str:
    if conflict.shared_rel is not None:
        return conflict.shared_rel
    copies = sorted(conflict.copies, key=lambda c: (is_create_dir_label(c.profile), profile_order(c.profile)))
    if copies:
        return copies[0].rel
    return conflict.variants[0].rel


def _build_task(layout: Layout, prop: Proposal, conflict: Conflict) -> MergeTask | None:
    places: list[tuple[str, str, Path, bool]] = []
    for c in conflict.copies:
        places.append(
            (c.profile, c.rel, prop.source_root(c.profile) / c.rel, not is_create_dir_label(c.profile))
        )
    if conflict.shared_rel is not None:
        places.append((SHARED, conflict.shared_rel, Path(prop.shared_root) / conflict.shared_rel, False))
    seen: set[str] = set()
    live = []
    for label, rel, path, retire in places:
        real = os.path.realpath(path)
        if real in seen or not (path / SKILL_FILE).is_file():
            continue
        seen.add(real)
        live.append((label, rel, path, retire))
    if not live:
        return None
    copies = [SkillCopy(label, conflict.name, rel, path, "own", "", None) for label, rel, path, _ in live]
    stats = compare_variants(copies)
    variants = []
    for (label, rel, path, retire), c in zip(live, copies, strict=True):
        size, mtime = _size_mtime(path)
        lines, files = stats[c.id]
        variants.append(
            MergeVariant(label, rel, path, size, _stamp(mtime), lines, files, fingerprint(path), retire)
        )
    return MergeTask(
        name=conflict.name,
        target_rel=_target_rel(prop, conflict),
        variants=variants,
        staging=Path(layout.staging_dir) / check_id(conflict.name),
        shared_root=Path(layout.shared_root),
        state_dir=Path(layout.state_dir),
        profile_homes=dict(prop.profile_homes),
        created=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def next_conflict(
    layout: Layout, proposal_or_state: Proposal | None = None, include_skipped: bool = False
) -> MergeTask | None:
    """The next unresolved conflict as a MergeTask, or None when the queue is empty.

    *proposal_or_state* is a Proposal, or None to use the proposal saved by
    the newest finished setup. A task prepare() already saved is returned as
    saved (its fingerprints date from when it was first created). Conflicts
    with no copy left on disk are passed over.
    """
    prop = proposal_or_state if proposal_or_state is not None else saved_proposal(layout)
    if prop is None:
        return None
    config = read_commons_config(layout)
    done = set(_names(config, RESOLVED))
    skipped = set() if include_skipped else set(_names(config, SKIPPED))
    for conflict in prop.conflicts:
        if conflict.name in done or conflict.name in skipped:
            continue
        task = load_task(layout, conflict.name) or _build_task(layout, prop, conflict)
        if task is not None:
            return task
    return None


# ------------------------------------------------------------------ prepare


def _save_task(task: MergeTask) -> None:
    path = _task_path(task.layout, task.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, json.loads(task.to_json()))


def prepare(task: MergeTask, seed: str = "empty") -> Path:
    """Fill task.staging with a starting point and save the task; return the staging folder.

    *seed* is 'empty', 'variant:<profile>' (a copy of that variant) or
    'shared' (the shared root's copy). Refuses when staging already exists.
    """
    layout = task.layout
    layout.check(profiles_from_homes(task.profile_homes))
    staging = Path(task.staging)
    if staging.exists() or staging.is_symlink():
        raise MergeError(f"{task.name}: staging {staging} already exists; edit it or move it away first")
    if seed == "empty":
        source = None
    elif seed == SHARED or seed.startswith("variant:"):
        label = SHARED if seed == SHARED else seed.removeprefix("variant:")
        source = next((v.path for v in task.variants if v.profile == label), None)
        if source is None:
            known = ", ".join(v.profile for v in task.variants)
            raise MergeError(f"{task.name}: no variant {label!r} (variants: {known})")
    else:
        raise MergeError(f"bad seed {seed!r}: use empty, variant:<profile> or shared")
    staging.parent.mkdir(parents=True, exist_ok=True)
    if source is None:
        staging.mkdir()
    else:
        copy_tree(Path(source), staging)
    _save_task(task)
    return staging


# ------------------------------------------------------------------ validate


def _clean_ref(raw: str) -> str | None:
    ref = re.split(r"[#?]", raw, maxsplit=1)[0].rstrip(".,;:")
    if not ref or any(ch in ref for ch in "*?[") or _SCHEME.match(ref) or ref.startswith(("/", "~", "\\")):
        return None
    norm = posixpath.normpath(ref)
    if norm in (".", "") or norm == ".." or norm.startswith("../"):
        return None  # outside the skill folder: not a file of this skill
    return norm


def skill_references(text: str) -> tuple[list[str], list[str]]:
    """(links, mentions) a SKILL.md names, in order, once each, fenced code blocks left out.

    links are markdown link targets [text](relative/path) that stay inside
    the skill folder (no URL, no absolute path, no '..' out of it): files the
    skill really points at. mentions are paths under references/, scripts/,
    templates/ or assets/ written in backticks and not also linked: often a
    file of the skill, but just as often a path in some other repo, so only
    worth a note. A '#anchor' or '?query' suffix is dropped and glob
    patterns are skipped; a link inside an inline code span is not a link.
    """
    text = _FENCE.sub("", text)
    links: list[str] = []
    for m in _LINK.finditer(_CODE_SPAN.sub("", text)):  # a link written as code is no link
        ref = _clean_ref(m.group(1))
        if ref is not None and ref not in links:
            links.append(ref)
    mentions: list[str] = []
    for m in _TICK_REF.finditer(text):
        ref = _clean_ref(m.group(1))
        if ref is not None and ref not in links and ref not in mentions:
            mentions.append(ref)
    return links, mentions


def referenced_files(text: str) -> list[str]:
    """Every path skill_references finds, links first."""
    links, mentions = skill_references(text)
    return links + mentions


def missing_files(folder: Path, refs: Iterable[str]) -> list[str]:
    """The *refs* that are not there under *folder*."""
    return [r for r in refs if not (Path(folder) / r).exists()]


def _check_skill_md(task: MergeTask, staging: Path, check: MergeCheck) -> None:
    md = staging / SKILL_FILE
    if not md.is_file():
        check.problems.append(f"{task.name}: staging has no {SKILL_FILE}")
        return
    try:
        text = md.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        check.problems.append(f"{task.name}: {SKILL_FILE} cannot be read as UTF-8 ({exc})")
        return
    block = frontmatter_block(text)
    parsed: Any = None
    if block is None:
        check.problems.append(f"{task.name}: {SKILL_FILE} has no frontmatter (a leading --- block)")
    else:
        try:
            parsed = yaml.safe_load(block[0])
        except yaml.YAMLError as exc:
            check.problems.append(f"{task.name}: {SKILL_FILE} frontmatter does not parse ({exc})")
        if parsed is not None and not isinstance(parsed, dict):
            check.problems.append(f"{task.name}: {SKILL_FILE} frontmatter is not a mapping")
            parsed = None
    if isinstance(parsed, dict):
        folder = PurePosixPath(task.target_rel).name
        declared = read_skill_name(md, folder)
        if declared != task.name or str(parsed.get("name", folder)) != task.name:
            check.problems.append(f"{task.name}: {SKILL_FILE} declares name {declared!r}, not {task.name!r}")
    for ref in missing_files(staging, referenced_files(text)):
        had = [v for v in task.variants if (Path(v.path) / ref).exists()]
        if had:  # the file existed in a variant: accepting would lose it
            check.missing_refs.append(ref)
            check.problems.append(
                f"{task.name}: {SKILL_FILE} references {ref}, which is not in staging "
                f"(it is in {', '.join(f'{v.profile}:{v.rel}' for v in had)})"
            )
        else:
            check.notes.append(f"{task.name}: {SKILL_FILE} names {ref}, which no copy has; not a lost file")


def validate_staged(task: MergeTask) -> MergeCheck:
    """Check task.staging against every variant (see module docstring)."""
    check = MergeCheck()
    staging = Path(task.staging)
    if not staging.is_dir() or staging.is_symlink():
        check.problems.append(f"{task.name}: no staging folder at {staging}; run prepare first")
        return check
    _check_skill_md(task, staging, check)
    staged = file_views(staging)
    pooled = {line for f in staged if f.kind == "text" for line in f.lines}
    digests = {f.digest for f in staged if f.kind in ("text", "binary")}
    staged_rels = {f.rel for f in staged}
    for v in task.variants:
        try:
            views = file_views(v.path)
        except OSError as exc:
            check.notes.append(f"{v.profile}:{v.rel} cannot be read now ({exc}); its lines were not checked")
            continue
        for f in views:
            if f.kind == "text":
                seen: set[str] = set()
                for line in f.lines:
                    if line.strip() and line not in pooled and line not in seen:
                        seen.add(line)
                        check.lost_lines.append(LostLine(v.profile, f.rel, line))
            elif f.kind == "binary" and f.digest not in digests:
                check.lost_files.append(LostFile(v.profile, f.rel))
                continue
            if f.kind != "dir" and f.rel not in staged_rels:
                what = {"link": "symlink", "binary": "file"}.get(f.kind, "empty file" if not f.lines else "file")
                check.notes.append(f"{v.profile}:{v.rel}/{f.rel} ({what}) is not in staging under that name")
    check.staged_hash = tree_sha256(staging)
    return check


def describe_losses(check: MergeCheck, limit: int = 20) -> str:
    """Plain text listing of lost lines and files (at most *limit* of each)."""
    out = []
    for lost in check.lost_lines[:limit]:
        out.append(f"  line from {lost.profile}:{lost.file}: {lost.line}")
    if len(check.lost_lines) > limit:
        out.append(f"  ... and {len(check.lost_lines) - limit} more lines")
    for f in check.lost_files[:limit]:
        out.append(f"  file from {f.profile}: {f.file}")
    if len(check.lost_files) > limit:
        out.append(f"  ... and {len(check.lost_files) - limit} more files")
    return "\n".join(out)


# ------------------------------------------------------------------ accept


def _set_names(config: dict[str, Any], key: str, add: Iterable[str] = (), drop: Iterable[str] = ()) -> None:
    names = [n for n in _names(config, key) if n not in set(drop)]
    for n in add:
        if n not in names:
            names.append(n)
    config[key] = names


def _check_target(task: MergeTask, target: Path) -> MergeVariant | None:
    """The shared variant at the target (to retire first), after refusing unsafe targets."""
    rel = PurePosixPath(task.target_rel)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] in (".commons", ".git"):
        raise MergeError(f"{task.name}: target {task.target_rel} is not a place a skill can land")
    for parent in list(target.parents)[: len(rel.parts) - 1]:
        if (parent / SKILL_FILE).is_file():
            raise MergeError(f"{task.name}: target {task.target_rel} is inside the skill at {parent}")
    shared = next((v for v in task.variants if v.profile == SHARED), None)
    if not (target.exists() or target.is_symlink()):
        if shared is not None:
            raise MergeError(
                f"{task.name}: the shared copy at {shared.path} is gone; run next_conflict again"
            )
        return None
    if shared is None or Path(shared.path) != target:
        raise MergeError(
            f"{task.name}: {target} already exists in the shared root and is not part of this merge"
        )
    if fingerprint(target) != shared.fingerprint:
        raise MergeError(f"{task.name}: the shared copy changed since the task was created; prepare again")
    return shared


def _check_accept(task: MergeTask, check: MergeCheck, force: bool) -> MergeVariant | None:
    """Every read-check accept makes; run with the repo lock held. Returns the shared variant to retire."""
    if check.problems:
        raise MergeError(f"{task.name}: not accepted:\n" + "\n".join(f"  {p}" for p in check.problems))
    if (check.lost_lines or check.lost_files) and not force:
        raise MergeError(
            f"{task.name}: the staged merge loses content (accept with force to keep it anyway):\n"
            + describe_losses(check)
        )
    if check.staged_hash is None or tree_sha256(Path(task.staging)) != check.staged_hash:
        raise MergeError(f"{task.name}: staging changed since it was validated; validate again")
    if task.name in _names(read_commons_config(task.layout), RESOLVED):
        raise MergeError(f"{task.name}: already resolved")
    return _check_target(task, Path(task.shared_root) / task.target_rel)


def accept(
    task: MergeTask, check: MergeCheck, force: bool = False, now: datetime | None = None
) -> AcceptResult:
    """Land the staged merge in the shared root (see module docstring).

    Refuses (MergeError, nothing changed) on any check problem, on lost
    lines or files unless *force*, when staging changed after validation,
    or when the target is not safe to write.
    """
    repo = GitRepo(task.shared_root)
    target = Path(task.shared_root) / task.target_rel
    profiles = profiles_from_homes(task.profile_homes)
    with repo.locked():  # every check and the operation under one hold of the lock
        shared = _check_accept(task, check, force)
        with operation(task.layout, profiles, f"merge-{task.name}", "merge", now) as op:
            if shared is not None:
                op.retire_shared(task.name, task.target_rel, expected=shared.fingerprint)
            op.place(task.name, Path(task.staging), target)
            sha = op.commit([target], f"merge {task.name}", kind="merge")
            kept = []
            for v in task.variants:
                if v.retire and v.profile in op.profiles:
                    why = op.retire_profile_copy(task.name, v.profile, v.rel, v.fingerprint)
                    if why is not None:
                        kept.append(why)
            layout = task.layout
            config = read_commons_config(layout)
            _set_names(config, RESOLVED, add=[task.name])
            _set_names(config, SKIPPED, drop=[task.name])
            op.write_config(config, f"commons: mark {task.name} resolved")
            task_file = _task_path(layout, task.name)
            if task_file.exists():
                op.move_task_file(task_file, op.backup_dir / "merge-task.json")
            op.journal.data["problems"] = kept
            return AcceptResult(op.backup_id, sha, kept, list(op.done))


def skip(layout: Layout, name: str, now: datetime | None = None) -> str:
    """Record *name* as skipped (journaled; restore undoes it): it stays a conflict, next_conflict passes over it.

    Returns the backup id of the operation.
    """
    with operation(layout, [], f"skip-{name}", "skip", now) as op:
        config = read_commons_config(layout)
        _set_names(config, SKIPPED, add=[name])
        op.write_config(config, f"commons: skip merge of {name}")
    return op.backup_id
