"""Hermes hooks: commit each skill edit in the shared root, and warn about new shadows.

on_skill_lifecycle fires on the agent's own thread right after a skill tool
write (Hermes tools/skill_usage.py _emit_skill_lifecycle, kwargs action,
skill_name, provenance, task_id, session_id, use_count, reused,
reuse_after_patch; deletes do not fire it). It must never slow or break a
turn, so it only puts the work on a bounded queue served by one daemon
thread and returns. The thread locates the skill, and when it lies inside
the shared root commits just that folder (git timeout 30 s, repo lock 10 s);
any failure is logged at warning, never raised. A full queue drops the
event with a warning: the change stays in the work tree and `commons check
--fix-safe` commits it later. At exit the queue gets a few seconds to drain.
With consistency lock shared, the hook also copies the skill's files
before it queues the commit (it runs inside Hermes' per-skill lock, which
lock shared makes common to every profile). If the folder has changed again
by the time the commit runs, or is gone, that copy is saved under
<state dir>/overwritten/ and check reports it, so no version written through
the skill tool is lost before git sees it. With lock own (the default) the
hook only stats consistency.json and queues the commit, as before.
While any commons journal is unfinished (an interrupted setup or operation,
see apply.unfinished_setups) the hook commits nothing and logs a warning,
the same rule check --fix-safe follows. Each job runs in the caller's
contextvars context (see Worker), so it sees the profile of the turn that
fired it.

on_session_start (a timeout-bounded hook in Hermes) queues a shadow check
of the active profile: local copies that hide a shared skill, minus the
intentional overrides. The answer is cached in <state dir>/cache.json (the
commons state dir, never under a skills root) under a key made of the
mtimes of the profile's config.yaml and of its skills dir and the shared
root plus their direct subfolders, so a new skill folder in a category
changes the key; only shadows not in the cached answer are logged.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import json
import logging
import os
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commons import hermes_adapter as ha
from commons.core.apply import unfinished_setups
from commons.core.backup import write_json_atomic
from commons.core.fleet import CONFIG_FILE, Profile
from commons.core.gitrepo import GitRepo
from commons.core.journal import read_commons_config
from commons.core.layout import Layout
from commons.core.resolve import TIER_CREATE_DIR, config_path, resolve

log = logging.getLogger("commons.hooks")

COMMIT_ACTIONS = frozenset({"created", "edited", "patched"})
CACHE_FILE = "cache.json"
QUEUE_SIZE = 64
OVERWRITTEN_DIR = "overwritten"
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024
EXIT_DRAIN_SECONDS = 5.0


class Worker:
    """One daemon thread draining a bounded queue of callables; errors are logged, never raised.

    Each job runs inside a copy of the submitting thread's contextvars
    context (contextvars.copy_context at submit time). Hermes keeps the
    active profile in a ContextVar (hermes_scope state, bound per turn), and
    a plain Thread does not inherit context, so without the copy the job
    would see the process default profile: the wrong locate(), commit label
    and shadow check.
    """

    def __init__(self, maxsize: int = QUEUE_SIZE) -> None:
        self._queue: queue.Queue[Callable[[], Any]] = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="commons-hooks", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except Exception as exc:  # noqa: BLE001 - a hook job must never raise
                log.warning("commons hook job failed: %s", exc)
            finally:
                self._queue.task_done()

    def submit(self, job: Callable[[], Any]) -> bool:
        self._ensure_thread()
        ctx = contextvars.copy_context()
        try:
            self._queue.put_nowait(functools.partial(ctx.run, job))
        except queue.Full:
            log.warning("commons hook queue is full; dropped one job (run `hermes commons check --fix-safe`)")
            return False
        return True

    def flush(self, timeout: float) -> bool:
        """Wait until every job queued so far has run; False on timeout."""
        done = threading.Event()
        self._ensure_thread()
        try:
            self._queue.put(done.set, timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout)


WORKER: Worker | None = Worker()


def _submit(job: Callable[[], Any]) -> None:
    worker = WORKER
    if worker is None:
        job()
    else:
        worker.submit(job)


@atexit.register
def _drain_at_exit() -> None:
    worker = WORKER
    if worker is not None and worker._thread is not None and worker._thread.is_alive():
        with contextlib.suppress(Exception):
            worker.flush(EXIT_DRAIN_SECONDS)


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


# ------------------------------------------------------------------ on_skill_lifecycle


Snapshot = dict[str, bytes]


def _read_tree(folder: Path, cap: int = SNAPSHOT_MAX_BYTES) -> Snapshot | None:
    """rel -> bytes of every file under *folder* (no .git); None when over *cap* or unreadable."""
    out: Snapshot = {}
    total = 0
    try:
        for p in sorted(folder.rglob("*")):
            if ".git" in p.relative_to(folder).parts or not p.is_file() or p.is_symlink():
                continue
            data = p.read_bytes()
            total += len(data)
            if total > cap:
                return None
            out[p.relative_to(folder).as_posix()] = data
    except OSError:
        return None
    return out


def _snapshot(name: str) -> tuple[Path, Snapshot] | None:
    """The skill's files now, when lock is shared and the skill lives in the shared root."""
    from commons import guard

    if guard.cached_settings().lock != "shared":
        return None
    folder = ha.locate(name)
    if folder is None:
        return None
    folder = Path(folder)
    if not folder.is_dir():
        return None
    layout = ha.layout(ha.fleet())
    if not _inside(folder, Path(layout.shared_root)):
        return None
    tree = _read_tree(folder)
    return (folder.resolve(), tree) if tree is not None else None


def _keep_overwritten(name: str, folder: Path, tree: Snapshot, session_id: str) -> Path | None:
    """Save *tree* under <state dir>/overwritten/ when the live folder no longer holds it."""
    live = _read_tree(folder) if folder.is_dir() else {}
    if live == tree:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = Path(ha.state_dir()) / OVERWRITTEN_DIR / f"{stamp}-{name}"
    for rel, data in tree.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (dest / ".commons-overwritten.json").write_text(
        json.dumps({"skill": name, "folder": str(folder), "session": session_id, "saved": stamp}) + "\n",
        encoding="utf-8",
    )
    log.warning(
        "commons: %s changed again before its commit; the version written by session %s is kept in %s",
        name,
        session_id or "none",
        dest,
    )
    return dest


def _commit_skill(action: str, name: str, session_id: str, snap: tuple[Path, Snapshot] | None = None) -> None:
    if snap is not None:
        _keep_overwritten(name, snap[0], snap[1], session_id)
    profiles = ha.fleet()
    layout = ha.layout(profiles)
    folder = ha.locate(name)
    if folder is None:
        log.debug("commons: %s %s: skill not found, nothing to commit", action, name)
        return
    shared = Path(layout.shared_root)
    if not _inside(folder, shared):
        return
    repo = GitRepo(shared)
    if not repo.is_repo():
        log.warning("commons: %s is not a git repo; %s %s not committed", shared, action, name)
        return
    left = unfinished_setups(layout)
    if left:
        # Same rule as check --fix-safe: half-done changes must not be committed as an edit.
        log.warning(
            "commons: operation %s did not finish; %s %s not committed (restore or resume it, then run"
            " `hermes commons check --fix-safe`)",
            left[-1],
            action,
            name,
        )
        return
    profile = ha.active_profile_name()
    message = f"{action} {name} (profile {profile}, session {session_id or 'none'})"
    sha = repo.commit_paths([folder.resolve()], message)
    if sha:
        log.info("commons: committed %s as %s", message, sha[:12])


def on_skill_lifecycle(action: str | None = None, skill_name: str | None = None, **kw: Any) -> None:
    """Queue a commit of a skill created, edited or patched inside the shared root."""
    try:
        if action not in COMMIT_ACTIONS or not skill_name:
            return
        session_id = str(kw.get("session_id") or "")
        snap = None
        try:
            snap = _snapshot(skill_name)
        except Exception as exc:  # noqa: BLE001 - a hook must never raise
            log.warning("commons: could not copy %s before its commit: %s", skill_name, exc)

        def job() -> None:
            try:
                _commit_skill(action, skill_name, session_id, snap)
            except Exception as exc:  # noqa: BLE001 - a hook must never raise
                log.warning("commons: could not commit %s %s: %s", action, skill_name, exc)

        _submit(job)
    except Exception as exc:  # noqa: BLE001 - a hook must never raise
        log.warning("commons on_skill_lifecycle failed: %s", exc)


# ------------------------------------------------------------------ on_session_start


def _mtimes(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    with contextlib.suppress(OSError):
        out["."] = os.stat(path).st_mtime_ns
        with os.scandir(path) as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    if entry.is_dir():
                        out[entry.name] = entry.stat().st_mtime_ns
    return out


def _cache_key(profile: Profile, layout: Layout) -> str:
    config_mtime = None
    with contextlib.suppress(OSError):
        config_mtime = os.stat(Path(profile.home) / CONFIG_FILE).st_mtime_ns
    key = {
        "config": config_mtime,
        "skills": _mtimes(profile.skills_dir),
        "shared": _mtimes(Path(layout.shared_root)),
        "commons_config": _mtimes(layout.commons_dir).get("."),
    }
    return json.dumps(key, sort_keys=True)


def _no_hash(_path: Path) -> str:
    return ""


def _shadowed(profile: Profile, layout: Layout) -> list[str]:
    """Names whose shared copy this profile hides with a local one, minus intentional overrides."""
    overrides = read_commons_config(layout).get("intentional_overrides", [])
    if not isinstance(overrides, list):
        overrides = []
    allowed = {o.get("name") for o in overrides if isinstance(o, dict) and o.get("profile") == profile.name}
    out = []
    for name, res in resolve(profile, _no_hash, _no_hash).items():
        if res.winner_path is None or res.winner_tier is None or res.winner_tier >= TIER_CREATE_DIR:
            continue
        if any(tier == TIER_CREATE_DIR for tier, _ in res.shadowed) and name not in allowed:
            out.append(name)
    return sorted(out)


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _shadow_check() -> None:
    profiles = ha.fleet()
    name = ha.active_profile_name()
    profile = next((p for p in profiles if p.name == name), None)
    if profile is None:
        return
    layout = ha.layout(profiles)
    create = profile.create_dir()
    if create is None or config_path(profile.home, create) != config_path(
        Path("/"), Path(layout.shared_root)
    ):
        return  # this profile does not read the shared root
    cache_path = Path(ha.state_dir()) / CACHE_FILE
    cache = _read_cache(cache_path)
    shadows = cache.get("shadows") if isinstance(cache.get("shadows"), dict) else {}
    entry = shadows.get(name) if isinstance(shadows.get(name), dict) else {}
    key = _cache_key(profile, layout)
    if entry.get("key") == key:
        return
    now = _shadowed(profile, layout)
    before = set(entry.get("shadowed") or [])
    new = [n for n in now if n not in before]
    if new:
        log.warning(
            "commons: in profile %s a local copy shadows the shared skill: %s (see `hermes commons check`)",
            name,
            ", ".join(new),
        )
    shadows[name] = {"key": key, "shadowed": now}
    cache["shadows"] = shadows
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(cache_path, cache)


def on_session_start(**kw: Any) -> None:
    """Queue the cached shadow check for the active profile."""
    try:

        def job() -> None:
            try:
                _shadow_check()
            except Exception as exc:  # noqa: BLE001 - a hook must never raise
                log.warning("commons: shadow check failed: %s", exc)

        _submit(job)
    except Exception as exc:  # noqa: BLE001 - a hook must never raise
        log.warning("commons on_session_start failed: %s", exc)
