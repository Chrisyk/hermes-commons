"""Phase 3 gate: scan, propose, apply, verify and restore the classic fleet in one pass."""

from __future__ import annotations

from datetime import UTC, datetime

from commons.core.apply import apply
from commons.core.backup import tree_sha256
from commons.core.configio import YamlFileWriter
from commons.core.fleet import discover
from commons.core.group import group
from commons.core.layout import Layout
from commons.core.proposal import Proposal, build_proposal, render_markdown
from commons.core.resolve import TIER_CREATE_DIR, resolve
from commons.core.restore import plan_restore, restore
from commons.core.scan import scan_profile
from commons.core.verify import resolvable_names, verify
from tests.conftest import PROFILES
from tests.fleet import dir_hash_md5, hub_content_hash, profile_home


def _state(root):
    return {
        p: (
            tree_sha256(profile_home(root, p) / "skills"),
            (profile_home(root, p) / "config.yaml").read_bytes(),
        )
        for p in PROFILES
    }


def test_setup_end_to_end(classic_fleet):
    root = classic_fleet
    layout = Layout(shared_root=root / "shared", state_dir=root / "commons")
    original = _state(root)
    profiles = discover(root)

    # scan
    copies = [c for p in profiles for c in scan_profile(p, dir_hash_md5, hub_content_hash)]
    statuses = {g.name: g.status for g in group(copies)}
    assert statuses["same"] == "identical" and statuses["contained"] == "contained"
    names_before = resolvable_names(profiles, dir_hash_md5, hub_content_hash)

    # propose (and survive a save and load, as the CLI does between review and apply)
    prop = build_proposal(profiles, layout, dir_hash_md5, hub_content_hash, {}, home=root)
    prop = Proposal.from_json(prop.to_json())
    assert {m.name for m in prop.moves} == {"same", "contained"}
    assert "## What next" in render_markdown(prop)

    # apply
    result = apply(
        prop, profiles, layout, YamlFileWriter(), datetime(2026, 11, 20, 12, tzinfo=UTC), home=root
    )
    assert result.error is None, result.error
    profiles = discover(root)
    for p in profiles:
        res = resolve(p, dir_hash_md5, hub_content_hash)
        assert res["same"].winner_tier == TIER_CREATE_DIR
        assert res["contained"].winner_tier == TIER_CREATE_DIR

    # verify
    assert verify(names_before, profiles, layout, dir_hash_md5, hub_content_hash) == []

    # restore
    plan = plan_restore(layout, result.backup_id)
    out = restore(plan, profiles, YamlFileWriter(), now=datetime(2026, 11, 20, 13, tzinfo=UTC))
    assert out.reports == []
    assert _state(root) == original
    assert (layout.shared_root / "general" / "same" / "SKILL.md").is_file()
