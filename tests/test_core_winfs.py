import subprocess
import sys
from pathlib import Path

import pytest

from commons.core import winfs


def test_dir_link_roundtrip_and_removal_keeps_target(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    (target / "f").write_text("x")
    link = tmp_path / "l"
    winfs.make_dir_link(link, target)
    assert winfs.is_link(link)
    assert Path(winfs.read_link(link)).resolve() == target.resolve()
    winfs.remove_link(link)
    assert not link.exists() and (target / "f").exists()


def test_fsync_path_file_and_dir(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    winfs.fsync_path(f)
    winfs.fsync_path(tmp_path)


def test_clean_link_text():
    if winfs.IS_WINDOWS:
        assert winfs.clean_link_text("\\\\?\\C:\\a\\b") == "C:\\a\\b"
    assert winfs.clean_link_text("/a/b") == "/a/b"


def test_relative_anchor_posix_and_drive():
    assert winfs.relative_anchor(Path("/a/b")) == Path("a/b") or winfs.IS_WINDOWS
    if winfs.IS_WINDOWS:
        assert winfs.relative_anchor(Path("C:\\a\\b")) == Path("C/a/b")


def test_replace_retry(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("1")
    b.write_text("2")
    winfs.replace_retry(a, b)
    assert b.read_text() == "1" and not a.exists()


def test_kill_tree_stops_child():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **winfs.popen_group_kwargs())
    winfs.kill_tree(proc)
    assert proc.wait(timeout=10) != 0


@pytest.mark.skipif(not winfs.IS_WINDOWS, reason="junctions are Windows-only")
def test_junction_is_link_but_not_symlink(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    link = tmp_path / "j"
    import _winapi

    _winapi.CreateJunction(str(target), str(link))
    assert winfs.is_junction(link) and winfs.is_link(link) and not link.is_symlink()
