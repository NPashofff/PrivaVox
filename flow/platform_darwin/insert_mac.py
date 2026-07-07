"""macOS insert: clipboard + synthesized Cmd-V (moved from flow/insert.py, W1).

Strategy: save the current clipboard (pbpaste), put the new text on the
clipboard (pbcopy), synthesize Cmd-V with a Quartz CGEvent, then restore
the previous clipboard after a short delay.

If the process has no Accessibility permission (CGEvent posting would be
silently ignored), we do NOT fake success: the text is left on the
clipboard and the user is told to press Cmd-V themselves.
"""

from __future__ import annotations

import os
import subprocess
import time

import Quartz

from ..config import DEFAULT, FlowConfig

KVK_ANSI_V = 9  # macOS virtual keycode for the "V" key


# pbcopy/pbpaste interpret their bytes per the process LOCALE (LC_CTYPE), not
# UTF-8 unconditionally. If the app is launched in a non-UTF-8 locale (C/POSIX,
# or a bogus value like LANG=bg), pbcopy stores UTF-8 bytes AS MacRoman →
# Cyrillic becomes mojibake ("Проба" → "–ü—Ä–æ–±–∞"). Force a UTF-8 locale and
# pass/read explicit UTF-8 bytes so insertion is encoding-safe in ANY env.
_UTF8_ENV = {**os.environ, "LC_ALL": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8"}


# 10 s, not 5: a Remote Desktop / "Windows App" session redirects the clipboard
# over the network, so pbpaste/pbcopy can be slow. Reading the old clipboard is
# best-effort — a timeout there must NEVER crash the dictation (it did: pbpaste
# TimeoutExpired propagated to the worker as "Грешка при обработка").
_CLIP_TIMEOUT = 10


def get_clipboard() -> str:
    """Current clipboard text via pbpaste ('' if empty, non-text, or slow)."""
    try:
        proc = subprocess.run(["pbpaste"], capture_output=True, env=_UTF8_ENV,
                              timeout=_CLIP_TIMEOUT)
        return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else ""
    except Exception:  # TimeoutExpired / OSError — old clipboard is best-effort
        return ""


def set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=_UTF8_ENV,
                   timeout=_CLIP_TIMEOUT, check=True)


def can_post_events() -> bool:
    """True if macOS will deliver CGEvents we post (Accessibility permission)."""
    try:
        preflight = getattr(Quartz, "CGPreflightPostEventAccess", None)
        if preflight is not None:
            return bool(preflight())
        from ApplicationServices import AXIsProcessTrusted  # fallback, older pyobjc
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def _post_cmd_v(key_event_delay_s: float) -> None:
    """Synthesize Cmd-V (key down + key up) via the HID event tap."""
    for key_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, KVK_ANSI_V, key_down)
        # Set flags explicitly so a still-held physical modifier (e.g. the
        # push-to-talk Option key) cannot turn this into Cmd-Opt-V.
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(key_event_delay_s)


# CGEventKeyboardSetUnicodeString carries at most 20 UTF-16 code units per
# keyboard event — longer strings must be streamed in chunks.
_TYPE_CHUNK_UTF16 = 20


def _utf16_chunks(text: str, limit: int = _TYPE_CHUNK_UTF16) -> list[str]:
    """Split text into pieces of ≤`limit` UTF-16 code units without splitting
    a surrogate pair (astral chars like emoji occupy two units)."""
    chunks: list[str] = []
    cur, cur_units = [], 0
    for ch in text:
        units = len(ch.encode("utf-16-le")) // 2  # 1, or 2 for astral chars
        if cur and cur_units + units > limit:
            chunks.append("".join(cur))
            cur, cur_units = [], 0
        cur.append(ch)
        cur_units += units
    if cur:
        chunks.append("".join(cur))
    return chunks


def type_text(text: str, config: FlowConfig = DEFAULT) -> str:
    """Insert `text` by TYPING it as synthetic Unicode keystrokes (the Wispr
    Flow / openless technique). Unlike clipboard+Cmd-V this crosses Remote
    Desktop / VNC boundaries: keystrokes are forwarded to the remote session
    as Unicode input, no clipboard sync required.

    ONE CHARACTER PER EVENT: macOS itself accepts up to 20 UTF-16 units per
    keyboard event, but RDP clients translate each event to a protocol input
    carrying a SINGLE unicode char — a 20-char event arrives remotely as just
    its first character (verified live: only "А" appeared). Per-char streaming
    is what pynput/Wispr do, and it works both locally and through RDP.

    Returns "typed" on success, "clipboard-only" when events can't be posted
    (no Accessibility) — the caller decides what to stage on the clipboard.
    """
    if not text:
        return "empty"
    if not can_post_events():
        return "clipboard-only"
    for ch in text:  # str iteration yields code points → surrogate pairs stay whole
        units = len(ch.encode("utf-16-le")) // 2  # 1, or 2 for astral chars
        for key_down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, key_down)
            # No modifier flags: a still-held push-to-talk key must not turn
            # the typed characters into shortcuts.
            Quartz.CGEventSetFlags(event, 0)
            Quartz.CGEventKeyboardSetUnicodeString(event, units, ch)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(config.type_chunk_delay_s)
    return "typed"


def paste_text(text: str, config: FlowConfig = DEFAULT) -> str:
    """Insert `text` into the focused app.

    Returns a status string:
      "pasted"         — Cmd-V posted, previous clipboard restored
      "clipboard-only" — no Accessibility permission; text left on clipboard
      "empty"          — nothing to insert
    """
    if not text:
        return "empty"

    previous = get_clipboard()
    try:
        set_clipboard(text)
    except Exception as e:
        # A stuck/redirected clipboard (RDP) can hang pbcopy — don't crash the
        # dictation; report it so the shell shows a clear message.
        print(f"[flow] клипбордът не отговори (pbcopy): {e!r}")
        return "clipboard-only"

    if not can_post_events():
        # Do not restore the old clipboard — the dictated text must stay there.
        print("[flow] Няма Accessibility права за симулиран Cmd-V.")
        print("[flow] текстът е в клипборда — натисни Cmd-V")
        print("[flow] (Разреши достъпа: System Settings -> Privacy & Security -> Accessibility.)")
        return "clipboard-only"

    _post_cmd_v(config.key_event_delay_s)

    # Give the focused app time to read the clipboard before restoring it.
    time.sleep(config.clipboard_restore_delay_s)
    try:
        set_clipboard(previous)
    except Exception:
        pass  # restoring the old clipboard is best-effort
    return "pasted"
