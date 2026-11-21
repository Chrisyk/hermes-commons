"""`hermes commons ...` (CLI) and `/commons ...` (slash command: status and check only).

Hermes calls setup_fn(parser) with the `commons` subparser and handler_fn(args)
for the parsed command; an int it returns becomes the exit code. Every
command writes to the out/err streams it is given, so the slash command can
collect the text without redirecting sys.stdout under a running gateway.

Writing commands print what they will do and ask [y/N] on a TTY; --yes
answers for them; with no TTY and no --yes they exit 2 with "needs
confirmation; rerun with --yes". `check --fix-safe` is the one write that
does not ask: it only commits shared-root changes and runs from cron.
Exit codes: 0 done, 1 refused or failed, 2 needs confirmation or bad usage.

No option is called -p or --profile: Hermes scans the whole command line for
those before argparse and switches the process to that profile, so
`status --only P` filters by profile instead.

The scheduled check (setup --schedule, or the question after an interactive
apply) writes <default HERMES_HOME>/scripts/commons-check.sh and runs, for
the default profile (cron is per profile; scripts must lie under that
profile's scripts/ dir, Hermes cron/scheduler_script.py), as verified with
`hermes cron create --help` on Hermes v0.23.1:
  hermes -p default cron create "0 * * * *" --script commons-check.sh
      --name "commons hourly check" --no-agent
--no-agent runs the script with no model call and delivers its stdout
verbatim; empty stdout (check --quiet with nothing wrong) is silent.
--schedule cannot be combined with --choose, and scheduling is refused while
COMMONS_HERMES_ROOT is set (see hermes_adapter).
"""

from __future__ import annotations

import argparse
import difflib
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, TextIO

from commons import hermes_adapter as ha
from commons.core import check as check_mod
from commons.core import consistency as cons
from commons.core import history, merge
from commons.core import promote as promote_mod
from commons.core import restore as restore_mod
from commons.core.apply import apply, load_setup_proposal
from commons.core.backup import list_backups, load_manifest
from commons.core.configio import ConfigError
from commons.core.fleet import Profile
from commons.core.gitrepo import GitError, GitRepo, LockTimeout
from commons.core.layout import Layout
from commons.core.paths import CRON_DIR, JOBS_FILE, CronStoreError, load_jobs_file
from commons.core.proposal import Proposal, apply_choice, build_proposal, render_markdown
from commons.core.report import render_status, status

NEEDS_CONFIRMATION = "needs confirmation; rerun with --yes"
SLASH_ALLOWED = ("status", "check")
SCRIPT_NAME = "commons-check.sh"
CRON_SCHEDULE = "0 * * * *"
CRON_NAME = "commons hourly check"
CRON_TIMEOUT = 60.0
_ERRORS = (
    ValueError,
    RuntimeError,
    OSError,
    LockTimeout,
    ConfigError,
    GitError,
    CronStoreError,
    ha.HermesApiChanged,
)


class UsageError(Exception):
    """argparse refused the command line (slash command only; the CLI lets argparse exit)."""


class _QuietParser(argparse.ArgumentParser):
    """An ArgumentParser that raises UsageError instead of printing and exiting."""

    def error(self, message: str):  # type: ignore[override]
        raise UsageError(f"{self.format_usage()}{self.prog}: error: {message}")

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        raise UsageError(message or "")

    def print_help(self, file: TextIO | None = None) -> None:
        raise UsageError(self.format_help())


def home_dir() -> Path:
    return Path.home()


# ------------------------------------------------------------------ parser


def setup_fn(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="commons_command", metavar="COMMAND")

    p = sub.add_parser("setup", help="propose a shared skill library, then apply it on approval")
    p.add_argument("--root", type=Path, help="the shared root (default: the create_dir most profiles use)")
    p.add_argument("--choose", action="append", default=[], metavar="S=keep:P|S=merge")
    p.add_argument("--apply", action="store_true", help="carry out the saved proposal")
    p.add_argument("--resume", metavar="ID", help="finish an interrupted apply")
    p.add_argument("--schedule", action="store_true", help="create the hourly script-only check")
    p.add_argument(
        "--lock",
        choices=cons.LOCK_LEVELS,
        help="consistency lock for a first setup (default shared; an existing setting is kept)",
    )
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("config", help="show or change the consistency settings (lock, guard)")
    csub = p.add_subparsers(dest="config_command", metavar="ACTION")
    csub.add_parser("show", help="print the settings (the default)")
    q = csub.add_parser(
        "set", help="lock own|shared, guard off|warn|strict, or guard.<profile> <level>|unset"
    )
    q.add_argument("key", metavar="KEY")
    q.add_argument("value", metavar="VALUE")
    q.add_argument("--yes", action="store_true")

    p = sub.add_parser("status", help="where every skill of every profile comes from")
    p.add_argument("--only", metavar="PROFILE", help="show one profile")
    p.add_argument("--skill", metavar="S", help="show where S resolves")

    p = sub.add_parser("check", help="drift report")
    p.add_argument("--fix-safe", action="store_true", help="commit uncommitted shared-root changes")
    p.add_argument("--quiet", action="store_true", help="print only warnings")

    p = sub.add_parser("promote", help="move a profile's own skill into the shared root")
    p.add_argument("skill")
    p.add_argument("--from", dest="from_profile", metavar="P", help="default: the active profile")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("demote", help="give one profile a private copy of a shared skill")
    p.add_argument("skill")
    p.add_argument("--to", dest="to_profile", metavar="P", required=True)
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("merge", help="merge conflicting copies, one at a time")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--next", action="store_true", help="show the next conflict (the default)")
    group.add_argument("--accept", metavar="S", help="land the staged merge of S")
    group.add_argument("--skip", metavar="S", help="leave S a conflict and move on")
    p.add_argument("--seed", metavar="empty|variant:P|shared", help="start the staging folder from this")
    p.add_argument("--force", action="store_true", help="accept even though lines or files are lost")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("log", help="git history of a shared skill (or of the shared root)")
    p.add_argument("skill", nargs="?")
    p.add_argument("-n", "--limit", type=int, default=20)

    p = sub.add_parser("undo", help="revert the latest change to a skill, or roll it back")
    p.add_argument("skill")
    p.add_argument("--to", metavar="SHA", help="put the skill back as it was at this commit")
    p.add_argument("--force", action="store_true", help="revert a merge or promote commit anyway")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("restore", help="list backups, or undo the operation that made one")
    p.add_argument("backup_id", nargs="?", metavar="ID")
    p.add_argument(
        "--copy-new-to",
        action="append",
        default=[],
        metavar="S=P[,P]",
        help="also copy shared skill S, new since the backup, into these profiles",
    )
    p.add_argument(
        "--cascade",
        action="store_true",
        help="first restore every later operation not restored yet, newest first",
    )
    p.add_argument("--yes", action="store_true")


def handler_fn(args: argparse.Namespace) -> int:
    return run_command(args, sys.stdout, sys.stderr)


def run_command(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    command = getattr(args, "commons_command", None) or "status"
    sandbox = os.environ.get("COMMONS_HERMES_ROOT")
    if sandbox:
        err.write(f"commons: COMMONS_HERMES_ROOT is set; working on {sandbox}, not the real Hermes root\n")
    try:
        return COMMANDS[command](args, out, err)
    except _ERRORS as exc:
        err.write(f"commons: {exc}\n")
        return 1


# ------------------------------------------------------------------ helpers


def confirm(question: str, yes: bool, out: TextIO, err: TextIO) -> bool | None:
    """True to go on, False on no, None when there is no TTY to ask (the caller exits 2)."""
    if yes:
        return True
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        err.write(f"commons: {NEEDS_CONFIRMATION}\n")
        return None
    out.write(f"{question} [y/N] ")
    out.flush()
    answer = stdin.readline().strip().lower()
    return answer in ("y", "yes")


def _asked(question: str, args: argparse.Namespace, out: TextIO, err: TextIO) -> int | None:
    """None to go on, else the exit code to return."""
    ok = confirm(question, getattr(args, "yes", False), out, err)
    if ok is None:
        return 2
    if not ok:
        out.write("nothing changed\n")
        return 1
    return None


def _fleet() -> tuple[list[Profile], Layout]:
    profiles = ha.fleet()
    return profiles, ha.layout(profiles)


def _profile(profiles: Sequence[Profile], name: str) -> Profile:
    found = next((p for p in profiles if p.name == name), None)
    if found is None:
        known = ", ".join(p.name for p in profiles)
        raise ValueError(f"no profile named {name!r} (profiles: {known})")
    return found


def _writer():
    return ha.config_writer()


def _bundled_index():
    try:
        return ha.bundled_index()
    except ha.HermesApiChanged:
        return None  # check reports the bundled side as not checked


def _lines(out: TextIO, title: str, items: Sequence[str]) -> None:
    if items:
        out.write(f"{title}:\n")
        out.writelines(f"  {item}\n" for item in items)


# ------------------------------------------------------------------ setup


def _proposals_dir() -> Path:
    return Path(ha.state_dir()) / "proposals"


def _latest_proposal() -> Path | None:
    found = sorted(_proposals_dir().glob("proposal-*.json"))
    return found[-1] if found else None


def _save_proposal(prop: Proposal) -> Path:
    folder = _proposals_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"proposal-{stamp}.json"
    path.with_suffix(".md").write_text(render_markdown(prop), encoding="utf-8")
    path.write_text(prop.to_json(), encoding="utf-8")
    return path


def _cron_jobs(profiles: Sequence[Profile]) -> dict[str, list[dict]]:
    return {p.name: load_jobs_file(Path(p.home) / CRON_DIR / JOBS_FILE)[1] for p in profiles}


def _same_root(a: Path, b: Path) -> bool:
    return Path(a).expanduser().absolute() == Path(b).expanduser().absolute()


def cmd_setup(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    if args.resume:
        return _setup_apply(args, out, err, resume=args.resume)
    if args.apply:
        return _setup_apply(args, out, err)
    if args.schedule and args.choose:
        err.write(
            "commons: --schedule cannot be combined with --choose; make the choices first, then run"
            " `hermes commons setup --schedule`\n"
        )
        return 2
    if args.schedule:
        return _schedule(args, out, err)
    profiles = ha.fleet()
    layout = ha.layout(profiles, args.root)
    prop = None
    latest = _latest_proposal()
    if args.choose and latest is not None:
        saved = Proposal.from_json(latest.read_text(encoding="utf-8"))
        if _same_root(saved.shared_root, layout.shared_root):
            prop = saved  # --choose adds to the choices already made
    if prop is None:
        prop = build_proposal(
            profiles, layout, ha.md5_dir_hash, ha.hub_hash, _cron_jobs(profiles), home=home_dir()
        )
    for choice in args.choose:
        prop = apply_choice(prop, choice)
    path = _save_proposal(prop)
    out.write(render_markdown(prop))
    out.write(f"\nproposal saved: {path.with_suffix('.md')}\n")
    return 0


def _plan_lines(prop: Proposal) -> list[str]:
    s = prop.summary
    changes = sum(len(v) for v in prop.config_changes.values())
    return [
        f"shared root: {prop.shared_root}",
        f"back up every profile ({len(prop.profile_homes)}) into {Path(prop.state_dir) / 'backups'}",
        f"copy {len(prop.moves)} skill(s) into the shared root and commit them",
        f"move {s.retires} redundant profile copy(ies) into the backup (nothing is deleted)",
        f"{changes} config change(s) through `hermes config set`",
        f"{len(prop.cron_rewrites)} cron prompt rewrite(s)",
        f"{len(prop.conflicts)} conflict(s) stay for `hermes commons merge`",
    ]


def _setup_apply(args: argparse.Namespace, out: TextIO, err: TextIO, resume: str | None = None) -> int:
    if args.schedule:
        ha.ensure_cron_allowed()  # refuse before anything is applied, not after
    if resume is not None:
        prop = load_setup_proposal(Layout(Path("."), Path(ha.state_dir())), resume)
        what = f"resume the interrupted setup {resume}"
    else:
        latest = _latest_proposal()
        if latest is None:
            err.write("commons: no saved proposal; run `hermes commons setup` first\n")
            return 1
        prop = Proposal.from_json(latest.read_text(encoding="utf-8"))
        if args.root is not None and not _same_root(prop.shared_root, args.root):
            err.write(f"commons: the saved proposal is for {prop.shared_root}; run setup --root again\n")
            return 1
        what = f"apply {latest.with_suffix('.md')}"
    out.write(f"commons setup will {what}:\n")
    out.writelines(f"  {line}\n" for line in _plan_lines(prop))
    code = _asked("Apply it?", args, out, err)
    if code is not None:
        return code
    profiles = ha.fleet()
    result = apply(
        prop,
        profiles,
        prop.layout,
        _writer(),
        home=home_dir(),
        md5_dir_hash=ha.md5_dir_hash,
        hub_hash=ha.hub_hash,
        resume=resume,
    )
    out.write(f"backup: {result.backup_id or 'none'}\n")
    out.write(f"steps done: {', '.join(result.steps_done) or 'none'}\n")
    _lines(out, "rewritten", result.rewritten)
    _lines(out, "not rewritten (the new file is missing)", result.not_rewritten)
    if result.error is not None:
        out.write(f"error: {result.error}\n")
        if result.backup_id:
            out.write(
                f"resume with `hermes commons setup --resume {result.backup_id}`"
                f" or undo with `hermes commons restore {result.backup_id}`\n"
            )
        return 1
    if result.problems:
        _lines(out, "verify found problems", result.problems)
        out.write(f"undo it with `hermes commons restore {result.backup_id}`\n")
        return 1
    out.write("verify: no problems\n")
    if resume is None:
        _first_setup_lock(args, out, err, result.backup_id)
    return _offer_schedule(args, out, err)


def _first_setup_lock(args: argparse.Namespace, out: TextIO, err: TextIO, backup_id: str | None) -> None:
    """A first setup gets lock shared (or --lock); an install that already has settings keeps them."""
    state = Path(ha.state_dir())
    current = cons.read_settings(state)
    if current.exists:
        out.write(f"consistency: lock {current.lock}, guard {current.guard} (kept)\n")
        return
    lock = getattr(args, "lock", None) or "shared"
    try:
        res = cons.switch_locks(ha.fleet(), state, lock)
    except (cons.ConsistencyError, OSError) as exc:
        err.write(
            f"commons: lock {lock} not set ({exc}); set it later with `hermes commons config set lock {lock}`\n"
        )
        cons.write_settings(state, cons.Settings(lock="own"))
        return
    cons.write_settings(state, cons.Settings(lock=lock))
    if backup_id:
        cons.record_setup(state, backup_id, res.backup)
    out.write(f"consistency: lock {lock}, guard off (change with `hermes commons config set`)\n")
    if res.backup is not None:
        out.write(f"  the profiles' old .locks folders are in {res.backup}\n")


# ------------------------------------------------------------------ scheduled check (5.5)


def check_script_text(command: Sequence[str]) -> str:
    return f"#!/bin/sh\nexec {shlex.join(command)}\n"


def write_check_script(root: Path, command: Sequence[str]) -> Path:
    """Write <root>/scripts/commons-check.sh (mode 0755) and return its path."""
    scripts = Path(root) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / SCRIPT_NAME
    tmp = path.with_name(f".{SCRIPT_NAME}.tmp")
    tmp.write_text(check_script_text(command), encoding="utf-8")
    tmp.chmod(0o755)
    tmp.replace(path)
    return path


def cron_create_args() -> list[str]:
    """Arguments after `hermes`, for the default profile's cron (verified with `hermes cron create --help`)."""
    return [
        "-p",
        "default",
        "cron",
        "create",
        CRON_SCHEDULE,
        "--script",
        SCRIPT_NAME,
        "--name",
        CRON_NAME,
        "--no-agent",
    ]


def _scheduled(root: Path) -> bool:
    try:
        _data, jobs = load_jobs_file(Path(root) / CRON_DIR / JOBS_FILE)
    except (CronStoreError, ValueError, OSError):
        return False
    return any(j.get("name") == CRON_NAME for j in jobs)


def schedule_check(
    root: Path,
    command: Callable[[Sequence[str]], list[str]] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Write the script and create the cron job; return what happened. Raises RuntimeError on failure."""
    ha.ensure_cron_allowed()
    command = command or ha.hermes_command
    if _scheduled(root):
        return f"the {CRON_NAME!r} cron job already exists"
    script = write_check_script(root, command(["commons", "check", "--fix-safe", "--quiet"]))
    argv = command(cron_create_args())
    env = {**os.environ, "HERMES_HOME": str(root)}
    try:
        proc = runner(argv, env=env, capture_output=True, text=True, timeout=CRON_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"`{shlex.join(argv)}` timed out after {CRON_TIMEOUT:g} s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"`{shlex.join(argv)}` failed (exit {proc.returncode}): {detail}")
    return f"wrote {script} and created the {CRON_NAME!r} cron job in the default profile (see `hermes cron list`)"


def _schedule(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    ha.ensure_cron_allowed()
    root = ha.default_root()
    out.write(
        f"commons will write {root / 'scripts' / SCRIPT_NAME} and create a script-only cron job"
        f" ({CRON_SCHEDULE}, no model call) running `hermes commons check --fix-safe --quiet`\n"
    )
    code = _asked("Create it?", args, out, err)
    if code is not None:
        return code
    out.write(schedule_check(root) + "\n")
    return 0


def _offer_schedule(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    root = ha.default_root()
    if _scheduled(root):
        return 0
    if ha.root_override() is not None:
        out.write(f"hourly check not offered: {ha.ROOT_ENV} is set\n")
        return 0
    if args.schedule:
        out.write(schedule_check(root) + "\n")
        return 0
    stdin = sys.stdin
    if args.yes or stdin is None or not stdin.isatty():
        out.write("hourly check not created; add it with `hermes commons setup --schedule --yes`\n")
        return 0
    if confirm("Run `commons check` hourly as a script-only cron job (no model call)?", False, out, err):
        out.write(schedule_check(root) + "\n")
    else:
        out.write("hourly check not created\n")
    return 0


# ------------------------------------------------------------------ config (consistency)

GUARD_RESTART = (
    "the guard's hooks load when Hermes starts: restart the gateway and any open chats"
    " (`hermes gateway restart`) for {who} to start checking writes"
)


def render_settings(settings: cons.Settings) -> list[str]:
    lines = [f"lock: {settings.lock}", f"guard: {settings.guard}"]
    lines += [f"guard.{p}: {lvl}" for p, lvl in sorted(settings.guard_overrides.items())]
    if not settings.exists:
        lines.append("(no consistency.json: the defaults, the same as commons 0.1)")
    lines += [f"problem: {x}" for x in settings.problems]
    return lines


def cmd_config(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    state = Path(ha.state_dir())
    current = cons.read_settings(state)
    if getattr(args, "config_command", None) in (None, "show"):
        out.writelines(f"{line}\n" for line in render_settings(current))
        return 0
    key, value = args.key, args.value
    profiles = ha.fleet()
    if key == "lock":
        if value not in cons.LOCK_LEVELS:
            err.write(f"commons: lock must be one of {', '.join(cons.LOCK_LEVELS)}\n")
            return 2
        plan = [cons.link_state(p) for p in profiles]
        target = {p.name: cons.lock_target(state, p.name, value) for p in profiles}
        out.write(f"set lock {value}: point every profile's skills/.locks at {cons.locks_root(state)}\n")
        out.writelines(f"  {s_.profile}: {s_.path} -> {target[s_.profile]}\n" for s_ in plan)
        out.write("  real .locks folders are moved into a backup (nothing is deleted)\n")
        code = _asked("Change it?", args, out, err)
        if code is not None:
            return code
        res = cons.switch_locks(profiles, state, value)
        new = cons.Settings(value, current.guard, dict(current.guard_overrides), exists=True)
        cons.write_settings(state, new)
        out.write(f"lock is now {value} (linked: {', '.join(res.linked) or 'none'})\n")
        if res.backup is not None:
            out.write(f"the old .locks folders are in {res.backup}\n")
        out.write("it takes effect on the next skill write; a write already running may overlap once\n")
        return 0
    overrides = dict(current.guard_overrides)
    if key == "guard":
        if value not in cons.GUARD_LEVELS:
            err.write(f"commons: guard must be one of {', '.join(cons.GUARD_LEVELS)}\n")
            return 2
        new = cons.Settings(current.lock, value, overrides, exists=True)
        who = "every profile"
    elif key.startswith("guard."):
        name = key.split(".", 1)[1]
        _profile(profiles, name)
        if value == "unset":
            overrides.pop(name, None)
        elif value in cons.GUARD_LEVELS:
            overrides[name] = value
        else:
            err.write(f"commons: guard.{name} must be one of {', '.join(cons.GUARD_LEVELS)} or unset\n")
            return 2
        new = cons.Settings(current.lock, current.guard, overrides, exists=True)
        who = name
    else:
        err.write("commons: KEY is lock, guard or guard.<profile>\n")
        return 2
    out.write(f"set {key} {value} in {cons.settings_path(state)}\n")
    code = _asked("Change it?", args, out, err)
    if code is not None:
        return code
    cons.write_settings(state, new)
    out.writelines(f"{line}\n" for line in render_settings(cons.read_settings(state)))
    if new.guard_anywhere() and not current.guard_anywhere():
        out.write(GUARD_RESTART.format(who=who) + "\n")
    return 0


# ------------------------------------------------------------------ status, check


def cmd_status(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    profiles = ha.fleet()
    only = getattr(args, "only", None)
    if only is not None:
        _profile(profiles, only)
    report = status(profiles, ha.md5_dir_hash, ha.hub_hash)
    out.write(render_status(report, profile=only, skill=getattr(args, "skill", None)))
    if getattr(args, "skill", None) is None:
        out.write(_consistency_line(profiles) + "\n")
    return 0


def _consistency_line(profiles: Sequence[Profile]) -> str:
    settings = cons.read_settings(Path(ha.state_dir()))
    line = f"consistency: lock {settings.lock}, guard {settings.guard}"
    if settings.guard_overrides:
        line += " (" + ", ".join(f"{p} {v}" for p, v in sorted(settings.guard_overrides.items())) + ")"
    if settings.lock == "own":
        try:
            near = check_mod.near_collisions(ha.layout(profiles))
        except (GitError, OSError, ValueError):
            near = []
        if near:
            line += (
                f"\n  {len(near)} time(s) two profiles edited the same skill within a minute"
                " (last 30 days); `hermes commons config set lock shared` makes such writes wait"
                " for each other"
            )
    return line


def cmd_check(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    profiles, layout = _fleet()

    def findings():
        return check_mod.run_check(
            profiles, layout, ha.md5_dir_hash, ha.hub_hash, bundled_index=_bundled_index(), home=home_dir()
        )

    found = findings()
    if getattr(args, "fix_safe", False):
        fixed = check_mod.fix_safe(found, layout)
        if isinstance(fixed, str):
            if not args.quiet:
                out.write(f"committed the uncommitted shared-root changes as {fixed[:12]}\n")
            found = findings()
        elif fixed is not None:
            found = [fixed, *found]
    out.write(check_mod.render_findings(found, quiet=getattr(args, "quiet", False)))
    return 0


# ------------------------------------------------------------------ promote, demote


def cmd_promote(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    profiles, layout = _fleet()
    source = args.from_profile or ha.active_profile_name()
    _profile(profiles, source)
    out.write(
        f"promote {args.skill}: copy {source}'s copy into {layout.shared_root}, commit it, and move"
        f" {source}'s copy into a backup\n"
    )
    code = _asked("Promote it?", args, out, err)
    if code is not None:
        return code
    res = promote_mod.promote(args.skill, source, layout, profiles, ha.md5_dir_hash, ha.hub_hash)
    out.write(
        f"promoted {args.skill} to {res.target} (commit {(res.sha or 'none')[:12]}, backup {res.backup_id})\n"
    )
    _lines(out, "still loading their own copy (see `hermes commons check`)", res.others)
    _lines(out, "kept (changed since the check)", res.kept)
    return 0


def cmd_demote(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    profiles, layout = _fleet()
    _profile(profiles, args.to_profile)
    out.write(
        f"demote {args.skill}: copy the shared {args.skill} into {args.to_profile}'s own skills and"
        " record it as an intentional override\n"
    )
    code = _asked("Demote it?", args, out, err)
    if code is not None:
        return code
    res = promote_mod.demote(args.skill, args.to_profile, layout, profiles)
    out.write(f"{args.to_profile} now has its own {args.skill} at {res.path} (backup {res.backup_id})\n")
    return 0


# ------------------------------------------------------------------ merge


def _describe_task(task: merge.MergeTask, out: TextIO) -> None:
    out.write(f"conflict: {task.name}\n")
    out.write(f"target: {Path(task.shared_root) / task.target_rel}\n")
    out.write(f"staging: {task.staging}\n")
    out.write("variants:\n")
    for v in task.variants:
        out.write(f"  {v.profile}: {v.path} ({v.size} bytes, {v.mtime}, {v.unique_lines} unique line(s))\n")
        out.writelines(f"    only here: {f}\n" for f in v.unique_files)


def _diff_lines(task: merge.MergeTask, limit: int = 60) -> list[str]:
    staged = Path(task.staging) / "SKILL.md"
    try:
        new = staged.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    lines: list[str] = []
    for v in task.variants:
        try:
            old = (Path(v.path) / "SKILL.md").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        diff = list(
            difflib.unified_diff(old, new, f"{v.profile}/SKILL.md", "staged/SKILL.md", lineterm="", n=1)
        )
        if len(diff) > limit:
            diff = [*diff[:limit], f"... ({len(diff) - limit} more diff lines)"]
        lines += diff
    return lines


def cmd_merge(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    _profiles, layout = _fleet()
    if args.skip:
        out.write(f"skip {args.skip}: it stays a conflict; `merge --next` passes over it\n")
        code = _asked("Skip it?", args, out, err)
        if code is not None:
            return code
        bid = merge.skip(layout, args.skip)
        out.write(f"skipped {args.skip} (backup {bid})\n")
        return 0
    if args.accept:
        return _merge_accept(args, layout, out, err)
    task = merge.next_conflict(layout)
    if task is None:
        out.write("no conflicts left to merge\n")
        return 0
    if Path(task.staging).exists():
        if args.seed:
            out.write(f"staging already exists; --seed {args.seed} ignored\n")
    else:
        merge.prepare(task, args.seed or "empty")
    _describe_task(task, out)
    out.write(
        f"\nWrite the merged skill into {task.staging}, keeping every variant's unique lines, then run"
        f" `hermes commons merge --accept {task.name}`.\n"
    )
    return 0


def _merge_accept(args: argparse.Namespace, layout: Layout, out: TextIO, err: TextIO) -> int:
    task = merge.load_task(layout, args.accept)
    if task is None:
        err.write(f"commons: no merge task for {args.accept}; run `hermes commons merge --next` first\n")
        return 1
    result = merge.validate_staged(task)
    _lines(out, "problems", result.problems)
    _lines(out, "missing referenced files", result.missing_refs)
    _lines(out, "notes", result.notes)
    lost = merge.describe_losses(result)
    if lost.strip():
        out.write(lost if lost.endswith("\n") else lost + "\n")
    if result.problems:
        out.write("not accepted: fix the problems above\n")
        return 1
    if (result.lost_lines or result.lost_files) and not args.force:
        out.write("not accepted: lines or files above are lost; add them, or pass --force\n")
        return 1
    out.writelines(line + "\n" for line in _diff_lines(task))
    out.write(
        f"accept {task.name}: move the staged copy to {Path(task.shared_root) / task.target_rel}, commit it,"
    )
    out.write(" and move the profile copies into a backup\n")
    code = _asked("Accept it?", args, out, err)
    if code is not None:
        return code
    res = merge.accept(task, result, force=args.force)
    out.write(f"merged {task.name} (commit {(res.sha or 'none')[:12]}, backup {res.backup_id})\n")
    _lines(out, "kept (changed since the task was made)", res.kept)
    return 0


# ------------------------------------------------------------------ log, undo


def cmd_log(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    _profiles, layout = _fleet()
    if args.skill:
        rows = history.log(layout, args.skill, n=args.limit)
    else:
        rows = GitRepo(layout.shared_root).log(None, n=args.limit)
    out.writelines(f"{sha[:12]}  {date}  {subject}\n" for sha, date, subject in rows)
    if not rows:
        out.write("no commits\n")
    return 0


def cmd_undo(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    _profiles, layout = _fleet()
    rows = history.log(layout, args.skill, n=1)
    if args.to:
        out.write(f"undo {args.skill}: put it back as it was at {args.to}\n")
    elif rows:
        out.write(f"undo {args.skill}: revert {rows[0][0][:12]} {rows[0][2]}\n")
    else:
        err.write(f"commons: {args.skill} has no commits in the shared root; nothing to undo\n")
        return 1
    code = _asked("Undo it?", args, out, err)
    if code is not None:
        return code
    res = history.undo(layout, args.skill, to=args.to, force=args.force)
    out.write(f"undone (commit {(res.sha or 'none')[:12]}, backup {res.backup_id})\n")
    _lines(out, "set aside into the backup", [str(p) for p in res.set_aside])
    return 0


# ------------------------------------------------------------------ restore


def _backup_rows(layout: Layout) -> list[str]:
    rows = []
    for bid in list_backups(layout):
        try:
            manifest = load_manifest(layout, bid)
        except (OSError, ValueError):
            manifest = {}
        label = manifest.get("label", "?")
        state = ""
        backup = layout.backup_dir(bid)
        if (backup / restore_mod.RESTORE_LOG).exists():
            state = " (restored)"
        elif not (backup / "journal.json").exists():
            state = " (no journal: a pre-restore copy)"
        rows.append(f"{bid}  {label}  {manifest.get('created', '')}{state}")
    return rows


def _copy_new_to(values: Sequence[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for value in values:
        name, sep, who = value.partition("=")
        targets = [w.strip() for w in who.split(",") if w.strip()]
        if not sep or not name.strip() or not targets:
            raise ValueError(f"bad --copy-new-to {value!r}: use S=P[,P]")
        out.setdefault(name.strip(), []).extend(targets)
    return out


def cmd_restore(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    profiles, layout = _fleet()
    if not args.backup_id:
        rows = _backup_rows(layout)
        if not rows:
            out.write(f"no backups in {layout.backups_dir}\n")
            return 0
        out.write("backups (oldest first):\n")
        out.writelines(f"  {row}\n" for row in rows)
        out.write("undo one with `hermes commons restore ID`\n")
        return 0
    copy_to = _copy_new_to(args.copy_new_to)
    plan = restore_mod.plan_restore(layout, args.backup_id)
    cascade = getattr(args, "cascade", False)
    if plan.later and not cascade:
        raise restore_mod.RestoreError(restore_mod.later_operations_message(plan.backup_id, plan.later))
    if plan.later:
        out.write(f"first restore {len(plan.later)} later operation(s), newest first:\n")
        out.writelines(f"  {bid}\n" for bid in plan.later)
    out.write(f"restore {plan.backup_id} ({plan.kind or 'setup'}):\n")
    out.write(f"  config.yaml of {len(plan.configs)} profile(s) put back\n")
    out.write(f"  {len(plan.retired)} retired copy(ies) moved back\n")
    out.write(f"  {len(plan.cron)} cron store(s) put back\n")
    _lines(
        out,
        "shared skills new or changed since (kept; --copy-new-to S=P to give a profile a copy)",
        [f"{c.name} ({c.status})" for c in plan.shared_changes],
    )
    _lines(out, "cannot be decided (reported, not touched)", plan.undecided)
    code = _asked("Restore it?", args, out, err)
    if code is not None:
        return code
    if cascade:
        steps = restore_mod.restore_cascade(
            layout, plan.backup_id, profiles, _writer(), copy_new_to=copy_to or None
        ).restored
    else:
        steps = [
            (plan.backup_id, restore_mod.restore(plan, profiles, _writer(), copy_new_to=copy_to or None))
        ]
    record = cons.setup_record(Path(layout.state_dir)) or {}
    for bid, res in steps:
        out.write(f"restored {bid} (a pre-restore backup is {res.pre_restore_backup_id})\n")
        _lines(out, "done", res.done)
        _lines(out, "reports", res.reports)
        if bid == record.get("setup"):
            back = cons.undo_setup_lock(profiles, Path(layout.state_dir))
            out.write(
                f"consistency: each profile's own skills/.locks put back ({', '.join(back) or 'none'})\n"
            )
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, TextIO, TextIO], int]] = {
    "setup": cmd_setup,
    "config": cmd_config,
    "status": cmd_status,
    "check": cmd_check,
    "promote": cmd_promote,
    "demote": cmd_demote,
    "merge": cmd_merge,
    "log": cmd_log,
    "undo": cmd_undo,
    "restore": cmd_restore,
}


# ------------------------------------------------------------------ slash command

_REFUSED = (
    "commons: only /commons status and /commons check (without --fix-safe) work here;"
    " run the others in a terminal as `hermes commons ...`"
)


def slash_handler(raw_args: str = "", **_context: Any) -> str:
    """`/commons [status|check] ...`: read-only, so it never asks and never writes."""
    try:
        tokens = shlex.split(raw_args or "")
    except ValueError as exc:
        return f"commons: {exc}"
    if not tokens:
        tokens = ["status"]
    if tokens[0] not in SLASH_ALLOWED or (tokens[0] == "check" and "--fix-safe" in tokens):
        return _REFUSED
    parser = _QuietParser(prog="/commons")
    setup_fn(parser)
    try:
        args = parser.parse_args(tokens)
    except UsageError as exc:
        return str(exc)
    if getattr(args, "fix_safe", False):
        return _REFUSED
    out, err = StringIO(), StringIO()
    run_command(args, out, err)
    return (out.getvalue() + err.getvalue()).rstrip("\n") or "commons: no output"
