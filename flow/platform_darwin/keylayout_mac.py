"""macOS 27: keep pynput from reading the keyboard layout off the main thread.

pynput's keyboard listener opens its thread with

    with keycode_context() as context:      # pynput/keyboard/_darwin.py

and keycode_context() reaches Carbon's TISGetInputSourceProperty through
ctypes. On macOS 27.0 (26A428, where this was diagnosed) that call can land in
islGetInputSourceListWithAdditions, which now does a dispatch_assert_queue() for
the MAIN queue. Earlier releases tolerated the same call — which is why this
surfaced as a sudden "it broke after the OS update". Off the main
thread the assert traps, and a trap in a C frame is not a Python exception we
could catch — the process dies outright:

    Flow quit unexpectedly.
    EXC_BREAKPOINT (SIGTRAP) ... Trace/BPT trap: 5
    _dispatch_assert_queue_fail <- dispatch_assert_queue
      <- islGetInputSourceListWithAdditions <- isValidateInputSourceRef
      <- TSMGetInputSourceProperty <- ffi_call <- PyCFuncPtr_call

The assert only fires when HIToolbox has to REBUILD its input-source list, so
it looks intermittent: it needs the cache to be cold or freshly invalidated —
a layout switch, or the system restoring a per-app input source as PrivaVox
comes to the front, right while the listener thread is starting. On an En/Bg
setup with a layout-switching helper that is most launches.

What pynput does with the layout afterwards is UCKeyTranslate(), which is a
pure function over an inert byte blob — no Carbon state, safe on any thread.
So the fix is to read the blob ONCE here on the main thread and hand pynput a
context manager that only yields the cached value. A distributed notification
refreshes it (again on the main thread) whenever the user switches layout.

prime() must run on the main thread before any pynput Listener starts.
"""

from __future__ import annotations

import contextlib

from Foundation import NSDistributedNotificationCenter, NSObject

# Posted by Text Input Services when the selected input source changes; the
# same constant Carbon exports as kTISNotifySelectedKeyboardInputSourceChanged.
_TIS_CHANGED = "AppleSelectedInputSourcesChangedNotification"

# (keyboard_type, layout_data), exactly what pynput's keycode_context() yields.
# Rebound wholesale on refresh: the listener thread only ever reads the name,
# and name rebinding is atomic, so no lock is needed.
_cached: tuple | None = None

# NSDistributedNotificationCenter does not retain its observers — drop this
# reference and layout changes stop arriving.
_observer = None


def _read_layout() -> tuple:
    """pynput's real Carbon read. MAIN THREAD ONLY — see module docstring."""
    from pynput._util.darwin import keycode_context as _real_keycode_context

    with _real_keycode_context() as context:
        # Safe to outlive the block: pynput already copied the layout out of
        # the Carbon object with .tobytes() before the CFRelease on exit.
        return context


def _refresh() -> bool:
    global _cached
    try:
        _cached = _read_layout()
    except Exception as e:
        print(f"[flow.keylayout] не успях да прочета подредбата: {e!r}")
        return False
    return True


@contextlib.contextmanager
def _cached_keycode_context():
    """Drop-in for pynput's keycode_context() that never touches Carbon."""
    yield _cached


class _LayoutObserver(NSObject):
    def layoutChanged_(self, _note) -> None:
        # Distributed notifications are delivered on the main run loop, which
        # is precisely where the Carbon read is allowed.
        _refresh()


def _install_layout_observer() -> None:
    global _observer
    _observer = _LayoutObserver.alloc().init()
    NSDistributedNotificationCenter.defaultCenter(
    ).addObserver_selector_name_object_(
        _observer, "layoutChanged:", _TIS_CHANGED, None)


def prime() -> bool:
    """Cache the layout and point pynput at the cache. Returns True on success.

    Call on the main thread, before the push-to-talk listener is created.
    """
    if not _refresh():
        # Leave pynput alone rather than feed it a NULL layout: the trap is
        # timing-dependent, a bad layout pointer would not be.
        print("[flow.keylayout] pynput остава непроменен — възможен е крах на macOS 27")
        return False

    import pynput._util.darwin as _util
    import pynput.keyboard._darwin as _kb

    _util.keycode_context = _cached_keycode_context
    # _darwin.py did `from pynput._util.darwin import keycode_context`, so its
    # module-level name is a separate reference and needs patching too.
    _kb.keycode_context = _cached_keycode_context

    try:
        _install_layout_observer()
    except Exception as e:
        # Worst case the cache goes stale after a layout switch. Harmless for
        # us: the keys the listener compares against (alt_r, esc) are not
        # layout-dependent — only character keys, which Flow ignores, would be
        # mapped by the old layout.
        print(f"[flow.keylayout] без следене на смяна на подредбата: {e!r}")
    return True
