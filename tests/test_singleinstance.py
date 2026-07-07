"""single-instance lockfile contract (Етап 3, т. 14).

The flock must: deny a second PROCESS while held, be idempotent within one
process, come back after release(), and — crucially for the permissions
relaunch (os._exit + `open`) — evaporate the moment the holding process dies.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from flow import singleinstance

ROOT = Path(__file__).resolve().parents[1]

_DENIED = 17  # arbitrary marker exit code for "acquire() returned False"


def _acquire_in_subprocess(lock_path: str) -> int:
    """Try the lock from a fresh process; 0 = acquired, _DENIED = refused."""
    code = (
        "import sys; from flow import singleinstance as si; "
        f"sys.exit(0 if si.acquire({lock_path!r}) else {_DENIED})"
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, timeout=30
    ).returncode


def test_second_process_is_denied_while_lock_is_held(tmp_path):
    lock = str(tmp_path / "flow.lock")
    assert singleinstance.acquire(lock) is True
    try:
        assert _acquire_in_subprocess(lock) == _DENIED
    finally:
        singleinstance.release()


def test_acquire_is_idempotent_within_the_process(tmp_path):
    lock = str(tmp_path / "flow.lock")
    assert singleinstance.acquire(lock) is True
    try:
        assert singleinstance.acquire(lock) is True
    finally:
        singleinstance.release()


def test_lock_is_free_again_after_release(tmp_path):
    lock = str(tmp_path / "flow.lock")
    assert singleinstance.acquire(lock) is True
    singleinstance.release()
    assert _acquire_in_subprocess(lock) == 0


def test_lock_dies_with_the_holding_process(tmp_path):
    # the subprocess acquires and exits: the kernel must release the flock
    # with it (this is what makes the os._exit relaunch safe — the fresh
    # instance can always take the lock after the old pid is gone)
    lock = str(tmp_path / "flow.lock")
    assert _acquire_in_subprocess(lock) == 0
    assert singleinstance.acquire(lock) is True
    singleinstance.release()


def test_lock_directory_is_created_when_missing(tmp_path):
    lock = str(tmp_path / "does" / "not" / "exist" / "flow.lock")
    assert singleinstance.acquire(lock) is True
    singleinstance.release()
