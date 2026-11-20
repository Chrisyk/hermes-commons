"""Synthetic Hermes fleet builder for tests.

A fleet is a fake Hermes root: profile "default" lives at the root and every
other profile lives at root/profiles/<name>/. Each profile gets a config.yaml
and a skills/ dir filled from a spec dict.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import suppress
from pathlib import Path

import yaml

SYMLINK_PREFIX = "symlink:"
_EMPTY_CONFIG = {"skills": {}, "plugins": {"enabled": []}}


def skill_md(name: str, description: str, lines: list[str]) -> str:
    """Return SKILL.md text with frontmatter and one body line per entry."""
    body = "".join(f"{line}\n" for line in lines)
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


_RUNTIME_CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def _is_runtime_cache(path: Path, skill_dir: Path) -> bool:
    """Mirror of Hermes tools/skills_sync_optional.py _is_runtime_cache."""
    relative = path.relative_to(skill_dir)
    if any(part in _RUNTIME_CACHE_DIRS for part in relative.parts[:-1]):
        return True
    if path.name in _RUNTIME_CACHE_DIRS and path.is_dir():
        return True
    return path.suffix in {".pyc", ".pyo"} and path.with_suffix(".py").is_file()


def dir_hash_md5(directory: Path) -> str:
    """Mirror of Hermes tools/skills_sync.py dir_hash (the .bundled_manifest hash).

    MD5 over relpath str + bytes in Path sort order, skipping runtime caches.
    """
    directory = Path(directory)
    hasher = hashlib.md5()
    with suppress(OSError):
        for fpath in sorted(directory.rglob("*")):
            if not _is_runtime_cache(fpath, directory) and fpath.is_file():
                hasher.update(str(fpath.relative_to(directory)).encode("utf-8"))
                hasher.update(fpath.read_bytes())
    return hasher.hexdigest()


def _content_digest(skill_path: Path) -> str:
    """Mirror of Hermes tools/skills_guard.py _content_digest for a skill directory."""
    h = hashlib.sha256()
    for rel, p in sorted(
        (p.relative_to(skill_path).as_posix(), p) for p in skill_path.rglob("*") if p.is_file()
    ):
        h.update(rel.encode("utf-8") + b"\x00")
        h.update(p.read_bytes())
    return h.hexdigest()


def hub_content_hash(path: Path) -> str:
    """Mirror of Hermes tools/skills_guard.py content_hash (the .hub/lock.json content_hash)."""
    return f"sha256:{_content_digest(Path(path))[:16]}"


def write_files(directory: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")


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


def _write_bundled_manifest(
    root: Path, profile: str, skills: Path, origins: dict[str, dict[str, str | bytes]]
) -> None:
    lines = []
    for name, files in origins.items():
        origin = root / "_origin" / profile / name
        if origin.exists():
            shutil.rmtree(origin)
        write_files(origin, files)
        lines.append(f"{name}:{dir_hash_md5(origin)}")
        shutil.rmtree(origin)
    (skills / ".bundled_manifest").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _write_hub_lock(skills: Path, installs: dict[str, str]) -> None:
    installed = {
        name: {
            "install_path": rel,
            "content_hash": hub_content_hash(skills / rel),
            "source": "test",
        }
        for name, rel in installs.items()
    }
    lock = skills / ".hub" / "lock.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"version": 1, "installed": installed}, indent=2), encoding="utf-8")


def make_fleet(root: Path, spec: dict) -> Path:
    """Build a fake Hermes fleet under root from spec and return root."""
    root = Path(root)
    profiles = {k: v for k, v in spec.items() if not k.startswith("_")}
    bundled = spec.get("_bundled", {})
    hub = spec.get("_hub", {})
    for name in set(profiles) | set(bundled) | set(hub):
        home = profile_home(root, name)
        (home / "skills").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump(_EMPTY_CONFIG, sort_keys=False), encoding="utf-8")
    for name, skills_spec in profiles.items():
        skills = profile_home(root, name) / "skills"
        for rel, entry in skills_spec.items():
            if isinstance(entry, str):
                if not entry.startswith(SYMLINK_PREFIX):
                    raise ValueError(
                        f"skill entry {name}:{rel} is a string but does not start with {SYMLINK_PREFIX!r}: {entry!r}"
                    )
                _make_symlink(root, skills / rel, entry[len(SYMLINK_PREFIX) :])
            else:
                write_files(skills / rel, entry)
    for name, origins in bundled.items():
        _write_bundled_manifest(root, name, profile_home(root, name) / "skills", origins)
    for name, installs in hub.items():
        _write_hub_lock(profile_home(root, name) / "skills", installs)
    return root
