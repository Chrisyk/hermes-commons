"""commons: one shared skill library for every Hermes profile."""

from __future__ import annotations

from pathlib import Path

SKILL_NAME = "commons"
SKILL_PATH = Path(__file__).resolve().parent / "skills" / "commons" / "SKILL.md"
SKILL_DESCRIPTION = "Use when editing, sharing or merging skills across profiles."


def register(ctx) -> None:
    """Hermes plugin entry point: hooks, `hermes commons`, `/commons` and the commons skill (a Path)."""
    from commons import cli, hooks

    ctx.register_hook("on_skill_lifecycle", hooks.on_skill_lifecycle)
    ctx.register_hook("on_session_start", hooks.on_session_start)
    ctx.register_cli_command(
        "commons",
        help="one shared skill library for every profile",
        setup_fn=cli.setup_fn,
        handler_fn=cli.handler_fn,
        description="Set up, check and maintain the skill library every Hermes profile shares.",
    )
    ctx.register_command(
        "commons",
        cli.slash_handler,
        description="Shared skill library: status or check (read-only)",
        args_hint="status|check",
    )
    ctx.register_skill(SKILL_NAME, SKILL_PATH, description=SKILL_DESCRIPTION)
