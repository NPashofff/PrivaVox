"""Push-to-talk hotkey handling via pynput.

Hold the configured key (default: right Option) to record, release to
process. Press Esc while recording to cancel. Debouncing of accidental
taps is done by the caller comparing the held duration against
config.min_recording_s (the listener reports how long the key was held).

macOS note: a keyboard listener needs Input Monitoring (and in practice
Accessibility) permission for the hosting process, e.g. your terminal app.
"""

from __future__ import annotations

import time
from typing import Callable

from pynput import keyboard

from .config import DEFAULT, FlowConfig
from .platform_impl import IS_MAC

_KEY_ALIASES: dict[str, keyboard.Key] = {
    "alt_r": keyboard.Key.alt_r,      # right Option (mac) / right Alt=AltGr (win)
    "alt_l": keyboard.Key.alt_l,
    "cmd_r": keyboard.Key.cmd_r,
    "ctrl_r": keyboard.Key.ctrl_r,    # the Windows default (right Alt is AltGr on BG layouts)
    "f12": keyboard.Key.f12,
    "f13": keyboard.Key.f13,          # legacy: settings.json saved before the F12 switch
}


# Every accepted config.hotkey value (what the shells' settings loaders and
# the hotkey-picker menus validate against).
VALID_HOTKEYS: frozenset[str] = frozenset(_KEY_ALIASES) | {"auto"}


def resolve_hotkey_name(name: str) -> str:
    """Canonical key name: "auto" becomes the platform default alias."""
    if name == "auto":
        # Platform default: right Option on macOS (identical to the pre-W2
        # fixed default), right Ctrl on Windows — see FlowConfig.hotkey.
        return "alt_r" if IS_MAC else "ctrl_r"
    if name not in _KEY_ALIASES:
        raise ValueError(
            f"Unsupported hotkey {name!r}; 'auto' or one of {sorted(_KEY_ALIASES)}"
        )
    return name


def resolve_hotkey(name: str) -> keyboard.Key:
    return _KEY_ALIASES[resolve_hotkey_name(name)]


class PushToTalk:
    """Listener wiring: hold hotkey -> on_start; release -> on_stop(held_s); Esc -> on_cancel."""

    def __init__(
        self,
        on_start: Callable[[], None],
        on_stop: Callable[[float], None],
        on_cancel: Callable[[], None],
        config: FlowConfig = DEFAULT,
    ) -> None:
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_cancel = on_cancel
        self._hotkey = resolve_hotkey(config.hotkey)
        self._pressed_at: float | None = None
        self._listener: keyboard.Listener | None = None

    @property
    def recording(self) -> bool:
        return self._pressed_at is not None

    def _handle_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key == self._hotkey and self._pressed_at is None:
            self._pressed_at = time.monotonic()
            self._on_start()
        elif key == keyboard.Key.esc and self._pressed_at is not None:
            self._pressed_at = None
            self._on_cancel()

    def _handle_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key == self._hotkey and self._pressed_at is not None:
            held = time.monotonic() - self._pressed_at
            self._pressed_at = None
            self._on_stop(held)

    def run_forever(self) -> None:
        """Block the calling thread on the listener (Ctrl-C to exit)."""
        with keyboard.Listener(
            on_press=self._handle_press, on_release=self._handle_release
        ) as listener:
            self._listener = listener
            listener.join()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
