"""Synthetic Hermes fleet builder for tests.

A fleet is a fake Hermes root: profile "default" lives at the root and every
other profile lives at root/profiles/<name>/. Each profile gets a config.yaml
and a skills/ dir filled from a spec dict.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import yaml

SYMLINK_PREFIX = "symlink:"
_EMPTY_CONFIG = {"skills": {}, "plugins": {"enabled": []}}


def skill_md(name: str, description: str, lines: list[str]) -> str:
    """Return SKILL.md text with frontmatter and one body line per entry."""
    body = "".join(f"{line}\n" for line in lines)
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def _tree_files(directory: Path) -> list[tuple[str, Path]]:
    files = []
    for path in directory.rglob("*"):
        rel = path.relative_to(directory)
        if "__pycache__" in rel.parts or path.suffix == ".pyc" or not path.is_file():
            continue
        files.append((rel.as_posix(), path))
    return sorted(files)


def _tree_hash(directory: Path, algo: str) -> str:
    digest = hashlib.new(algo)
    for rel, path in _tree_files(directory):
        digest.update(rel.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def dir_hash_md5(directory: Path) -> str:
    """Local copy of Hermes' bundled skill dir hash (MD5 over sorted relpath + bytes)."""
    return _tree_hash(Path(directory), "md5")


def dir_hash_sha256(directory: Path) -> str:
    """Same walk as dir_hash_md5 but SHA-256, used for hub lock content hashes."""
    return _tree_hash(Path(directory), "sha256")


def write_files(directory: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)


def profile_home(root: Path, profile: str) -> Path:
    return root if profile == "default" else root / "profiles" / profile


def _make_symlink(root: Path, link: Path, target_spec: str) -> None:
    target = Path(target_spec)
    if not target.is_absolute():
        target = root / target
    if not target.exists():
        name = link.name
        write_files(target, {"SKILL.md": skill_md(name, f"Linked skill {name}.", ["linked body"])})
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)


def _write_bundled_manifest(skills: Path, origins: dict[str, dict[str, str | bytes]]) -> None:
    lines = []
    for name, files in origins.items():
        with tempfile.TemporaryDirectory() as tmp:
            write_files(Path(tmp), files)
            lines.append(f"{name}:{dir_hash_md5(Path(tmp))}")
    (skills / ".bundled_manifest").write_text("".join(f"{line}\n" for line in lines))


def _write_hub_lock(skills: Path, installs: dict[str, str]) -> None:
    installed = {
        name: {
            "install_path": rel,
            "content_hash": "sha256:" + dir_hash_sha256(skills / rel),
            "source": "test",
        }
        for name, rel in installs.items()
    }
    lock = skills / ".hub" / "lock.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"version": 1, "installed": installed}, indent=2))


def make_fleet(root: Path, spec: dict) -> Path:
    """Build a fake Hermes fleet under root from spec and return root."""
    root = Path(root)
    profiles = {k: v for k, v in spec.items() if not k.startswith("_")}
    bundled = spec.get("_bundled", {})
    hub = spec.get("_hub", {})
    for name in set(profiles) | set(bundled) | set(hub):
        home = profile_home(root, name)
        (home / "skills").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump(_EMPTY_CONFIG, sort_keys=False))
    for name, skills_spec in profiles.items():
        skills = profile_home(root, name) / "skills"
        for rel, entry in skills_spec.items():
            if isinstance(entry, str) and entry.startswith(SYMLINK_PREFIX):
                _make_symlink(root, skills / rel, entry[len(SYMLINK_PREFIX) :])
            else:
                write_files(skills / rel, entry)
    for name, origins in bundled.items():
        _write_bundled_manifest(profile_home(root, name) / "skills", origins)
    for name, installs in hub.items():
        _write_hub_lock(profile_home(root, name) / "skills", installs)
    return root
