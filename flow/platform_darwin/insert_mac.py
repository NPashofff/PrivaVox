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


KVK_COMMAND = 55   # left ⌘
KVK_CONTROL = 59   # left ⌃ (Control)


def _post_ctrl_v(key_event_delay_s: float) -> None:
    """Synthesize a full Ctrl+V chord: ⌃ down → V down → V up → ⌃ up.

    For REMOTE sessions: the remote Windows pastes with Ctrl+V, and the RDP
    client forwards ⌃ + the V scancode straight through (⌃ is not swapped like
    ⌘, and paste is bound to the physical V position regardless of the remote
    layout's "ж"). So sending the remote's OWN paste shortcut sidesteps the
    "⌘V → remote Paste" redirect entirely — which never fired for our synthetic
    events anyway.
    """
    ctrl = Quartz.kCGEventFlagMaskControl
    steps = (
        (KVK_CONTROL, True, ctrl),   # ⌃ down
        (KVK_ANSI_V, True, ctrl),    # V down (with ⌃ held)
        (KVK_ANSI_V, False, ctrl),   # V up
        (KVK_CONTROL, False, 0),     # ⌃ up
    )
    for keycode, key_down, flags in steps:
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, key_down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(key_event_delay_s)


def _post_cmd_v(key_event_delay_s: float) -> None:
    """Synthesize a full ⌘V chord: ⌘ down → V down → V up → ⌘ up.

    Posting V with only the Command FLAG (and no ⌘ key event) works for local
    apps, but a Remote Desktop client tracks the modifier via real key events
    and, seeing no ⌘ held, forwards a bare "V" scancode → "ж" on a Bulgarian
    remote layout. Pressing ⌘ as an explicit key makes the client recognize
    ⌘V and apply its "redirect ⌘V → remote Paste" mapping. Explicit flags keep
    a still-held push-to-talk key from turning this into ⌘⌥V.
    """
    cmd = Quartz.kCGEventFlagMaskCommand
    steps = (
        (KVK_COMMAND, True, cmd),   # ⌘ down
        (KVK_ANSI_V, True, cmd),    # V down (with ⌘ held)
        (KVK_ANSI_V, False, cmd),   # V up
        (KVK_COMMAND, False, 0),    # ⌘ up
    )
    for keycode, key_down, flags in steps:
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, key_down)
        Quartz.CGEventSetFlags(event, flags)
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


def paste_remote(text: str, config: FlowConfig = DEFAULT) -> str:
    """Stage `text` for a Remote Desktop session; the user pastes with a
    PHYSICAL Ctrl+V.

    Copy only — DON'T synthesize a paste. Proven live: "Windows App" forwards
    our synthetic ⌘V / Ctrl+V as a bare key (ж / v / V), never as a shortcut,
    so auto-paste only prepends garbage. A physical Ctrl+V, however, is
    redirected to the remote Paste and inserts the (already-synced) text.

    The catch is the SYNC. Read out of Windows App 11.4 itself: it has NO
    pasteboard timer. It looks at the Mac pasteboard in exactly two places —
      • window RESIGNS key: if the remote clipboard has news and the Mac
        pasteboard is unchanged, it writes the remote content onto the Mac
        pasteboard; if the Mac pasteboard HAS changed it logs "updated by
        another application" and leaves that remote news PENDING;
      • window BECOMES key: if changeCount differs from the one it last saw,
        it announces the Mac pasteboard to the remote.
    So the only sequence the client is built for is the human one: switch
    away → copy elsewhere → switch back. We reproduce exactly that: deactivate
    the client, let it finish its resign work, copy the text ONLY THEN, and
    reactivate. Copying BEFORE the deactivation (what this did until 0.1.27)
    walks the client into the "updated by another application" branch — fine
    on a fresh session, but once something was copied on the remote side the
    next Ctrl+V pasted that remote content (old text, even a picture) instead
    of the dictation.

    Returns "clipboard-only" (staged), "clipboard-error" (clipboard hung) or
    "empty". Never restores the old clipboard: the remote fetch is lazy and
    may happen minutes later.
    """
    if not text:
        return "empty"
    cc0 = _pb_change_count()
    try:
        _stage_remote(text, config)
    except Exception as e:
        # A stuck/redirected clipboard (RDP) can hang pbcopy — surface it
        # instead of reporting a green "ready for Ctrl+V" that would paste
        # the previous dictation.
        print(f"[flow] клипбордът не отговори (pbcopy, remote): {e!r}")
        return "clipboard-error"
    cc1 = _pb_change_count()
    time.sleep(config.remote_clipboard_settle_s)
    # VERIFY the staged text survived: the client's resign work is async (it
    # goes through an XPC helper), so a late remote→Mac write can still land
    # on top of ours. A clobbered pasteboard is exactly "Ctrl+V pastes the
    # old content", so cycle once more.
    verified = get_clipboard() == text
    if not verified and config.remote_reactivate_nudge:
        try:
            _stage_remote(text, config)
        except Exception:
            return "clipboard-error"
        time.sleep(config.remote_clipboard_settle_s)
        verified = get_clipboard() == text
    cc2 = _pb_change_count()
    print(f"[flow] remote clipboard: changeCount {cc0}→{cc1}→{cc2} verified={verified}")
    if not verified:
        # Last write wins — at least the Mac side ends up holding the text.
        try:
            set_clipboard(text)
        except Exception:
            return "clipboard-error"
    return "clipboard-only"


def _stage_remote(text: str, config: FlowConfig) -> None:
    """Put `text` on the pasteboard — inside a focus cycle of the RDP client
    when the nudge is enabled, so the copy happens while the client is
    deactivated. Raises if the copy itself fails."""
    if not config.remote_reactivate_nudge:
        set_clipboard(text)
        return
    outcome: dict[str, object] = {}

    def stage() -> None:
        try:
            set_clipboard(text)
            outcome["ok"] = True
        except Exception as e:  # reported after the client has its focus back
            outcome["err"] = e

    _nudge_frontmost_reactivate(stage)
    if "err" in outcome:
        raise outcome["err"]  # type: ignore[misc]
    if "ok" not in outcome:
        set_clipboard(text)  # the cycle bailed out before staging


_NUDGE_POLL_S = 0.05
# After the client is deactivated: give its resign handler at least this long
# to start (it is dispatched async and writes through an XPC helper) …
_NUDGE_RESIGN_MIN_S = 0.2
# … then require the pasteboard to stay untouched this long before we copy …
_NUDGE_QUIET_S = 0.15
# … but never wait longer than this in total.
_NUDGE_RESIGN_MAX_S = 1.0
# Stay deactivated this long after the copy, so the pasteboard server has
# committed it before the client compares changeCount on activation.
_NUDGE_DWELL_S = 0.15


def _pb_change_count() -> int:
    """NSPasteboard change counter — bumps on EVERY write, ours or anyone
    else's. Logged so an external clipboard writer is visible."""
    try:
        import AppKit

        return int(AppKit.NSPasteboard.generalPasteboard().changeCount())
    except Exception:
        return -1


def _frontmost_pid() -> int:
    import AppKit

    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(front.processIdentifier()) if front is not None else -1


def _wait_frontmost(pid: int, want_front: bool, timeout_s: float) -> bool:
    """Poll until `pid` is (want_front=True) / is no longer (False) the
    frontmost app. True on success, False on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        if (_frontmost_pid() == pid) == want_front:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_NUDGE_POLL_S)


def _wait_pasteboard_quiet() -> int:
    """Wait for the deactivated client to finish touching the Mac pasteboard
    (it may write the remote clipboard onto it). Returns how many foreign
    writes were seen — logged, because each one is a remote→Mac sync that
    would have clobbered a text staged too early."""
    start = time.monotonic()
    last = _pb_change_count()
    last_change = start
    writes = 0
    while True:
        time.sleep(0.03)
        now = time.monotonic()
        cc = _pb_change_count()
        if cc != last:
            last, last_change, writes = cc, now, writes + 1
        elapsed = now - start
        if elapsed >= _NUDGE_RESIGN_MAX_S:
            return writes
        if elapsed >= _NUDGE_RESIGN_MIN_S and now - last_change >= _NUDGE_QUIET_S:
            return writes


def _hand_focus_back(front) -> None:
    """Reactivate `front` so that its window BECOMES KEY again.

    Measured on macOS 27 with a probe app: a bare
    front.activateWithOptions_() from here makes the app active
    (NSApplicationDidBecomeActive, frontmostApplication flips back — so every
    check we log says "reactivated") but its window never gets
    NSWindowDidBecomeKey. Windows App announces the Mac clipboard ONLY from
    that notification, which is why the whole cycle looked perfect in the log
    and still synced nothing. The cooperative hand-over (yield activation,
    then activate *from* us) restores the key window within ~10 ms."""
    import AppKit

    def go() -> None:
        try:
            app = AppKit.NSApplication.sharedApplication()
            app.yieldActivationToApplication_(front)
            front.activateFromApplication_options_(
                AppKit.NSRunningApplication.currentApplication(), 0)
        except Exception:  # macOS < 14: no cooperative activation API
            front.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(go)  # NSApp wants the main thread
    except Exception:
        go()


def _nudge_frontmost_reactivate(stage=None) -> None:
    """Take the RDP client through a REAL deactivate→reactivate cycle and run
    `stage` (the copy) in the middle — the user's "switch away, copy, switch
    back", scripted. See paste_remote for why the order matters. Best-effort:
    never raises, and `stage` is called at most once (not at all only if the
    cycle fails before reaching it — the caller then copies directly).

    Deactivation lever: activating PrivaVox ITSELF. It is an accessory app
    with no regular windows, so nothing visible happens, there is no Space
    switch, and it works on a fullscreen client (macOS silently ignores Hide
    for fullscreen apps, and RDP sessions typically run fullscreen). hide()
    stays as the windowed fallback, and every step is VERIFIED by polling the
    frontmost app instead of sleeping blind."""
    try:
        import AppKit

        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return
        pid = int(front.processIdentifier())

        def self_activate() -> None:
            app = AppKit.NSApplication.sharedApplication()
            app.activateIgnoringOtherApps_(True)

        try:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(self_activate)  # NSApp wants the main thread
        except Exception:
            self_activate()
        deactivated = _wait_frontmost(pid, want_front=False, timeout_s=0.8)
        if not deactivated:
            front.hide()  # windowed sessions; fullscreen ignores Hide
            deactivated = _wait_frontmost(pid, want_front=False, timeout_s=0.8)
        foreign = _wait_pasteboard_quiet() if deactivated else 0
        if stage is not None:
            stage()
        time.sleep(_NUDGE_DWELL_S)
        front.unhide()  # undo the fallback hide(); no-op otherwise
        _hand_focus_back(front)
        reactivated = _wait_frontmost(pid, want_front=True, timeout_s=1.0)
        if not reactivated:
            # Never strand the user without keyboard focus — one more try,
            # the blunt way.
            front.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            reactivated = _wait_frontmost(pid, want_front=True, timeout_s=1.0)
        print(f"[flow] remote nudge: deactivated={deactivated} "
              f"remote_writes={foreign} reactivated={reactivated}")
    except Exception as e:
        print(f"[flow] remote reactivate nudge failed: {e!r}")


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
