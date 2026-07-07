"""W2: singleinstance_win contract tests — msvcrt mocked (runs on macOS).

flow/platform_win32/singleinstance_win.py imports msvcrt at module top (the
mirror of fcntl in the darwin module), so the fake module is installed in
sys.modules BEFORE the import. A real temp-file fd backs each test: os.lseek
must actually run, because the msvcrt.locking contract is positional (it
locks bytes from the CURRENT position — a regression that stops seeking to 0
would lock the wrong byte after the pid write).
"""

from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

# real msvcrt constants (https://docs.python.org/3/library/msvcrt.html)
LK_UNLCK = 0
LK_NBLCK = 2


@pytest.fixture()
def si_win(monkeypatch):
    """(module, fake_msvcrt_log) with a fresh import against a fake msvcrt."""
    log: list[tuple] = []
    fake = types.ModuleType("msvcrt")
    fake.LK_UNLCK = LK_UNLCK
    fake.LK_NBLCK = LK_NBLCK
    fake.error = OSError

    def locking(fd, mode, nbytes):
        log.append(("locking", fd, mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if getattr(fake, "raise_on_lock", False) and mode == LK_NBLCK:
            raise OSError(13, "Permission denied")

    fake.locking = locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.delitem(sys.modules, "flow.platform_win32.singleinstance_win",
                        raising=False)
    module = importlib.import_module("flow.platform_win32.singleinstance_win")
    yield module, fake, log
    # leave no fake-backed module behind for other tests
    sys.modules.pop("flow.platform_win32.singleinstance_win", None)


@pytest.fixture()
def lockfd(tmp_path):
    fd = os.open(str(tmp_path / "flow.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    yield fd
    os.close(fd)


def test_try_lock_locks_one_byte_at_offset_zero(si_win, lockfd):
    module, _, log = si_win
    os.lseek(lockfd, 0, os.SEEK_SET)
    os.write(lockfd, b"1234\n")          # simulate an old pid: position != 0
    assert module.try_lock(lockfd) is True
    assert log == [("locking", lockfd, LK_NBLCK, 1, 0)]  # non-blocking, 1 byte, AT 0


def test_try_lock_false_when_held_elsewhere(si_win, lockfd):
    module, fake, log = si_win
    fake.raise_on_lock = True            # another process holds the byte
    assert module.try_lock(lockfd) is False
    assert log[-1][:4] == ("locking", lockfd, LK_NBLCK, 1)


def test_unlock_unlocks_the_same_byte_at_offset_zero(si_win, lockfd):
    module, _, log = si_win
    assert module.try_lock(lockfd) is True
    os.write(lockfd, f"{os.getpid()}\n".encode())  # the acquire() pid write
    module.unlock(lockfd)
    assert log[-1] == ("locking", lockfd, LK_UNLCK, 1, 0)  # seeked back to 0


def test_lock_unlock_do_not_swallow_other_oserrors(si_win):
    module, _, _ = si_win
    with pytest.raises(OSError):
        module.unlock(-1)                # bad fd: os.lseek raises loudly
