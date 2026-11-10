"""msvcrt.locking primitives for the single-instance guard (Windows) — W2.

Mirror of flow/platform_darwin/singleinstance_mac.py (fcntl.flock): same
try_lock/unlock contract, consumed by flow/singleinstance.py, which owns the
shared orchestration (fd lifetime, pid debug write, directory creation).

Windows specifics:
- msvcrt.locking locks a BYTE RANGE starting at the fd's CURRENT position, so
  both primitives seek to offset 0 first and lock exactly 1 byte. The range
  may extend beyond EOF (the file starts empty), which Win32 explicitly
  allows. The shared acquire() then ftruncate+writes the pid through the SAME
  fd — the lock owner may write inside its own locked region.
- LK_NBLCK is the non-blocking probe (≈ LOCK_EX|LOCK_NB): it raises OSError
  immediately when another process holds the byte.
- The OS releases the lock when the holding process dies (any death), so a
  stale lockfile can never block the next start — same guarantee as flock.

Like the darwin module, this imports its platform lock module (msvcrt) at
top and must only be imported behind the sys.platform dispatch; the macOS
unit tests inject a fake `msvcrt` into sys.modules before importing it.
"""

from __future__ import annotations

import msvcrt
import os


def try_lock(fd: int) -> bool:
    """Non-blocking exclusive lock on fd; False → another process holds it."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)  # the pid write moved the position
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
