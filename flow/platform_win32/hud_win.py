"""Tkinter dictation HUD — phase W2. Same public API as flow/hud.py (mac).

A borderless, always-on-top dark pill at the bottom-center of the primary
screen. Colors: white/red/green/orange. Width follows the text.

Threading pattern (document-worthy because tkinter is NOT thread-safe):
Tk objects may only be touched by the thread that created them. So on
Windows the HUD runs a DEDICATED daemon thread that owns the Tk root and its
mainloop; every public call (show/hide/close, callable from any thread) only
bumps the generation counter and puts a command on a queue.Queue. The Tk
thread drains that queue from a `root.after(poll_ms, …)` self-rescheduling
poll — the after-callback runs on the Tk thread, so all widget access stays
there. Delayed hides are `root.after` timers armed on the Tk thread.

macOS testing note: Tk's Aqua backend aborts the PROCESS if a window is
created off the main thread ("NSWindow should only be instantiated on the
main thread"), so the worker-thread mode is Windows-only. For the mac test
suite the Tk half is factored into explicit methods (_tk_init/_tk_drain/
_tk_destroy) that the smoke test drives synchronously on the MAIN thread
with `_start_tk=False` — same code paths, no thread. The thread+Tk combo
itself is real-Windows-only behavior (W4 checklist).

Generation counter semantics (identical to flow/hud.py):
- every show()/hide() bumps the generation under a lock;
- a queued show whose generation is stale by drain time draws nothing;
- a scheduled hide fires only if its generation is still current — a timer
  armed for an old message can never take down a newer (possibly
  persistent) one.

If Tk cannot initialize (headless machine / no display) the HUD prints one
loud line and degrades to a no-op — a missing overlay must never take the
dictation pipeline down.
"""

from __future__ import annotations

import queue
import threading

_H = 44
_MIN_W = 220
_EDGE_GAP = 80        # min horizontal air between pill and screen edges
_TEXT_PAD = 16        # label inset inside the pill (each side)
_BOTTOM_MARGIN = 96

_PILL_BG = "#141414"
_ALPHA = 0.93
# Any color no real pill will ever use; on Windows it becomes see-through
# (-transparentcolor), producing true rounded corners.
_TRANSPARENT_KEY = "#0b0c0d"

_COLORS = {
    "white": "#ffffff",
    "red": "#ff5f57",
    "green": "#32d74b",
    "orange": "#ffa028",
}

_QUIT_JOIN_S = 3.0


class HUD:
    """show(text, color="white", hide_after=None) / hide(), any thread.

    Test seams (used by tests/test_hud_win.py, harmless in production):
      _start_tk=False   — never spawn the Tk thread; tests either inspect the
                          queue directly or drive _tk_init/_tk_drain on the
                          main thread (the only Tk-capable thread on macOS).
      _deiconify=False  — full Tk pipeline but the window stays withdrawn
                          (smoke tests without flashing a window).
    """

    def __init__(self, poll_ms: int = 50, *, _start_tk: bool = True,
                 _deiconify: bool = True) -> None:
        self._poll_ms = poll_ms
        self._start_tk = _start_tk
        self._deiconify = _deiconify
        self._gen = 0                       # bumped by every show()/hide()
        self._gen_lock = threading.Lock()
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._dead = False                  # Tk failed to start: no-op mode
        # Tk state — created by _tk_init on the Tk-owner thread, None before.
        self._root = None
        self._canvas = None
        self._font = None
        self._transparent = False

    # ---- thread-safe API -----------------------------------------------------

    def show(self, text: str, color: str = "white", hide_after: float | None = None) -> None:
        if self._dead:
            return
        with self._gen_lock:
            self._gen += 1
            gen = self._gen
        self._queue.put(("show", gen, text, color, hide_after))
        self._ensure_thread()

    def hide(self) -> None:
        if self._dead:
            return
        with self._gen_lock:
            self._gen += 1  # invalidates every scheduled hide as well
        self._queue.put(("hide",))
        # no _ensure_thread(): hiding an overlay that never existed needs no
        # Tk; the command drains (as a no-op) when a show starts the thread

    def close(self) -> None:
        """Tear the Tk thread down (tests / graceful quit); safe to re-call."""
        self._queue.put(("quit",))
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_QUIT_JOIN_S)

    # ---- internals -------------------------------------------------------------

    def _is_current(self, gen: int) -> bool:
        with self._gen_lock:
            return gen == self._gen

    def _ensure_thread(self) -> None:
        if not self._start_tk:
            return
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="flow-hud-tk", daemon=True
                )
                self._thread.start()

    # ---- Tk side (every method below runs ONLY on the Tk-owner thread:
    # ---- self._thread in production, the main thread in the mac tests) ---------

    def _tk_init(self) -> bool:
        """Create the root/canvas/font. True on success; False → no-op mode."""
        try:
            import tkinter as tk
            import tkinter.font as tkfont

            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)          # borderless
            try:
                root.attributes("-topmost", True)
                root.attributes("-alpha", _ALPHA)
            except tk.TclError:
                pass
            try:  # Windows-only: rounded corners via a color key
                root.configure(bg=_TRANSPARENT_KEY)
                root.attributes("-transparentcolor", _TRANSPARENT_KEY)
                self._transparent = True
            except tk.TclError:
                root.configure(bg=_PILL_BG)      # square-ish fallback (mac dev)
            canvas = tk.Canvas(
                root, height=_H, width=_MIN_W, highlightthickness=0, bd=0,
                bg=_TRANSPARENT_KEY if self._transparent else _PILL_BG,
            )
            canvas.pack()
            self._root, self._canvas = root, canvas
            self._font = tkfont.Font(family="Segoe UI", size=12, weight="bold")
            return True
        except Exception as e:
            print(f"[flow.hud] Tkinter HUD недостъпен ({e!r}) — продължавам без пил")
            self._dead = True
            return False

    def _tk_draw(self, text: str, color_name: str) -> None:
        """Size the pill to `text`, redraw it, bottom-center it on the screen."""
        root, canvas = self._root, self._canvas
        needed = self._font.measure(text) + 2 * _TEXT_PAD
        w = max(_MIN_W, min(needed, root.winfo_screenwidth() - _EDGE_GAP))
        canvas.delete("all")
        canvas.configure(width=w)
        r = _H / 2.0  # pill = two half-circles + a joining rectangle
        canvas.create_oval(0, 0, 2 * r, _H, fill=_PILL_BG, outline="")
        canvas.create_oval(w - 2 * r, 0, w, _H, fill=_PILL_BG, outline="")
        canvas.create_rectangle(r, 0, w - r, _H, fill=_PILL_BG, outline="")
        canvas.create_text(
            w / 2, _H / 2, text=text,
            fill=_COLORS.get(color_name, _COLORS["white"]), font=self._font,
        )
        x = (root.winfo_screenwidth() - w) // 2
        y = root.winfo_screenheight() - _BOTTOM_MARGIN - _H
        root.geometry(f"{int(w)}x{_H}+{int(x)}+{int(y)}")

    def _tk_hide_if_current(self, gen: int) -> None:
        if self._is_current(gen):
            self._root.withdraw()

    def _tk_drain(self) -> bool:
        """One pass over the command queue. Returns True when quitting."""
        while True:
            try:
                cmd = self._queue.get_nowait()
            except queue.Empty:
                return False
            if cmd[0] == "quit":
                return True
            if cmd[0] == "hide":
                self._root.withdraw()
                continue
            _, gen, text, color_name, hide_after = cmd
            if not self._is_current(gen):
                continue  # superseded by a newer show()/hide() before drawing
            self._tk_draw(text, color_name)
            if self._deiconify:
                self._root.deiconify()
                self._root.lift()
                try:
                    self._root.attributes("-topmost", True)
                except Exception:
                    pass
            if hide_after is not None:
                self._root.after(int(hide_after * 1000),
                                 lambda g=gen: self._tk_hide_if_current(g))

    def _tk_destroy(self) -> None:
        root, self._root, self._canvas, self._font = self._root, None, None, None
        if root is not None:
            root.destroy()

    def _run(self) -> None:
        """Thread target (Windows): own the Tk root, poll the queue, mainloop."""
        if not self._tk_init():
            return

        def poll() -> None:
            if self._tk_drain():
                self._tk_destroy()
                return
            self._root.after(self._poll_ms, poll)

        self._root.after(self._poll_ms, poll)
        try:
            self._root.mainloop()
        except Exception as e:  # pragma: no cover - display torn down under us
            print(f"[flow.hud] Tk mainloop падна: {e!r}")
            self._dead = True
