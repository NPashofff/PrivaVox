"""W2: Tkinter HUD — generation-counter logic (headless) + a real-Tk smoke.

Two layers, per the hud_win threading note:
- pure logic: HUD(_start_tk=False) never spawns the Tk thread; tests inspect
  the command queue and the generation counter directly.
- Tk smoke: macOS Tk (Aqua) aborts the process if a window is created off
  the main thread, so the smoke test drives the SAME _tk_init/_tk_drain
  methods synchronously on the main thread, window withdrawn
  (_deiconify=False) — no visible flash, no thread. The worker-thread mode
  is exercised only on real Windows (W4 checklist).

Skips cleanly when tkinter or a display is unavailable.
"""

from __future__ import annotations

import time

import pytest

from flow.platform_win32.hud_win import HUD


# --------------------------------------------------------------------------
# 1. Generation counter + queue semantics (no Tk at all)
# --------------------------------------------------------------------------

def test_show_enqueues_and_bumps_generation():
    hud = HUD(_start_tk=False)
    hud.show("Записва…", "red")
    hud.show("Обработва…", hide_after=1.5)
    assert hud._gen == 2
    first = hud._queue.get_nowait()
    second = hud._queue.get_nowait()
    assert first == ("show", 1, "Записва…", "red", None)
    assert second == ("show", 2, "Обработва…", "white", 1.5)
    assert hud._thread is None  # _start_tk=False: no Tk thread ever


def test_hide_bumps_generation_and_invalidates_scheduled_hides():
    hud = HUD(_start_tk=False)
    hud.show("Вмъкнато", "green", hide_after=1.2)
    gen_of_show = hud._gen
    assert hud._is_current(gen_of_show)
    hud.hide()  # bumps: the armed hide timer for gen_of_show must be stale now
    assert not hud._is_current(gen_of_show)
    assert hud._queue.get_nowait()[0] == "show"
    assert hud._queue.get_nowait() == ("hide",)


def test_newer_show_supersedes_older_before_drawing():
    hud = HUD(_start_tk=False)
    hud.show("старо")
    hud.show("ново")
    # the drain-time check the Tk thread applies:
    assert not hud._is_current(1)   # old show must draw nothing
    assert hud._is_current(2)       # the newest one owns the pill


def test_dead_hud_is_a_noop():
    hud = HUD(_start_tk=False)
    hud._dead = True
    hud.show("текст")
    hud.hide()
    assert hud._queue.empty() and hud._gen == 0


# --------------------------------------------------------------------------
# 2. Real-Tk smoke on the main thread (withdrawn window)
# --------------------------------------------------------------------------

def _display_available() -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


needs_display = pytest.mark.skipif(
    not _display_available(), reason="tkinter unavailable or no display"
)


@needs_display
def test_tk_smoke_show_hide_without_crash():
    hud = HUD(_start_tk=False, _deiconify=False)  # main thread, never mapped
    assert hud._tk_init() is True
    try:
        hud.show("PrivaVox е готов — задръж десния Ctrl и говори", hide_after=0.05)
        hud.show("● Записва…", "red")           # supersedes the first
        assert hud._tk_drain() is False          # draws only the current one
        hud.hide()
        assert hud._tk_drain() is False
        hud.show("✓ Вмъкнато", "green", hide_after=0.03)
        assert hud._tk_drain() is False
        time.sleep(0.05)
        hud._root.update()                       # let the after-timer fire
        assert not hud._dead
    finally:
        hud._queue.put(("quit",))
        assert hud._tk_drain() is True
        hud._tk_destroy()
    assert hud._root is None


@needs_display
def test_tk_scheduled_hide_respects_generation():
    hud = HUD(_start_tk=False, _deiconify=False)
    assert hud._tk_init() is True
    try:
        withdrawn: list[str] = []
        real_withdraw = hud._root.withdraw
        hud._root.withdraw = lambda: (withdrawn.append("w"), real_withdraw())[1]

        hud.show("кратко", hide_after=0.02)      # gen 1, hide armed
        assert hud._tk_drain() is False
        hud.show("постоянно")                     # gen 2 — must survive
        assert hud._tk_drain() is False
        time.sleep(0.04)
        hud._root.update()                        # gen-1 hide timer fires…
        assert withdrawn == []                    # …and does NOT hide gen 2
        hud.hide()                                # explicit hide always hides
        assert hud._tk_drain() is False
        assert withdrawn == ["w"]
    finally:
        hud._tk_destroy()
