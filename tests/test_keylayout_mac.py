"""macOS 27 crash: pynput read the keyboard layout off the main thread.

The listener thread opened with `with keycode_context()`, which calls Carbon's
TISGetInputSourceProperty; on macOS 27 that can reach a dispatch_assert_queue
for the main queue and SIGTRAP the whole process ("Flow quit unexpectedly",
EXC_BREAKPOINT). flow/platform_darwin/keylayout_mac.py reads the layout once on
the main thread and feeds pynput the cache instead.

The trap itself is timing-dependent (it needs HIToolbox's input-source cache to
be cold), so these tests assert the invariant that makes it impossible rather
than trying to race it: after prime(), the code path the listener thread runs
performs NO Carbon call at all.
"""

from __future__ import annotations

import threading

import pytest

keylayout = pytest.importorskip(
    "flow.platform_darwin.keylayout_mac",
    reason="mac-only (pyobjc Foundation / pynput darwin backend)")

import pynput._util.darwin as pynput_util          # noqa: E402
import pynput.keyboard._darwin as pynput_kb        # noqa: E402


@pytest.fixture
def restore_pynput():
    """prime() monkeypatches pynput and the module caches — undo both."""
    saved = (pynput_util.keycode_context, pynput_kb.keycode_context,
             keylayout._cached, keylayout._observer)
    yield
    (pynput_util.keycode_context, pynput_kb.keycode_context,
     keylayout._cached, keylayout._observer) = saved


def test_prime_patches_both_references(restore_pynput):
    # keyboard/_darwin.py did `from ... import keycode_context`, so its
    # module-level name is a SECOND reference; patching only _util would leave
    # the listener thread calling the real one.
    assert keylayout.prime() is True
    assert pynput_util.keycode_context is keylayout._cached_keycode_context
    assert pynput_kb.keycode_context is keylayout._cached_keycode_context


def test_cached_layout_is_usable(restore_pynput):
    assert keylayout.prime() is True
    keyboard_type, layout_data = keylayout._cached
    assert isinstance(keyboard_type, int)
    # An inert blob: UCKeyTranslate over it is a pure function, which is why
    # handing it to another thread is safe when the Carbon read is not.
    assert isinstance(layout_data, bytes) and layout_data


def test_listener_path_makes_no_carbon_call(restore_pynput):
    """The regression itself: what Listener._run() does must not reach Carbon."""
    assert keylayout.prime() is True

    calls: list[str] = []
    main_id = threading.main_thread().ident
    real = pynput_util.CarbonExtra.TISGetInputSourceProperty
    pynput_util.CarbonExtra.TISGetInputSourceProperty = (
        lambda *a, **kw: (calls.append("main" if threading.get_ident() == main_id
                                       else "worker"), real(*a, **kw))[1])
    try:
        seen = []

        def listener_thread_body():
            # Verbatim the opening of pynput.keyboard._darwin.Listener._run.
            with pynput_kb.keycode_context() as context:
                seen.append(context)

        t = threading.Thread(target=listener_thread_body)
        t.start()
        t.join()
    finally:
        pynput_util.CarbonExtra.TISGetInputSourceProperty = real

    assert calls == [], f"Carbon called from {calls} — macOS 27 can trap here"
    assert seen == [keylayout._cached]


def test_refreshed_layout_reaches_the_listener(restore_pynput):
    """A layout switch refreshes on the main thread; the listener sees it."""
    assert keylayout.prime() is True
    refreshed = (42, b"pretend-this-is-the-bulgarian-layout")
    keylayout._cached = refreshed          # what the notification handler does

    seen: list[tuple] = []

    def listener_thread_body():
        with pynput_kb.keycode_context() as context:
            seen.append(context)

    t = threading.Thread(target=listener_thread_body)
    t.start()
    t.join()

    assert seen == [refreshed]


def test_failed_read_leaves_pynput_alone(restore_pynput, monkeypatch):
    """A NULL layout would break pynput unconditionally — worse than the trap."""
    original = pynput_kb.keycode_context
    monkeypatch.setattr(keylayout, "_read_layout",
                        lambda: (_ for _ in ()).throw(OSError("Carbon is unhappy")))
    keylayout._cached = None

    assert keylayout.prime() is False
    assert pynput_kb.keycode_context is original
