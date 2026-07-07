"""W2: insert_win contract tests — every Win32 call mocked (runs on macOS).

flow/platform_win32/insert_win.py resolves user32/kernel32 through _dlls()
at call time, so these tests swap in recording fakes and verify the EXACT
Win32 choreography: open/close pairing, CF_UNICODETEXT, EmptyClipboard before
SetClipboardData, GlobalAlloc sizing, handle ownership handover, and the
paste_text status contract shared with the mac implementation.
"""

from __future__ import annotations

import pytest

from flow.config import FlowConfig
from flow.platform_win32 import insert_win

CF_UNICODETEXT = insert_win.CF_UNICODETEXT


class FakeUser32:
    def __init__(self, log, *, open_results=None, clipboard_handle=7001,
                 set_result=1):
        self.log = log
        self._open_results = list(open_results) if open_results is not None else [1]
        self.clipboard_handle = clipboard_handle
        self.set_result = set_result
        self.set_calls: list[tuple[int, object]] = []

    def OpenClipboard(self, owner):
        result = self._open_results.pop(0) if self._open_results else 1
        self.log.append(("OpenClipboard", owner, result))
        return result

    def CloseClipboard(self):
        self.log.append(("CloseClipboard",))
        return 1

    def GetClipboardData(self, fmt):
        self.log.append(("GetClipboardData", fmt))
        return self.clipboard_handle

    def EmptyClipboard(self):
        self.log.append(("EmptyClipboard",))
        return 1

    def SetClipboardData(self, fmt, handle):
        self.log.append(("SetClipboardData", fmt, handle))
        self.set_calls.append((fmt, handle))
        return self.set_result


class FakeKernel32:
    def __init__(self, log, *, lock_ptr=0xBEEF, alloc_handle=4242):
        self.log = log
        self.lock_ptr = lock_ptr
        self.alloc_handle = alloc_handle
        self.alloc_sizes: list[int] = []

    def GlobalLock(self, handle):
        self.log.append(("GlobalLock", handle))
        return self.lock_ptr

    def GlobalUnlock(self, handle):
        self.log.append(("GlobalUnlock", handle))
        return 1

    def GlobalAlloc(self, flags, size):
        self.log.append(("GlobalAlloc", flags, size))
        self.alloc_sizes.append(size)
        return self.alloc_handle

    def GlobalFree(self, handle):
        self.log.append(("GlobalFree", handle))
        return None


@pytest.fixture()
def win(monkeypatch):
    """Install fakes; returns (log, user32, kernel32) with hooks patched."""
    log: list[tuple] = []
    user32 = FakeUser32(log)
    kernel32 = FakeKernel32(log)
    monkeypatch.setattr(insert_win, "_dlls", lambda: (user32, kernel32))
    monkeypatch.setattr(insert_win, "_read_wide_string", lambda ptr: "предишен текст")
    copied: list[tuple] = []
    monkeypatch.setattr(insert_win, "_copy_into",
                        lambda ptr, buf, size: copied.append((ptr, size)))
    return log, user32, kernel32, copied


# --------------------------------------------------------------------------
# get_clipboard
# --------------------------------------------------------------------------

def test_get_clipboard_order_and_value(win):
    log, user32, kernel32, _ = win
    assert insert_win.get_clipboard() == "предишен текст"
    names = [entry[0] for entry in log]
    assert names == ["OpenClipboard", "GetClipboardData", "GlobalLock",
                     "GlobalUnlock", "CloseClipboard"]
    assert ("GetClipboardData", CF_UNICODETEXT) in log
    assert ("GlobalLock", user32.clipboard_handle) in log


def test_get_clipboard_empty_when_no_text_flavor(win):
    log, user32, _, _ = win
    user32.clipboard_handle = 0  # no CF_UNICODETEXT on the clipboard
    assert insert_win.get_clipboard() == ""
    assert [e[0] for e in log] == ["OpenClipboard", "GetClipboardData", "CloseClipboard"]


def test_get_clipboard_empty_when_clipboard_busy(win, monkeypatch):
    log, user32, _, _ = win
    monkeypatch.setattr(insert_win, "_OPEN_RETRY_DELAY_S", 0.0)
    user32._open_results = [0] * insert_win._OPEN_RETRIES  # always busy
    assert insert_win.get_clipboard() == ""
    # retried, then gave up WITHOUT CloseClipboard (nothing was opened)
    assert [e[0] for e in log].count("OpenClipboard") == insert_win._OPEN_RETRIES
    assert "CloseClipboard" not in [e[0] for e in log]


def test_open_clipboard_retries_transient_busy(win, monkeypatch):
    log, user32, _, _ = win
    monkeypatch.setattr(insert_win, "_OPEN_RETRY_DELAY_S", 0.0)
    user32._open_results = [0, 0, 1]  # busy, busy, open
    assert insert_win.get_clipboard() == "предишен текст"
    assert [e[0] for e in log].count("OpenClipboard") == 3


# --------------------------------------------------------------------------
# set_clipboard
# --------------------------------------------------------------------------

def test_set_clipboard_order_sizing_and_handover(win):
    log, user32, kernel32, copied = win
    text = "здравей 123"
    insert_win.set_clipboard(text)
    names = [e[0] for e in log]
    assert names == ["OpenClipboard", "EmptyClipboard", "GlobalAlloc",
                     "GlobalLock", "GlobalUnlock", "SetClipboardData",
                     "CloseClipboard"]
    # UTF-16 sizing: (len + trailing NUL) * sizeof(wchar); wchar is 2 bytes on
    # Windows but platform-dependent under ctypes — assert in wchar units.
    import ctypes
    assert kernel32.alloc_sizes == [(len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)]
    assert copied and copied[0][0] == kernel32.lock_ptr
    assert user32.set_calls == [(CF_UNICODETEXT, kernel32.alloc_handle)]
    assert "GlobalFree" not in names  # ownership passed to the system


def test_set_clipboard_frees_handle_when_set_fails(win):
    log, user32, kernel32, _ = win
    user32.set_result = 0  # SetClipboardData failure
    with pytest.raises(RuntimeError):
        insert_win.set_clipboard("x")
    names = [e[0] for e in log]
    assert ("GlobalFree", kernel32.alloc_handle) in log
    assert names[-1] == "CloseClipboard"  # clipboard never left open


def test_set_clipboard_raises_when_busy(win, monkeypatch):
    log, user32, _, _ = win
    monkeypatch.setattr(insert_win, "_OPEN_RETRY_DELAY_S", 0.0)
    user32._open_results = [0] * insert_win._OPEN_RETRIES
    with pytest.raises(RuntimeError):
        insert_win.set_clipboard("x")


# --------------------------------------------------------------------------
# paste_text: the cross-platform status contract
# --------------------------------------------------------------------------

@pytest.fixture()
def paste_env(monkeypatch):
    calls: list[tuple] = []
    clipboard = {"value": "старият клипборд"}
    monkeypatch.setattr(insert_win, "get_clipboard",
                        lambda: (calls.append(("get",)), clipboard["value"])[1])

    def fake_set(text):
        calls.append(("set", text))
        clipboard["value"] = text

    monkeypatch.setattr(insert_win, "set_clipboard", fake_set)
    monkeypatch.setattr(insert_win, "_post_ctrl_v",
                        lambda delay: calls.append(("ctrl_v", delay)))
    config = FlowConfig(clipboard_restore_delay_s=0.0, key_event_delay_s=0.0)
    return calls, clipboard, config


def test_paste_text_happy_path(paste_env):
    calls, clipboard, config = paste_env
    assert insert_win.paste_text("новият текст", config) == "pasted"
    assert calls == [
        ("get",),                      # save the previous clipboard…
        ("set", "новият текст"),       # …put ours on…
        ("ctrl_v", 0.0),               # …paste…
        ("set", "старият клипборд"),   # …restore the original
    ]
    assert clipboard["value"] == "старият клипборд"


def test_paste_text_empty(paste_env):
    calls, _, config = paste_env
    assert insert_win.paste_text("", config) == "empty"
    assert calls == []  # clipboard untouched


def test_paste_text_restore_is_best_effort(paste_env, monkeypatch):
    calls, _, config = paste_env
    state = {"n": 0}

    def flaky_set(text):
        state["n"] += 1
        if state["n"] == 2:  # the restore call
            raise RuntimeError("клипбордът е зает")
        calls.append(("set", text))

    monkeypatch.setattr(insert_win, "set_clipboard", flaky_set)
    assert insert_win.paste_text("текст", config) == "pasted"  # not raised


def test_can_post_events_is_true_on_windows_contract():
    # No TCC on Windows: never a "clipboard-only" degradation by permission.
    assert insert_win.can_post_events() is True


def test_post_ctrl_v_uses_virtual_key_not_character(monkeypatch):
    """The V must go out as VK 0x56, never the character "v": on a Cyrillic
    keyboard layout VkKeyScan("v") fails and pynput would inject a Unicode
    packet that no app recognizes as the Ctrl+V accelerator (the paste then
    silently no-ops — the original Windows 'нищо не вмъква' bug)."""
    import pynput.keyboard as pk

    events: list[tuple] = []

    class FakeController:
        def press(self, key):
            events.append(("press", key))

        def release(self, key):
            events.append(("release", key))

    monkeypatch.setattr(pk, "Controller", FakeController)
    insert_win._post_ctrl_v(0.0)

    v_key = pk.KeyCode.from_vk(0x56)
    assert events == [
        ("press", pk.Key.ctrl_l),
        ("press", v_key),
        ("release", v_key),
        ("release", pk.Key.ctrl_l),
    ]
