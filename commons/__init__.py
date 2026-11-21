"""commons: one shared skill library for every Hermes profile.

register() runs whenever Hermes discovers plugins, so it imports nothing but
the standard library: the hooks, the CLI and the slash command are thin
wrappers that import commons.hooks / commons.cli (and with them PyYAML and
commons.core) on first use.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SKILL_NAME = "commons"
SKILL_PATH = Path(__file__).resolve().parent / "skills" / "commons" / "SKILL.md"
SKILL_DESCRIPTION = "Use when editing, sharing or merging skills across profiles."


MISSING_DEPS = (
    "commons cannot load its dependencies ({exc}). Hermes installs them (PyYAML, from pyproject.toml)"
    " when the plugin is installed or enabled with `hermes plugins install` / `hermes plugins enable`;"
    " for an enabled plugin run `hermes pm repair`, then restart Hermes."
)


def _missing(exc: ImportError) -> str:
    return MISSING_DEPS.format(exc=exc)


def on_skill_lifecycle(**kw) -> None:
    try:
        from commons import hooks
    except ImportError as exc:
        logging.getLogger("commons.hooks").warning(_missing(exc))
        return
    hooks.on_skill_lifecycle(**kw)


def on_session_start(**kw) -> None:
    try:
        from commons import hooks
    except ImportError as exc:
        logging.getLogger("commons.hooks").warning(_missing(exc))
        return
    hooks.on_session_start(**kw)


def pre_tool_call(**kw):
    try:
        from commons import guard
    except ImportError as exc:
        logging.getLogger("commons.guard").warning(_missing(exc))
        return None
    return guard.pre_tool_call(**kw)


def post_tool_call(**kw) -> None:
    try:
        from commons import guard
    except ImportError:
        return
    guard.post_tool_call(**kw)


def _guard_wanted() -> bool:
    """True when consistency.json turns the guard on for some profile (stdlib only; False on any doubt).

    Read once at plugin load, so the default (no file, guard off) registers no
    tool hook at all and costs nothing per tool call.
    """
    import json
    import os

    try:
        root = os.environ.get("COMMONS_HERMES_ROOT", "").strip()
        if root:
            home = Path(root).expanduser()
        else:
            from hermes_constants import get_default_hermes_root

            home = Path(get_default_hermes_root())
        data = json.loads((home / "commons" / "consistency.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no file, no Hermes, bad JSON: the guard stays off
        return False
    if not isinstance(data, dict):
        return False
    overrides = data.get("guard_overrides")
    levels = [data.get("guard"), *(overrides.values() if isinstance(overrides, dict) else [])]
    return any(level in ("warn", "strict") for level in levels)


def setup_fn(parser) -> None:
    try:
        from commons import cli
    except ImportError:
        # Keep `hermes commons ...` a command, so handler_fn can say what is wrong.
        parser.add_argument("rest", nargs=argparse.REMAINDER)
        return
    cli.setup_fn(parser)


def handler_fn(args) -> int:
    try:
        from commons import cli
    except ImportError as exc:
        sys.stderr.write(_missing(exc) + "\n")
        return 1
    return cli.handler_fn(args)


def slash_handler(raw_args: str = "", **context) -> str:
    try:
        from commons import cli
    except ImportError as exc:
        return _missing(exc)
    return cli.slash_handler(raw_args, **context)


def register(ctx) -> None:
    """Hermes plugin entry point: hooks, `hermes commons`, `/commons` and the commons skill (a Path)."""
    ctx.register_hook("on_skill_lifecycle", on_skill_lifecycle)
    ctx.register_hook("on_session_start", on_session_start)
    if _guard_wanted():
        ctx.register_hook("pre_tool_call", pre_tool_call)
        ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_cli_command(
        "commons",
        help="one shared skill library for every profile",
        setup_fn=setup_fn,
        handler_fn=handler_fn,
        description="Set up, check and maintain the skill library every Hermes profile shares.",
    )
    ctx.register_command(
        "commons",
        slash_handler,
        description="Shared skill library: status or check (read-only)",
        args_hint="status|check",
    )
    ctx.register_skill(SKILL_NAME, SKILL_PATH, description=SKILL_DESCRIPTION)
