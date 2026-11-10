"""On-screen dictation HUD: a floating pill at the bottom-center of the screen.

Shows the live dictation state (recording / processing / inserted) so the user
gets feedback where they're looking, not just in the menu bar. AppKit panel,
non-activating, click-through, visible over full-screen apps.

All AppKit calls are marshalled to the main thread via AppHelper.callAfter,
so show()/hide() are safe to call from any thread. Delayed hides run on the
main run loop (AppHelper.callLater — no thread per show) and carry a
generation number: every show()/hide() bumps the generation, and a scheduled
hide fires only if its generation is still current — a timer armed for an old
message can never take down a newer (possibly persistent) one.
"""

from __future__ import annotations

import math
import threading

import AppKit
from PyObjCTools import AppHelper

_H = 44.0
_MIN_W = 220.0
_EDGE_GAP = 80.0        # min horizontal air between pill and screen edges
_TEXT_PAD = 16.0        # label inset inside the pill (each side)
_LABEL_H = 20.0
_BOTTOM_MARGIN = 96.0

_COLORS = {
    "white": AppKit.NSColor.whiteColor,
    "red": AppKit.NSColor.systemRedColor,
    "green": AppKit.NSColor.systemGreenColor,
    "orange": AppKit.NSColor.systemOrangeColor,
}


class HUD:
    def __init__(self) -> None:
        self._panel: AppKit.NSPanel | None = None
        self._label: AppKit.NSTextField | None = None
        self._gen = 0                       # bumped by every show()/hide()
        self._gen_lock = threading.Lock()

    # ---- main-thread internals --------------------------------------------

    def _ensure_panel(self) -> None:
        if self._panel is not None:
            return
        # placeholder rect: _place() sizes and positions it on every show
        rect = AppKit.NSMakeRect(0.0, _BOTTOM_MARGIN, _MIN_W, _H)
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        content = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, _MIN_W, _H))
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(_H / 2.0)
        content.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.93).CGColor()
        )

        label = AppKit.NSTextField.labelWithString_("")
        label.setFrame_(AppKit.NSMakeRect(_TEXT_PAD, (_H - _LABEL_H) / 2.0,
                                          _MIN_W - 2 * _TEXT_PAD, _LABEL_H))
        label.setAlignment_(AppKit.NSTextAlignmentCenter)
        label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(15, AppKit.NSFontWeightMedium))
        label.setTextColor_(AppKit.NSColor.whiteColor())
        label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        content.addSubview_(label)

        panel.setContentView_(content)
        self._panel, self._label = panel, label

    def _measure(self, text: str) -> float:
        """Intrinsic single-line width of `text` in the label's font (pt)."""
        font = self._label.font() or AppKit.NSFont.systemFontOfSize_weight_(
            15, AppKit.NSFontWeightMedium)
        attr = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            text, {AppKit.NSFontAttributeName: font})
        return float(attr.size().width)

    def _place(self, text: str) -> None:
        """Size the pill to `text` and bottom-center it on the CURRENT main
        screen — on every show, never cached: on multi-display setups the
        main screen (the one with keyboard focus) moves, and its frame.origin
        is non-zero in global coordinates. AppKit's y axis grows upward from
        the bottom-left, so the pill sits at y = frame.origin.y + margin.
        """
        screen = AppKit.NSScreen.mainScreen()
        if screen is None:  # pragma: no cover - headless / display asleep
            screens = AppKit.NSScreen.screens()
            if not screens:
                return
            screen = screens[0]
        frame = screen.frame()
        needed = math.ceil(self._measure(text)) + 2 * _TEXT_PAD
        w = max(_MIN_W, min(float(needed), frame.size.width - _EDGE_GAP))
        x = frame.origin.x + (frame.size.width - w) / 2.0
        y = frame.origin.y + _BOTTOM_MARGIN
        self._panel.setFrame_display_(AppKit.NSMakeRect(x, y, w, _H), True)
        # the content view tracks the panel; the label needs its inset frame
        self._label.setFrame_(AppKit.NSMakeRect(_TEXT_PAD, (_H - _LABEL_H) / 2.0,
                                                w - 2 * _TEXT_PAD, _LABEL_H))

    def _apply(self, gen: int, text: str, color_name: str,
               hide_after: float | None) -> None:
        with self._gen_lock:
            if gen != self._gen:
                return  # superseded by a newer show()/hide() before drawing
        self._ensure_panel()
        self._label.setStringValue_(text)
        self._label.setTextColor_(_COLORS.get(color_name, _COLORS["white"])())
        self._place(text)
        self._panel.orderFrontRegardless()
        if hide_after is not None:
            # We are ON the main thread here (via callAfter), which is what
            # callLater needs: it arms an NSTimer on the current run loop.
            AppHelper.callLater(hide_after, self._hide_if_current, gen)

    def _hide_if_current(self, gen: int) -> None:
        with self._gen_lock:
            if gen != self._gen:
                return  # a newer message owns the panel — leave it alone
        if self._panel is not None:
            self._panel.orderOut_(None)

    def _hide_now(self) -> None:
        if self._panel is not None:
            self._panel.orderOut_(None)

    # ---- thread-safe API ---------------------------------------------------

    def show(self, text: str, color: str = "white", hide_after: float | None = None) -> None:
        with self._gen_lock:
            self._gen += 1
            gen = self._gen
        AppHelper.callAfter(self._apply, gen, text, color, hide_after)

    def hide(self) -> None:
        with self._gen_lock:
            self._gen += 1  # invalidates every scheduled hide as well
        AppHelper.callAfter(self._hide_now)
