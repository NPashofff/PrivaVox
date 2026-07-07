"""fcntl.flock primitives for the single-instance guard (macOS / POSIX).

The kernel releases an flock together with its holding process (any death:
sys.exit, os._exit, crash, kill -9), so a stale lockfile can never block the
next start. The shared orchestration and the full contract live in
flow/singleinstance.py; this module is the lock primitive only.
"""

from __future__ import annotations

import fcntl


def try_lock(fd: int) -> bool:
    """Non-blocking exclusive lock on fd; False → another process holds it."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
