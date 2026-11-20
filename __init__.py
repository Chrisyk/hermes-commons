"""Hermes plugin entry for the repo root: Hermes loads <plugin dir>/__init__.py.

The code is the top-level package `commons` (commons/ beside this file), and
it imports itself as `commons.*`. Hermes imports this file as
hermes_plugins.<name>, so `commons` is importable only when Hermes' package
manager installed this project (pyproject.toml) into its environment. When it
did not (a plugin dir copied by hand, or `hermes plugins validate` without
--install-deps), commons/ is loaded from beside this file under its own
name, the way an install would provide it. No sys.path change is made.

Single module identity: Hermes gives this module __path__ = [plugin dir], so
without care `hermes_plugins.<name>.commons.core.configio` would import as a
second copy of every commons module (with its own ConfigError, its own hook
worker, ...). __path__ is emptied once commons is loaded, so commons.* is the
only name its modules have; this file needs no submodules of its own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent / "commons"


def _commons():
    try:
        import commons
    except ImportError:
        commons = None
    if commons is not None and hasattr(commons, "SKILL_PATH") and hasattr(commons, "register"):
        return commons
    if commons is not None:
        raise ImportError(f"another package named commons is installed ({commons.__file__}); remove it")
    spec = importlib.util.spec_from_file_location(
        "commons", _PACKAGE / "__init__.py", submodule_search_locations=[str(_PACKAGE)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_PACKAGE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["commons"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("commons", None)
        raise
    return module


register = _commons().register

# No submodules through the plugin package (see the module docstring).
__path__: list[str] = []

__all__ = ["register"]
