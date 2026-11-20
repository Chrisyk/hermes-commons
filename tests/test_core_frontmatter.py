"""commons.core.frontmatter must parse SKILL.md frontmatter as Hermes does."""

import json
import os
import subprocess

import pytest

from commons.core.frontmatter import parse_frontmatter, read_frontmatter
from tests.test_hash_fidelity import _hermes_python, _hermes_src

_CASES = {
    "plain": "---\nname: plain\ndescription: d\n---\nbody\n",
    "folded": "---\nname: f\ndescription: >\n  folded over\n  two lines\n---\n",
    "literal": "---\nname: l\ndescription: |\n  kept\n  lines\n---\n",
    "bool-y": "---\nname: y\n---\n",
    "bool-on": "---\nname: on\n---\n",
    "number": "---\nname: 123\n---\n",
    "octal": "---\nname: 017\n---\n",
    "sexagesimal": "---\nname: 1:20\n---\n",
    "null": "---\nname: ~\n---\n",
    "duplicate": "---\nname: one\nname: two\n---\n",
    "broken": "---\nname: [unclosed\ndescription: still read\n---\n",
    "list": "---\n- a\n- b\n---\n",
    "bom": "\ufeff---\nname: bom\n---\n",
    "crlf": "---\r\nname: crlf\r\n---\r\n",
    "no-close": "---\nname: open\n",
    "comment": "---\nname: c # note\nplatforms: [macos]\n---\n",
    "nested": "---\nname: n\nmetadata:\n  hermes:\n    tags: [a, b]\n---\n",
}

_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import hermes_bootstrap  # noqa: F401
from agent.skill_utils import parse_frontmatter
cases = json.loads(sys.argv[2])
print(json.dumps({k: parse_frontmatter(v)[0] for k, v in cases.items()}, default=repr))
"""


def test_folded_description():
    assert parse_frontmatter(_CASES["folded"])["description"] == "folded over two lines"


def test_duplicate_key_takes_line_fallback():
    assert parse_frontmatter(_CASES["duplicate"]) == {"name": "two"}


def test_bare_y_is_a_yaml11_bool():
    assert parse_frontmatter(_CASES["bool-y"]) == {"name": True}


def test_read_frontmatter_strict_decoding(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_bytes(b"---\nname: x\ndescription: caf\xff\n---\n")
    assert read_frontmatter(md)["name"] == "x"
    assert read_frontmatter(md, strict=True) == {}
    assert read_frontmatter(tmp_path / "missing.md") == {}


def test_matches_real_hermes(tmp_path):
    python, src = _hermes_python(), _hermes_src()
    if python is None or src is None:
        pytest.skip("no Hermes runtime python or Hermes source checkout found")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "h")}
    proc = subprocess.run(
        [str(python), "-I", "-c", _PROBE, str(src), json.dumps(_CASES)],
        check=False,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    real = json.loads(proc.stdout.strip().splitlines()[-1])
    ours = json.loads(json.dumps({k: parse_frontmatter(v) for k, v in _CASES.items()}, default=repr))
    assert ours == real
