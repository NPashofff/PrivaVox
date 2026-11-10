"""Platform-dependent filesystem locations for Flow's runtime state.

darwin: ~/Library/Application Support/Flow   (the existing layout, unchanged —
        the live mac runtime, TCC grants included, lives there today)
win32:  %LOCALAPPDATA%\\PrivaVox               (the Install-PrivaVox.ps1 contract:
        venv, code copy, settings.json, dictionary.txt, PrivaVox.log)

Only path STRINGS live here — directory creation stays with the callers
(e.g. flow.singleinstance.acquire makes the lock's directory on demand).
"""

from __future__ import annotations

import os

from .platform_impl import IS_WINDOWS


def runtime_dir() -> str:
    """Per-user runtime dir (settings.json, flow.lock, …)."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
        return os.path.join(base, "PrivaVox")
    return os.path.expanduser("~/Library/Application Support/Flow")


def lock_path() -> str:
    """Single-instance lockfile (see flow/singleinstance.py)."""
    return os.path.join(runtime_dir(), "flow.lock")


def log_path() -> str:
    """App log. On win32 the app CREATES this file itself (pythonw has no
    console — flow/platform_win32/shell.py redirects stdout/stderr here); on
    macOS the Flow.app launcher owns the redirect and this is just its path.
    """
    if IS_WINDOWS:
        return os.path.join(runtime_dir(), "PrivaVox.log")
    return os.path.expanduser("~/Library/Logs/Flow.log")
