"""Two real Hermes processes (two profiles) patch one shared skill at once.

Runs Hermes' own tools/skill_manager_tool.skill_manage, so it needs a Hermes
runtime: COMMONS_HERMES_PYTHON (a python that can import Hermes' deps) and
COMMONS_HERMES_SRC (the checkout; default ~/.hermes/hermes-agent). Optional
COMMONS_HERMES_SITE adds a site-packages dir. Skipped when none is found.

Each process appends N distinct lines to the same SKILL.md, one patch per
line. With lock shared (both .locks link to one folder) every line must
survive. With each profile's own lock some lines can be lost; that half only
records whether the race showed up, because timing decides it.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path

import pytest

from commons.core import consistency as cons
from commons.core.fleet import discover
from tests.fleet import skill_md, write_files

N = 40

WORKER = r"""
import json, sys, time
src, site, start, who, n = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4], int(sys.argv[5])
sys.path[:0] = [p for p in (src, site) if p]
from tools import skill_manager_tool as s
while time.time() < start:
    time.sleep(0.001)
fails = []
for i in range(n):
    r = json.loads(s.skill_manage(action="patch", name="race", old_string="<!-- end -->",
                                  new_string=f"- {who} {i}\n<!-- end -->"))
    if not r.get("success"):
        fails.append(r.get("error"))
print(json.dumps({"fails": fails}))
"""


def _runtime():
    py = os.environ.get("COMMONS_HERMES_PYTHON")
    if not py:
        found = sorted(glob.glob(str(Path.home() / ".hermes" / "tools" / "python-*" / "bin" / "python3")))
        py = found[0] if found else None
    src = Path(os.environ.get("COMMONS_HERMES_SRC") or Path.home() / ".hermes" / "hermes-agent")
    site = os.environ.get("COMMONS_HERMES_SITE", "")
    if not py or not (src / "tools" / "skill_manager_tool.py").is_file():
        return None
    probe = subprocess.run(
        [
            py,
            "-I",
            "-c",
            f"import sys; sys.path[:0]=[{str(src)!r}, {site!r}]; import tools.skill_manager_tool",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=src,
    )
    return (py, src, site) if probe.returncode == 0 else None


RUNTIME = _runtime()
pytestmark = pytest.mark.skipif(RUNTIME is None, reason="no importable Hermes runtime")


def _fleet(tmp_path):
    root = tmp_path / "h"
    shared = tmp_path / "shared"
    for home in (root, root / "profiles" / "dev"):
        (home / "skills").mkdir(parents=True)
        (home / "config.yaml").write_text(f"skills:\n  create_dir: {shared}\n", encoding="utf-8")
    body = skill_md("race", "Race target.", ["start"]) + "<!-- end -->\n"
    write_files(shared / "general" / "race", {"SKILL.md": body})
    return root, shared


def _race(root: Path, shared: Path) -> tuple[int, list]:
    import time

    py, src, site = RUNTIME
    start = str(time.time() + 3.0)
    procs = []
    for who, home in (("a", root), ("b", root / "profiles" / "dev")):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home.parent), "HERMES_HOME": str(home)}
        procs.append(
            subprocess.Popen(
                [py, "-I", "-c", WORKER, str(src), site, start, who, str(N)],
                cwd=src,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    fails = []
    for p in procs:
        out, errtxt = p.communicate(timeout=300)
        assert p.returncode == 0, errtxt[-2000:]
        fails += json.loads(out.strip().splitlines()[-1])["fails"]
    text = (shared / "general" / "race" / "SKILL.md").read_text(encoding="utf-8")
    kept = sum(1 for who in "ab" for i in range(N) if f"- {who} {i}\n" in text)
    return kept, fails


def test_shared_lock_keeps_every_concurrent_edit(tmp_path):
    root, shared = _fleet(tmp_path)
    cons.switch_locks(discover(root), root / "commons", "shared")
    kept, fails = _race(root, shared)
    assert fails == []
    assert kept == 2 * N


def test_own_locks_race_is_observed(tmp_path, capsys):
    root, shared = _fleet(tmp_path)
    kept, fails = _race(root, shared)
    with capsys.disabled():
        print(f"\n  lock own: {kept}/{2 * N} concurrent edits kept, {len(fails)} patch errors")
    assert kept <= 2 * N
