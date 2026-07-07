"""Detect whether a text-input UI element currently has keyboard focus.

Used by the menu bar app to warn the user at record start ("no input field
selected") and to decide whether to also keep the text on the clipboard.
It must never block an insert: any uncertainty answers True.

The query goes to the FRONTMOST APPLICATION's AX tree, not the system-wide
element — the system-wide kAXFocusedUIElementAttribute returns NoValue for
many apps (Electron and друга custom UI) even while a field is focused.
"""

from __future__ import annotations

import time

_TEXT_ROLES = {
    "AXTextField",
    "AXTextArea",
    "AXComboBox",
    # no "AXSearchField": that is only an AXSubrole — real search fields
    # report role AXTextField, so an entry here would be dead weight
}

_NO_VALUE = -25212  # kAXErrorNoValue

# PIDs where we've already asked Electron/Chromium to build its AX tree
# (AXManualAccessibility). One-time per app instance; others ignore it.
_ax_enabled_pids: set[int] = set()


def focused_text_target() -> tuple[str, str]:
    """Return (verdict, detail_for_logs).

    verdict: "text"         — confidently a text input
             "no-text"      — the bare desktop (Finder frontmost, no focused
                              element): the ONLY case confident enough to
                              refuse a recording outright
             "soft-no-text" — focused element with a confident non-text role
                              (button, cell, group…): warn, but let the
                              recording/insert proceed — spreadsheet cells
                              (AXCell) accept typing yet expose no
                              text-field AX attributes
             "unknown"      — can't tell (Electron apps hide their AX tree;
                              treat as OK — never warn, never block)
    """
    try:
        import AppKit
        from ApplicationServices import (  # type: ignore
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            AXUIElementSetAttributeValue,
            kAXFocusedUIElementAttribute,
            kAXRoleAttribute,
            kAXSelectedTextAttribute,
        )
    except Exception:
        return "unknown", "ax-imports-unavailable"

    try:
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return "unknown", "no-frontmost-app"
        app_name = str(front.localizedName())
        pid = front.processIdentifier()
        app_el = AXUIElementCreateApplication(pid)

        err, focused = AXUIElementCopyAttributeValue(
            app_el, kAXFocusedUIElementAttribute, None
        )
        if (err == _NO_VALUE or (err == 0 and focused is None)) and pid not in _ax_enabled_pids:
            # Electron/Chromium builds its AX tree only when an assistive
            # client asks for it. Ask once per app instance, then re-query.
            _ax_enabled_pids.add(pid)
            AXUIElementSetAttributeValue(app_el, "AXManualAccessibility", True)
            time.sleep(0.25)
            err, focused = AXUIElementCopyAttributeValue(
                app_el, kAXFocusedUIElementAttribute, None
            )
        if err == _NO_VALUE or (err == 0 and focused is None):
            # Finder with no focused element = the bare desktop; anything
            # else simply hides its tree from us even after the nudge.
            # Match Finder by bundle id — localizedName is localization-fragile.
            if str(front.bundleIdentifier()) == "com.apple.finder":
                return "no-text", f"desktop({app_name})"
            return "unknown", f"no-focused-element({app_name})"
        if err != 0:
            return "unknown", f"ax-uncertain({app_name}, err={err})"

        err, role = AXUIElementCopyAttributeValue(focused, kAXRoleAttribute, None)
        role = str(role) if err == 0 and role is not None else "unknown-role"
        if role in _TEXT_ROLES:
            return "text", f"{role}({app_name})"

        # Web content and custom views often report other roles (AXWebArea,
        # AXGroup…) yet expose AXSelectedText when they accept text input.
        err, _sel = AXUIElementCopyAttributeValue(
            focused, kAXSelectedTextAttribute, None
        )
        if err == 0:
            return "text", f"{role}+selected-text({app_name})"
        # Confident non-text role — but only SOFT: warn without blocking,
        # because e.g. Numbers/Excel cells take typed input regardless.
        return "soft-no-text", f"{role}({app_name})"
    except Exception as e:
        return "unknown", f"ax-error {e!r}"
