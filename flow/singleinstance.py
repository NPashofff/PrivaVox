"""Single-instance guard: a locked lockfile inside the python process.

Covers EVERY way Flow can start (Flow.app, `python -m flow.app`,
`python -m flow`, Flow.command) — unlike the old pgrep check in the app
launcher, which only saw the bundle path.

The lock is `flow.paths.lock_path()` (macOS: ~/Library/Application
Support/Flow/flow.lock — unchanged; Windows:
%LOCALAPPDATA%\\PrivaVox\\flow.lock). The fd stays open for the whole process
lifetime; the OS releases the lock when the process dies (any death:
sys.exit, os._exit, crash, kill -9), so a stale lockfile can never block the
next start.

Only the lock PRIMITIVE is platform-specific (macOS: fcntl.flock in
flow/platform_darwin/singleinstance_mac.py; Windows: msvcrt.locking in
flow/platform_win32/singleinstance_win.py) — everything else here is shared.

File mode (`python -m flow --file`) must NOT take the lock — it is used
by the test suite and can run alongside a live daemon.
"""

from __future__ import annotations

import os

from .paths import lock_path
from .platform_impl import IS_MAC

if IS_MAC:
    from .platform_darwin.singleinstance_mac import try_lock, unlock
else:
    from .platform_win32.singleinstance_win import try_lock, unlock  # noqa: F401

LOCK_PATH = lock_path()

# Keep the locked fd referenced for the process lifetime: closing it (or
# letting it be garbage collected) would release the lock.
_lock_fd: int | None = None


def acquire(path: str = LOCK_PATH) -> bool:
    """Try to become THE Flow instance. False → another process holds the lock.

    Idempotent per process: once this process holds a lock, further calls
    return True without touching the file again.
    """
    global _lock_fd
    if _lock_fd is not None:
        return True
    # dev runs may predate the runtime dir — create it (harmless if present)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    if not try_lock(fd):
        os.close(fd)
        return False
    try:  # pid in the file is a debugging aid only — the lock is the truth
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    _lock_fd = fd
    return True


def release() -> None:
    """Let go of the lock (tests; normal exits rely on process death)."""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        unlock(_lock_fd)
    finally:
        os.close(_lock_fd)
        _lock_fd = None
