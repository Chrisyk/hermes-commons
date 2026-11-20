"""Check a fleet after setup: nothing lost, nothing shadowing the shared root.

verify() returns plain-text problems (empty means clean):

- a name a profile could load before setup that it cannot load now
  (missing, or refused as ambiguous);
- a shared-root copy shadowed by a profile-local (or project) copy that is
  not listed as an intentional override;
- a name that is ambiguous within the shared-root tier (two different
  copies there), for every profile that reads the shared root;
- the commons state dir inside the shared root or any skills root.

Ambiguity inside a profile's own skills dir that setup left alone (an
in-profile duplicate) is not a problem here: it was there before, and
the proposal reports it. Stdlib only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from commons.core.fleet import Profile
from commons.core.layout import Layout
from commons.core.resolve import TIER_CREATE_DIR, TIER_NAMES, ScanCache, config_path, resolve

Override = str | Mapping[str, str]


def resolvable_names(
    profiles: Iterable[Profile],
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
) -> dict[str, set[str]]:
    """Per profile, every declared name skill_view would load (ambiguous names excluded)."""
    cache = ScanCache()
    return {
        p.name: {n for n, r in resolve(p, md5_dir_hash, hub_hash, cache=cache).items() if r.winner_path}
        for p in profiles
    }


def _override_keys(overrides: Iterable[Override]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for o in overrides:
        if isinstance(o, str):
            profile, sep, name = o.partition(":")
            if sep:
                keys.add((profile, name))
        elif isinstance(o, Mapping) and "profile" in o and "name" in o:
            keys.add((str(o["profile"]), str(o["name"])))
    return keys


def verify(
    names_before: Mapping[str, set[str]],
    profiles: list[Profile],
    layout: Layout,
    md5_dir_hash: Callable[[Path], str],
    hub_hash: Callable[[Path], str],
    intentional_overrides: Iterable[Override] = (),
) -> list[str]:
    """Problems with the fleet as it is now (see module docstring)."""
    problems: list[str] = []
    try:
        layout.check(profiles)
    except ValueError as exc:
        problems.append(f"commons state dir: {exc}")
    allowed = _override_keys(intentional_overrides)
    shared = config_path(Path("/"), Path(layout.shared_root))
    cache = ScanCache()
    for p in profiles:
        now = resolve(p, md5_dir_hash, hub_hash, cache=cache)
        for name in sorted(names_before.get(p.name, set())):
            r = now.get(name)
            if r is None or r.winner_path is None:
                why = "is ambiguous" if r is not None and r.is_ambiguous else "is missing"
                problems.append(f"{p.name}: {name} no longer resolves (it {why})")
        for name, r in sorted(now.items()):
            shared_shadowed = [
                c for tier, c in r.shadowed if tier == TIER_CREATE_DIR and c.path.is_relative_to(shared)
            ]
            if r.winner is not None and shared_shadowed and (p.name, name) not in allowed:
                tier = TIER_NAMES.get(r.winner_tier, str(r.winner_tier))
                problems.append(
                    f"{p.name}: {name} from the shared root is shadowed by a {tier} copy at {r.winner.path}"
                )
            if r.is_ambiguous and r.winner_tier == TIER_CREATE_DIR:
                where = ", ".join(sorted(str(c.path) for c in r.ambiguous))
                problems.append(f"{p.name}: {name} is ambiguous in the shared root ({where})")
    return problems
