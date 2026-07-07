"""macOS insert: clipboard + synthesized Cmd-V (moved from flow/insert.py, W1).

Strategy: save the current clipboard (pbpaste), put the new text on the
clipboard (pbcopy), synthesize Cmd-V with a Quartz CGEvent, then restore
the previous clipboard after a short delay.

If the process has no Accessibility permission (CGEvent posting would be
silently ignored), we do NOT fake success: the text is left on the
clipboard and the user is told to press Cmd-V themselves.
"""

from __future__ import annotations

import subprocess
import time

import Quartz

from ..config import DEFAULT, FlowConfig

KVK_ANSI_V = 9  # macOS virtual keycode for the "V" key


def get_clipboard() -> str:
    """Current clipboard text via pbpaste ('' if empty or non-text)."""
    proc = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
    return proc.stdout if proc.returncode == 0 else ""


def set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text, text=True, timeout=5, check=True)


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
    set_clipboard(text)

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
