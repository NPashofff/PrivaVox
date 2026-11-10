"""paste_remote (macOS remote-desktop staging) — no real AppKit/clipboard.

Windows App has no pasteboard timer: it announces the Mac clipboard to the
remote only when its window BECOMES key with a changed changeCount, and on
RESIGN key it either pulls the remote clipboard onto the Mac one or — if the
Mac one already changed — leaves that remote news pending. A text copied
BEFORE the deactivation therefore breaks as soon as something was copied on
the remote side (the next Ctrl+V pasted that remote content, even a picture).
paste_remote must: deactivate → copy → reactivate (never copy first) →
settle → VERIFY the text survived (one more cycle if not). A hung clipboard
must surface as "clipboard-error", never as a green "ready".
"""

from __future__ import annotations

import pytest

Quartz = pytest.importorskip("Quartz")  # mac-only module under test

from flow.config import FlowConfig  # noqa: E402
from flow.platform_darwin import insert_mac  # noqa: E402


def _cfg(**kw) -> FlowConfig:
    kw.setdefault("remote_clipboard_settle_s", 0.0)  # no real sleeping in tests
    return FlowConfig(**kw)


@pytest.fixture()
def quiet_cc(monkeypatch):
    """Stub the NSPasteboard change counter (diagnostic-only)."""
    monkeypatch.setattr(insert_mac, "_pb_change_count", lambda: 0)


def test_empty_text_is_reported(quiet_cc):
    assert insert_mac.paste_remote("", _cfg()) == "empty"


def test_copy_happens_only_while_client_is_deactivated(monkeypatch, quiet_cc):
    calls: list[str] = []
    monkeypatch.setattr(insert_mac, "set_clipboard", lambda t: calls.append(f"copy:{t}"))
    monkeypatch.setattr(insert_mac, "get_clipboard", lambda: "проба")

    def fake_nudge(stage=None):
        calls.append("deactivate")
        if stage is not None:
            stage()
        calls.append("reactivate")

    monkeypatch.setattr(insert_mac, "_nudge_frontmost_reactivate", fake_nudge)
    status = insert_mac.paste_remote("проба", _cfg(remote_reactivate_nudge=True))
    assert status == "clipboard-only"
    # NO copy before the deactivation — that is the regression this guards.
    assert calls == ["deactivate", "copy:проба", "reactivate"]


def test_clobbered_clipboard_gets_a_second_cycle(monkeypatch, quiet_cc):
    calls: list[str] = []
    monkeypatch.setattr(insert_mac, "set_clipboard", lambda t: calls.append("copy"))
    reads = iter(["НЕЩО ДРУГО", "проба"])  # first verify fails, second passes
    monkeypatch.setattr(insert_mac, "get_clipboard", lambda: next(reads))

    def fake_nudge(stage=None):
        calls.append("nudge")
        if stage is not None:
            stage()

    monkeypatch.setattr(insert_mac, "_nudge_frontmost_reactivate", fake_nudge)
    status = insert_mac.paste_remote("проба", _cfg(remote_reactivate_nudge=True))
    assert status == "clipboard-only"
    # cycle(+copy) → verify FAIL → cycle(+copy) → verify OK
    assert calls == ["nudge", "copy", "nudge", "copy"]


def test_nudge_can_be_disabled(monkeypatch, quiet_cc):
    calls: list[str] = []
    monkeypatch.setattr(insert_mac, "set_clipboard", lambda t: calls.append("copy"))
    monkeypatch.setattr(insert_mac, "get_clipboard", lambda: "проба")
    monkeypatch.setattr(insert_mac, "_nudge_frontmost_reactivate",
                        lambda stage=None: calls.append("nudge"))
    status = insert_mac.paste_remote("проба", _cfg(remote_reactivate_nudge=False))
    assert status == "clipboard-only"
    assert calls == ["copy"]


def test_hung_clipboard_is_an_error_not_a_green_ready(monkeypatch, quiet_cc):
    def _boom(_text: str) -> None:
        raise RuntimeError("pbcopy stuck (RDP redirection)")

    monkeypatch.setattr(insert_mac, "set_clipboard", _boom)
    order: list[str] = []

    def fake_nudge(stage=None):
        order.append("deactivate")
        stage()  # must NOT raise out of the cycle …
        order.append("reactivate")  # … the client always gets its focus back

    monkeypatch.setattr(insert_mac, "_nudge_frontmost_reactivate", fake_nudge)
    status = insert_mac.paste_remote("проба", _cfg(remote_reactivate_nudge=True))
    assert status == "clipboard-error"
    assert order == ["deactivate", "reactivate"]


def test_cycle_that_never_stages_still_copies(monkeypatch, quiet_cc):
    """No frontmost app / AppKit failure: the nudge returns without calling
    stage — the text must still reach the clipboard."""
    calls: list[str] = []
    monkeypatch.setattr(insert_mac, "set_clipboard", lambda t: calls.append("copy"))
    monkeypatch.setattr(insert_mac, "get_clipboard", lambda: "проба")
    monkeypatch.setattr(insert_mac, "_nudge_frontmost_reactivate", lambda stage=None: None)
    status = insert_mac.paste_remote("проба", _cfg(remote_reactivate_nudge=True))
    assert status == "clipboard-only"
    assert calls == ["copy"]


def test_real_nudge_stages_after_deactivation_and_quiet_wait(monkeypatch):
    """The real cycle, with AppKit stubbed: deactivate → wait for the client's
    resign work → stage → hand focus back cooperatively."""
    import sys
    import types

    order: list[str] = []

    class _Front:
        def processIdentifier(self):
            return 42

        def hide(self):
            order.append("hide")

        def unhide(self):
            order.append("unhide")

        def activateWithOptions_(self, _opts):
            order.append("blunt-activate")

        def activateFromApplication_options_(self, _me, _opts):
            order.append("reactivate")

    class _App:
        def activateIgnoringOtherApps_(self, _flag):
            order.append("self-activate")

        def yieldActivationToApplication_(self, _other):
            order.append("yield")

    fake = types.SimpleNamespace(
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
            frontmostApplication=lambda: _Front())),
        NSApplication=types.SimpleNamespace(sharedApplication=lambda: _App()),
        NSApplicationActivateIgnoringOtherApps=2,
        NSRunningApplication=types.SimpleNamespace(currentApplication=lambda: object()),
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake)
    monkeypatch.setitem(sys.modules, "PyObjCTools", types.SimpleNamespace(
        AppHelper=types.SimpleNamespace(callAfter=lambda fn: fn())))
    monkeypatch.setattr(insert_mac, "_wait_frontmost", lambda pid, want_front, timeout_s: True)
    monkeypatch.setattr(insert_mac, "_wait_pasteboard_quiet",
                        lambda: order.append("quiet-wait") or 0)
    monkeypatch.setattr(insert_mac, "_NUDGE_DWELL_S", 0.0)
    insert_mac._nudge_frontmost_reactivate(lambda: order.append("stage"))
    # The hand-back MUST be the cooperative one: a blunt activateWithOptions_
    # reactivates the app without making its window key, and Windows App only
    # syncs the clipboard on NSWindowDidBecomeKey.
    assert order == ["self-activate", "quiet-wait", "stage", "unhide", "yield", "reactivate"]
