"""The stale-write guard: pre_tool_call / post_tool_call hooks (consistency guard warn or strict).

commons registers these two hooks only when consistency.json turns the guard
on for some profile when the plugin loads (see commons/__init__.py), so with
the default (guard off) no tool call pays for them. Turning the guard on
therefore needs a restart of the processes that should run it; turning it
off, or switching between warn and strict, takes effect on the next call,
because the hooks re-read the settings (cached by file signature).

Watched tools, files inside the shared root only:
  reads   skill_view (name, file_path), read_file (path; a read from line 1
          that covers the whole file)
  writes  skill_manage (flat or operations shape), write_file (path,
          content), patch (path, old_string, new_string, replace_all)
The rules are in commons.core.guard_rules. strict blocks a stale write with
a message that tells the agent to re-read and re-apply; warn lets it through
and appends the would-be refusal to <state dir>/guard.log. Every error inside
the guard lets the call through: the hooks never break a turn.

Not covered (documented limits): edits through terminal or execute_code,
calls that skip the pre_tool_call hook, relative paths in file tools, files
over 4 MiB, sessions with neither session_id nor task_id.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from commons import hermes_adapter as ha
from commons.core import consistency as cons
from commons.core import guard_rules as gr

log = logging.getLogger("commons.guard")

READ_TOOLS = frozenset({"skill_view", "read_file"})
WRITE_TOOLS = frozenset({"skill_manage", "write_file", "patch"})
WATCHED = READ_TOOLS | WRITE_TOOLS
FULL_WRITE_ACTIONS = frozenset({"edit", "write_file", "remove_file", "delete"})
LAYOUT_TTL = 30.0
LOCATE_TTL = 5.0
GUARD_LOG = "guard.log"
DEFAULT_READ_LIMIT = 2000
MAX_CALLS = 1024


class _State:
    def __init__(self) -> None:
        self.memory = gr.Memory()
        self.lock = threading.Lock()
        self.settings: cons.Settings | None = None
        self.settings_sig: Any = object()
        self.shared: tuple[float, Path | None] = (0.0, None)
        self.located: dict[str, tuple[float, Path | None]] = {}
        self.calls: OrderedDict[str, list[tuple[Any, ...]]] = OrderedDict()


STATE = _State()


# ------------------------------------------------------------------ cached lookups


def cached_settings() -> cons.Settings:
    """The consistency settings, re-read only when the file's signature changes."""
    return _settings()


def _settings() -> cons.Settings:
    path = cons.settings_path(ha.state_dir())
    sig = cons.file_signature(path)
    with STATE.lock:
        if sig == STATE.settings_sig and STATE.settings is not None:
            return STATE.settings
    try:
        fresh = cons.read_settings_strict(ha.state_dir())
    except (OSError, ValueError) as exc:
        log.warning("commons guard: cannot read %s (%s); keeping the last good settings", path, exc)
        return STATE.settings or cons.Settings()
    with STATE.lock:
        STATE.settings, STATE.settings_sig = fresh, sig
    return fresh


def _shared_root() -> Path | None:
    now = time.monotonic()
    with STATE.lock:
        stamp, root = STATE.shared
        if now - stamp < LAYOUT_TTL and stamp:
            return root
    root = Path(ha.layout(ha.fleet()).shared_root).resolve()
    with STATE.lock:
        STATE.shared = (now, root)
    return root


def _locate(name: str) -> Path | None:
    now = time.monotonic()
    with STATE.lock:
        hit = STATE.located.get(name)
        if hit is not None and now - hit[0] < LOCATE_TTL:
            return hit[1]
    found = ha.locate(name)
    with STATE.lock:
        if len(STATE.located) > 512:
            STATE.located.clear()
        STATE.located[name] = (now, found)
    return found


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _skill_file(name: str | None, file_path: str | None) -> Path | None:
    """The real file a skill_manage / skill_view call targets, if it lies in the shared root."""
    if not name or ":" in name:
        return None
    folder = _locate(name)
    root = _shared_root()
    if folder is None or root is None:
        return None
    folder = Path(folder)
    if folder.is_file():  # a legacy flat .md skill
        target = folder
    else:
        rel = Path(file_path) if file_path else Path("SKILL.md")
        if rel.is_absolute() or ".." in rel.parts:
            return None
        target = folder / rel
    target = target.resolve()
    return target if _inside(target, root) else None


def _plain_file(path: Any) -> Path | None:
    if not isinstance(path, str) or not path:
        return None
    p = Path(path).expanduser()
    if not p.is_absolute():
        return None
    root = _shared_root()
    if root is None:
        return None
    p = p.resolve()
    return p if _inside(p, root) else None


def _session_key(kw: dict[str, Any]) -> str | None:
    sid = str(kw.get("session_id") or "")
    if sid:
        return f"s:{sid}"
    tid = str(kw.get("task_id") or "")
    return f"t:{tid}" if tid else None


def _text(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


# ------------------------------------------------------------------ write planning


def _ops(args: dict[str, Any]) -> list[dict[str, Any]]:
    ops = args.get("operations")
    if isinstance(ops, list):
        return [o for o in ops if isinstance(o, dict)]
    return [args]


def _skill_manage_targets(args: dict[str, Any]) -> list[tuple[str, Path, dict[str, Any]]]:
    """(kind, file, op) for every op that writes an existing shared file. kind: full | patch."""
    out = []
    for op in _ops(args):
        action = op.get("action")
        name = op.get("name")
        if action == "patch" and op.get("content"):
            kind, file_path = "full", None
        elif action == "patch":
            kind, file_path = "patch", op.get("file_path")
        elif action in ("edit", "delete"):
            kind, file_path = "full", None
        elif action in ("write_file", "remove_file"):
            kind, file_path = "full", op.get("file_path")
            if not file_path:
                continue
        else:
            continue  # create: Hermes refuses an existing name itself
        target = _skill_file(name, file_path)
        if target is not None:
            out.append((kind, target, op))
    return out


def _predict(kind: str, op: dict[str, Any], text: str | None) -> str | None:
    action = op.get("action")
    if action == "delete" or action == "remove_file":
        return gr.ABSENT
    if action == "write_file":
        content = op.get("file_content", op.get("content"))
        return gr.digest_text(content) if isinstance(content, str) else None
    if kind == "full":
        content = op.get("content")
        return gr.digest_text(content) if isinstance(content, str) else None
    return gr.predicted_patch(
        text, op.get("old_string") or "", op.get("new_string") or "", bool(op.get("replace_all"))
    )


def _plain_targets(tool: str, args: dict[str, Any]) -> list[tuple[str, Path, dict[str, Any]]]:
    target = _plain_file(args.get("path"))
    if target is None:
        return []
    if tool == "write_file":
        return [("full", target, {"action": "write_file", "file_content": args.get("content")})]
    if args.get("mode") not in (None, "replace") or not args.get("old_string"):
        return [("patch", target, {"action": "patch", "old_string": ""})]
    return [("patch", target, dict(args, action="patch"))]


# ------------------------------------------------------------------ reporting


def _write_log(entry: dict[str, Any]) -> None:
    try:
        path = Path(ha.state_dir()) / GUARD_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning("commons guard: cannot write %s: %s", GUARD_LOG, exc)


def _remember_call(call_id: str, items: list[tuple[Any, ...]]) -> None:
    if not call_id or not items:
        return
    with STATE.lock:
        STATE.calls[call_id] = items
        while len(STATE.calls) > MAX_CALLS:
            STATE.calls.popitem(last=False)


def _pop_call(call_id: str) -> list[tuple[Any, ...]]:
    with STATE.lock:
        return STATE.calls.pop(call_id, []) if call_id else []


# ------------------------------------------------------------------ hooks


def _pre(tool_name: str, args: dict[str, Any], kw: dict[str, Any]) -> dict[str, Any] | None:
    profile = ha.active_profile_name()
    level = _settings().guard_for(profile)
    if level == "off":
        return None
    key = _session_key(kw)
    if key is None:
        return None
    req = str(kw.get("api_request_id") or "")
    call_id = str(kw.get("tool_call_id") or "")
    if tool_name in READ_TOOLS:
        if tool_name == "skill_view":
            target = _skill_file(args.get("name"), args.get("file_path"))
        else:
            target = _plain_file(args.get("path"))
        if target is None:
            return None
        version = gr.read_version(target)
        if version is None:
            return None
        if tool_name == "read_file":
            text = _text(version[1])
            offset = int(args.get("offset") or 1)
            limit = int(args.get("limit") or DEFAULT_READ_LIMIT)
            if offset > 1 or (text is not None and text.count("\n") + 1 > limit):
                return None  # a partial read is not a read of the file
        STATE.memory.saw((key, str(target)), version[0], req)
        _remember_call(call_id, [("read", (key, str(target)), version[0])])
        return None
    if tool_name == "skill_manage":
        targets = _skill_manage_targets(args)
    else:
        targets = _plain_targets(tool_name, args)
    if not targets:
        return None
    refusals: list[tuple[Path, str]] = []
    plans: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for kind, target, op in targets:
        version = gr.read_version(target)
        if version is None:
            continue
        disk, data = version
        text = _text(data)
        mkey = (key, str(target))
        if kind == "full":
            verdict = gr.decide_full_write(STATE.memory, mkey, disk, req)
        else:
            verdict = gr.decide_patch(STATE.memory, mkey, disk, text, op.get("old_string") or "", req)
        if not verdict.allow:
            refusals.append((target, verdict.reason))
            continue
        predicted = None if str(target) in seen else _predict(kind, op, text)
        seen.add(str(target))
        plans.append(("write", mkey, predicted))
    if refusals:
        target, reason = refusals[0]
        entry = {
            "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": level,
            "profile": profile,
            "session": key,
            "tool": tool_name,
            "file": str(target),
            "reason": reason,
        }
        _write_log(entry)
        if level == "strict":
            return {"action": "block", "message": gr.block_message(target, reason)}
        log.info("commons guard (warn): would refuse %s on %s: %s", tool_name, target, reason)
    for _, mkey, predicted in plans:
        STATE.memory.will_write(mkey, predicted)
    _remember_call(call_id, plans)
    return None


def _succeeded(tool_name: str, result: Any, status: Any) -> bool:
    if status == "error" or status == "blocked":
        return False
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except ValueError:
            return status in (None, "", "ok", "success")
        if isinstance(data, dict) and (
            data.get("error") or data.get("success") is False or data.get("staged")
        ):
            return False
    return True


def pre_tool_call(
    tool_name: str = "", args: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any] | None:
    """Record reads; refuse (strict) or log (warn) writes based on an old read. Never raises."""
    if tool_name not in WATCHED:
        return None
    try:
        return _pre(tool_name, args if isinstance(args, dict) else {}, kw)
    except Exception as exc:  # noqa: BLE001 - the guard must never break a turn
        log.warning("commons guard: %s check failed, call allowed: %s", tool_name, exc)
        return None


def post_tool_call(tool_name: str = "", result: Any = None, status: Any = None, **kw: Any) -> None:
    """Confirm or drop what pre_tool_call planned for this call. Best effort: nothing depends on it running."""
    if tool_name not in WATCHED:
        return
    try:
        items = _pop_call(str(kw.get("tool_call_id") or ""))
        if not items:
            return
        ok = _succeeded(tool_name, result, status)
        for item in items:
            if item[0] == "read":
                now = gr.read_version(Path(item[1][1]))
                if not ok or now is None or now[0] != item[2]:
                    # failed, or the file changed during the read: the session may hold either version
                    STATE.memory.forget_if(item[1], item[2])
            else:
                STATE.memory.wrote(item[1], ok)
    except Exception as exc:  # noqa: BLE001
        log.warning("commons guard: post %s failed: %s", tool_name, exc)
