"""Windows insert: clipboard + synthesized Ctrl+V — phase W2.

Same public API and return contract as flow/platform_darwin/insert_mac.py:
paste_text ("pasted"/"clipboard-only"/"empty"), get_clipboard, set_clipboard,
can_post_events.

Clipboard: raw Win32 via ctypes (user32/kernel32, CF_UNICODETEXT) — no
pywin32 dependency. Keystroke: pynput's keyboard.Controller, which uses
SendInput on Windows; like the mac CGEvent version we press the modifier
EXPLICITLY (Ctrl down → V down → V up → Ctrl up with small gaps) rather than
relying on whatever modifiers the user still holds. Note the push-to-talk key
is the RIGHT Ctrl and paste posts the LEFT one; the paste runs seconds after
the PTT release, and a re-held right Ctrl only adds Ctrl to Ctrl+V anyway.

There is no TCC equivalent on Windows: SendInput just works, so
can_post_events() is constantly True and the "clipboard-only" branch of
paste_text is kept only for contract parity (it is reachable only if a
future check lands).

The Win32 DLL handles are resolved at CALL time through _dlls() — the module
imports cleanly on macOS, where the unit tests monkeypatch _dlls (and the
tiny _read/_write memory helpers) to verify the exact Win32 call order.
"""

from __future__ import annotations

import ctypes
import time

from ..config import DEFAULT, FlowConfig

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_OPEN_RETRIES = 10          # OpenClipboard fails transiently while another
_OPEN_RETRY_DELAY_S = 0.01  # process holds the clipboard — retry briefly


def _dlls():
    """user32/kernel32 with pointer-safe signatures (resolved per call so the
    module imports on macOS; tests monkeypatch this)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Explicit 64-bit-safe types: the ctypes default int return would truncate
    # HANDLE/HGLOBAL pointers on 64-bit Windows.
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    return user32, kernel32


def _read_wide_string(ptr: int) -> str:
    """NUL-terminated UTF-16 string at ptr (separate helper: mockable)."""
    return ctypes.wstring_at(ptr)


def _copy_into(ptr: int, buf, size: int) -> None:
    """memmove into a GlobalLock'ed block (separate helper: mockable)."""
    ctypes.memmove(ptr, buf, size)


def _open_clipboard(user32) -> bool:
    for attempt in range(_OPEN_RETRIES):
        if user32.OpenClipboard(None):
            return True
        if attempt < _OPEN_RETRIES - 1:
            time.sleep(_OPEN_RETRY_DELAY_S)
    return False


def get_clipboard() -> str:
    """Current clipboard text ('' if empty, non-text, or clipboard busy)."""
    user32, kernel32 = _dlls()
    if not _open_clipboard(user32):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""  # empty clipboard or no text flavor — same as mac ''
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return _read_wide_string(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard(text: str) -> None:
    """Put text on the clipboard (CF_UNICODETEXT). Raises on failure, like
    the mac pbcopy version (subprocess check=True)."""
    user32, kernel32 = _dlls()
    if not _open_clipboard(user32):
        raise RuntimeError("клипбордът е зает от друго приложение (OpenClipboard)")
    handle = None
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)  # includes the trailing NUL
        size = ctypes.sizeof(buf)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise RuntimeError("GlobalAlloc не успя (клипборд)")
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            raise RuntimeError("GlobalLock не успя (клипборд)")
        try:
            _copy_into(ptr, buf, size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise RuntimeError("SetClipboardData не успя")
        handle = None  # ownership passed to the system — must NOT be freed
    finally:
        if handle is not None:
            kernel32.GlobalFree(handle)  # only on failure before handover
        user32.CloseClipboard()


def can_post_events() -> bool:
    """True: Windows has no TCC — SendInput needs no permission grant."""
    return True


def _post_ctrl_v(key_event_delay_s: float) -> None:
    """Synthesize Ctrl+V (SendInput via pynput) with explicit modifier."""
    from pynput.keyboard import Controller, Key, KeyCode

    kb = Controller()
    # VK_V (0x56), never the character "v": pynput resolves characters via
    # VkKeyScan in the process's CURRENT keyboard layout, and on a Cyrillic
    # layout there is no Latin "v" — it falls back to a Unicode packet that
    # no app recognizes as the Ctrl+V accelerator (paste silently no-ops).
    # Accelerators match virtual-key codes, which are layout-independent —
    # same idea as the mac shell posting the physical V key (KVK_ANSI_V).
    v_key = KeyCode.from_vk(0x56)
    # Explicit press/release sequencing (mirrors the mac CGEvent flag care):
    # we own the whole chord instead of composing with held physical keys.
    kb.press(Key.ctrl_l)
    time.sleep(key_event_delay_s)
    kb.press(v_key)
    time.sleep(key_event_delay_s)
    kb.release(v_key)
    time.sleep(key_event_delay_s)
    kb.release(Key.ctrl_l)


def paste_text(text: str, config: FlowConfig = DEFAULT) -> str:
    """Insert `text` into the focused app.

    Returns a status string (the mac contract, verbatim):
      "pasted"         — Ctrl+V posted, previous clipboard restored
      "clipboard-only" — events can't be posted; text left on clipboard
      "empty"          — nothing to insert
    """
    if not text:
        return "empty"

    previous = get_clipboard()
    set_clipboard(text)

    if not can_post_events():  # pragma: no cover - constant True on Windows
        # Do not restore the old clipboard — the dictated text must stay there.
        print("[flow] Не мога да пратя Ctrl+V — текстът е в клипборда, натисни Ctrl+V")
        return "clipboard-only"

    _post_ctrl_v(config.key_event_delay_s)

    # Give the focused app time to read the clipboard before restoring it.
    time.sleep(config.clipboard_restore_delay_s)
    try:
        set_clipboard(previous)
    except Exception:
        pass  # restoring the old clipboard is best-effort
    return "pasted"
