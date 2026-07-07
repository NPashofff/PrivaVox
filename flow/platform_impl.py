"""Platform dispatch for Flow (phase W1 of docs/windows-port-plan.md).

flow/platform_darwin/ — macOS implementations (production, unchanged behavior)
flow/platform_win32/  — Windows implementations (stubs until phase W2)

The pattern is deliberately the lightest one: callers branch on the booleans
below and import the platform module at the call site, e.g.

    from .platform_impl import IS_MAC

    if IS_MAC:
        from .platform_darwin.insert_mac import paste_text
    else:
        from .platform_win32.insert_win import paste_text

Kept dependency-free (stdlib only): it is imported by nearly every flow
module, including at interpreter startup on both platforms.
"""

from __future__ import annotations

import sys

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
