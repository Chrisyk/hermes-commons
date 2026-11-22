import os

from commons.core import filelock


def _open(path):
    return os.open(path, os.O_RDWR | os.O_CREAT | filelock.O_BINARY, 0o600)


def test_second_handle_cannot_lock_until_released(tmp_path):
    p = tmp_path / "lock"
    a, b = _open(p), _open(p)
    try:
        assert filelock.try_lock(a, 0) is True
        assert filelock.try_lock(b, 0) is False
        filelock.unlock(a, 0)
        assert filelock.try_lock(b, 0) is True
        filelock.unlock(b, 0)
    finally:
        os.close(a)
        os.close(b)


def test_holder_text_readable_while_high_offset_locked(tmp_path):
    p = tmp_path / "lock"
    a, b = _open(p), _open(p)
    off = filelock.HIGH_OFFSET if filelock.IS_WINDOWS else 0
    try:
        assert filelock.try_lock(a, off)
        filelock.write_at_start(a, b"123 commons\n")
        assert filelock.read_at_start(b).startswith(b"123 commons")
        filelock.unlock(a, off)
    finally:
        os.close(a)
        os.close(b)
