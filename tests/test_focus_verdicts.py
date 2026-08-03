"""focus verdict mapping (Етап 3, т. 32) — no real AX calls.

AppKit/ApplicationServices are stubbed through sys.modules, so this runs
headless and deterministically. Verifies the softened semantics:

- hard "no-text" ONLY for the bare desktop (Finder by bundle id, no focus)
- confident non-text roles (button/cell/…) → "soft-no-text" (record anyway)
- AXSelectedText on any role still means "text"
- no dead "AXSearchField" entry (it is a subrole; the role is AXTextField)
"""

from __future__ import annotations

import sys
import types

from flow import focus

_NO_VALUE = focus._NO_VALUE
_PID = 4242


def _install_ax(monkeypatch, *, bundle="com.example.app", name="TestApp",
                focused_err=0, focused=None, role="AXButton", sel_err=1):
    """Install fake AppKit + ApplicationServices modules for one scenario."""
    front = types.SimpleNamespace(
        localizedName=lambda: name,
        bundleIdentifier=lambda: bundle,
        processIdentifier=lambda: _PID,
    )
    workspace = types.SimpleNamespace(frontmostApplication=lambda: front)
    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = types.SimpleNamespace(sharedWorkspace=lambda: workspace)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)

    ax = types.ModuleType("ApplicationServices")
    ax.kAXFocusedUIElementAttribute = "AXFocusedUIElement"
    ax.kAXRoleAttribute = "AXRole"
    ax.kAXSelectedTextAttribute = "AXSelectedText"
    ax.AXUIElementCreateApplication = lambda pid: ("app-el", pid)
    ax.AXUIElementSetAttributeValue = lambda el, attr, value: 0

    def copy_attr(element, attribute, _reserved):
        if attribute == "AXFocusedUIElement":
            return focused_err, focused
        if attribute == "AXRole":
            return 0, role
        if attribute == "AXSelectedText":
            return sel_err, ("" if sel_err == 0 else None)
        raise AssertionError(f"unexpected AX attribute {attribute!r}")

    ax.AXUIElementCopyAttributeValue = copy_attr
    monkeypatch.setitem(sys.modules, "ApplicationServices", ax)
    # pretend the Electron nudge already happened: skips its 0.25 s sleep
    monkeypatch.setattr(focus, "_ax_enabled_pids", {_PID})


def test_bare_desktop_is_the_only_hard_no_text(monkeypatch):
    _install_ax(monkeypatch, bundle="com.apple.finder", name="Finder",
                focused_err=_NO_VALUE, focused=None)
    verdict, detail = focus.focused_text_target()
    assert verdict == "no-text"
    assert detail.startswith("desktop(")


def test_finder_matched_by_bundle_id_not_localized_name(monkeypatch):
    # a localized Finder ("Файндър"…) must still be caught — and any other
    # app that merely CALLS itself Finder must not be
    _install_ax(monkeypatch, bundle="com.apple.finder", name="Файндър",
                focused_err=_NO_VALUE, focused=None)
    assert focus.focused_text_target()[0] == "no-text"

    _install_ax(monkeypatch, bundle="com.evil.fakefinder", name="Finder",
                focused_err=_NO_VALUE, focused=None)
    assert focus.focused_text_target()[0] == "unknown"


def test_hidden_ax_tree_stays_unknown(monkeypatch):
    _install_ax(monkeypatch, bundle="com.example.electron", name="Slack",
                focused_err=_NO_VALUE, focused=None)
    verdict, detail = focus.focused_text_target()
    assert verdict == "unknown"
    assert "no-focused-element" in detail


def test_text_field_is_text(monkeypatch):
    _install_ax(monkeypatch, focused="el", role="AXTextField", sel_err=1)
    verdict, detail = focus.focused_text_target()
    assert verdict == "text"
    assert detail.startswith("AXTextField(")


def test_confident_non_text_role_is_soft(monkeypatch):
    # AXCell (spreadsheets) accepts typing yet exposes no AXSelectedText:
    # must warn, must NOT hard-block the recording
    _install_ax(monkeypatch, focused="el", role="AXCell", sel_err=1)
    verdict, detail = focus.focused_text_target()
    assert verdict == "soft-no-text"
    assert detail.startswith("AXCell(")


def test_selected_text_promotes_any_role_to_text(monkeypatch):
    _install_ax(monkeypatch, focused="el", role="AXWebArea", sel_err=0)
    verdict, detail = focus.focused_text_target()
    assert verdict == "text"
    assert "+selected-text" in detail


def test_no_dead_searchfield_entry():
    assert "AXSearchField" not in focus._TEXT_ROLES
