"""Regression tests for the three Windows silent-startup bugs (found live on a
real Win10 box). All three are reproducible off-Windows.

Bug 1: module-level `class _DockMenuHandler(AppKit.NSObject)` / `class
       FlowApp(rumps.App)` in flow/app.py execute on every platform, so the
       AppKit/rumps names must resolve off-mac — else the import dies with
       NameError before logging is armed (silent under pythonw).
Bug 2: pystray counts an action's positional-with-default params in
       co_argcount, so `lambda icon, item, k=key:` (3) is rejected; the
       captured value must be keyword-only.
Bug 3: under pythonw sys.stdout/stderr are non-None but their writes vanish,
       so an `is not None` guard never redirects — detect pythonw by name.
"""

from __future__ import annotations

import subprocess
import sys
import types


def test_flow_app_imports_when_not_mac():
    """Bug 1: importing flow.app with IS_MAC forced False must NOT raise
    (NameError: name 'AppKit' is not defined)."""
    code = (
        "import flow.platform_impl as p; p.IS_MAC = False; "
        "import flow.app; "
        "print('IMPORT_OK', flow.app.AppKit.NSObject is object, "
        "flow.app.rumps.App is object)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"import crashed off-mac:\n{r.stderr}"
    assert "IMPORT_OK True True" in r.stdout


class _FakePystray:
    """Minimal pystray stand-in that reproduces its co_argcount assertion:
    a callable action whose co_argcount exceeds 2 is rejected."""

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class MenuItem:
        def __init__(self, text, action=None, checked=None, radio=False,
                     default=False, visible=True, enabled=True):
            if callable(action):
                code = getattr(action, "__code__", None)
                # only real functions/lambdas carry a genuine code object
                if isinstance(code, types.CodeType) and code.co_argcount > 2:
                    raise ValueError(action)  # what real pystray raises
            self.text, self.action = text, action


def test_menu_actions_pass_pystray_argcount():
    """Bug 2: _build_menu must produce actions pystray accepts (co_argcount<=2)."""
    from unittest.mock import MagicMock

    from flow.platform_win32 import shell

    app = MagicMock()
    app._models = ["m1", "m2"]
    # unbound call so we exercise the real lambda construction in _build_menu
    menu = shell.PrivaVoxApp._build_menu(app, _FakePystray)  # must not raise
    assert menu is not None


def test_setup_logging_detects_pythonw(tmp_path, monkeypatch):
    """Bug 3: run under a 'pythonw.exe' interpreter → redirect even though
    sys.stdout/stderr look valid."""
    from flow.platform_win32 import shell

    # forward slashes so os.path.basename resolves on the POSIX test host too
    monkeypatch.setattr(shell.sys, "executable", "/py/pythonw.exe")
    monkeypatch.setattr(shell.paths, "runtime_dir", lambda: str(tmp_path))
    monkeypatch.setattr(shell.paths, "log_path", lambda: str(tmp_path / "PrivaVox.log"))
    real_stdout, real_stderr = sys.stdout, sys.stderr
    try:
        shell._setup_logging()
        assert sys.stdout is not real_stdout   # redirected despite non-None
        assert (tmp_path / "PrivaVox.log").exists()
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr


def test_setup_logging_console_run_keeps_streams(monkeypatch):
    """A normal python.exe console run must NOT redirect (dev ergonomics)."""
    from flow.platform_win32 import shell

    monkeypatch.setattr(shell.sys, "executable", "/py/python.exe")
    before = sys.stdout
    shell._setup_logging()
    assert sys.stdout is before
