"""Insert text into the focused app: clipboard + a synthesized paste keystroke.

Platform implementations behind a stable public API (paste_text,
get_clipboard, set_clipboard, can_post_events):

  - macOS:   flow/platform_darwin/insert_mac.py — pbcopy/pbpaste + Quartz
             CGEvent Cmd-V, gated on the Accessibility permission.
  - Windows: flow/platform_win32/insert_win.py — ctypes clipboard + Ctrl+V
             via pynput/SendInput (no TCC gate; can_post_events() is True).
"""

from __future__ import annotations

from .platform_impl import IS_MAC

if IS_MAC:
    from .platform_darwin.insert_mac import (  # noqa: F401
        KVK_ANSI_V,
        _post_cmd_v,
        can_post_events,
        get_clipboard,
        paste_text,
        set_clipboard,
    )
else:
    from .platform_win32.insert_win import (  # noqa: F401
        can_post_events,
        get_clipboard,
        paste_text,
        set_clipboard,
    )

__all__ = ["paste_text", "get_clipboard", "set_clipboard", "can_post_events"]
