"""W4 finding 2 — the "Клавиш за диктовка" picker: persistence + live swap.

The menus themselves need a real menu bar / tray (W4 on Windows, manual on
mac), but everything under them is testable headless:

- resolve_hotkey_name / VALID_HOTKEYS in flow/hotkey.py
- the "hotkey" settings round-trip of the MAC shell (module-level
  load_settings/save_settings in flow/app.py; the win32 shell's merge-write
  variant is covered in test_win32_shell.py)
- set_hotkey's live-restart logic in BOTH shells, by calling the real
  (unbound) methods on a minimal stub instance with flow.hotkey.PushToTalk
  mocked: old listener stopped, replacement built with the new key on a
  fresh thread, callbacks preserved, recording guard honored.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import Mock

import pytest
from pynput import keyboard

import flow.hotkey as hotkey
from flow.config import FlowConfig

flow_app = pytest.importorskip(
    "flow.app", reason="mac shell deps (rumps/AppKit) not installed")
win_shell = pytest.importorskip(
    "flow.platform_win32.shell", reason="pystray/Pillow not installed")


# --------------------------------------------------------------------------
# resolver + curated menus
# --------------------------------------------------------------------------

def test_resolve_hotkey_name_auto_per_platform(monkeypatch):
    monkeypatch.setattr(hotkey, "IS_MAC", True)
    assert hotkey.resolve_hotkey_name("auto") == "alt_r"
    monkeypatch.setattr(hotkey, "IS_MAC", False)
    assert hotkey.resolve_hotkey_name("auto") == "ctrl_r"
    assert hotkey.resolve_hotkey_name("f12") == "f12"   # explicit passes through
    assert hotkey.resolve_hotkey_name("f13") == "f13"   # legacy saved pick still resolves
    with pytest.raises(ValueError, match="Unsupported hotkey"):
        hotkey.resolve_hotkey_name("hyper")


def test_resolve_hotkey_for_all_picker_choices():
    for key, expected in (("alt_r", keyboard.Key.alt_r),
                          ("cmd_r", keyboard.Key.cmd_r),
                          ("ctrl_r", keyboard.Key.ctrl_r),
                          ("f12", keyboard.Key.f12),
                          ("f13", keyboard.Key.f13)):
        assert hotkey.resolve_hotkey(key) == expected


def test_curated_menu_keys_are_valid_and_hinted():
    for shell_mod in (flow_app, win_shell):
        for key, label in shell_mod._HOTKEYS:
            assert key in hotkey.VALID_HOTKEYS, (shell_mod.__name__, key)
            assert key in shell_mod._HOTKEY_HINTS
            assert label  # Bulgarian label present


# --------------------------------------------------------------------------
# mac shell settings round-trip (fixed key set gained "hotkey")
# --------------------------------------------------------------------------

def test_mac_settings_hotkey_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    config = FlowConfig()
    config.hotkey = "cmd_r"
    flow_app.save_settings(config, str(path))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["hotkey"] == "cmd_r"
    assert set(on_disk) == {"language_mode", "ui_language", "ollama_model",
                            "speaker_rhotacism", "hotkey"}
    fresh = FlowConfig()
    flow_app.load_settings(fresh, str(path))
    assert fresh.hotkey == "cmd_r"


def test_mac_settings_invalid_hotkey_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hotkey": "hyper", "language_mode": "bg"}),
                    encoding="utf-8")
    config = FlowConfig()
    flow_app.load_settings(config, str(path))
    assert config.hotkey == "auto"          # bad value ignored...
    assert config.language_mode == "bg"     # ...without poisoning the rest


# --------------------------------------------------------------------------
# live restart (set_hotkey) with a mocked PushToTalk
# --------------------------------------------------------------------------

class FakePTT:
    """Stands in for flow.hotkey.PushToTalk; records what the shell did."""

    def __init__(self, on_start, on_stop, on_cancel, config):
        self.callbacks = (on_start, on_stop, on_cancel)
        self.hotkey_at_construction = config.hotkey
        self.stopped = False
        self.ran = threading.Event()

    def run_forever(self):
        self.ran.set()

    def stop(self):
        self.stopped = True


def _stub_shell(cls, tmp_path, monkeypatch):
    """A real shell instance minus __init__ (no menu bar / tray needed):
    just the attributes set_hotkey/_start_ptt/_run_ptt actually touch."""
    made: list[FakePTT] = []

    def factory(*args, **kwargs):
        ptt = FakePTT(*args, **kwargs)
        made.append(ptt)
        return ptt

    monkeypatch.setattr(hotkey, "PushToTalk", factory)
    monkeypatch.chdir(tmp_path)             # mac shell saves ./settings.json
    if cls is not flow_app.FlowApp:         # win shell saves runtime_dir/settings.json
        monkeypatch.setattr(win_shell.paths, "runtime_dir", lambda: str(tmp_path))
    app = cls.__new__(cls)
    app.config = FlowConfig()
    app._rec_lock = threading.Lock()
    app._recording_active = False
    app._hud = Mock()
    cbs = (Mock(name="on_start"), Mock(name="on_stop"), Mock(name="on_cancel"))
    app._ptt_callbacks = cbs
    old = FakePTT(*cbs, app.config)
    app._ptt = old
    if cls is flow_app.FlowApp:             # mac-only checkmark plumbing
        app._hotkey_items = {}
        app._dock_hotkey_items = {}
    else:                                   # win32 menu refresh plumbing
        app._icon = Mock()
    return app, old, cbs, made


@pytest.mark.parametrize("cls_name", ["mac", "win"])
def test_set_hotkey_live_restart(cls_name, tmp_path, monkeypatch):
    cls = flow_app.FlowApp if cls_name == "mac" else win_shell.PrivaVoxApp
    app, old, cbs, made = _stub_shell(cls, tmp_path, monkeypatch)
    pick = "cmd_r" if cls_name == "mac" else "f12"

    cls.set_hotkey(app, pick)

    assert old.stopped                       # old listener stopped...
    assert made and app._ptt is made[-1] and app._ptt is not old
    assert made[-1].hotkey_at_construction == pick   # ...new one on the new key
    assert made[-1].callbacks == cbs         # pipeline callbacks preserved
    assert made[-1].ran.wait(2.0)            # run_forever on a fresh thread
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk["hotkey"] == pick         # persisted
    texts = [c.args[0] for c in app._hud.show.call_args_list]
    assert any(t.startswith("Клавиш:") for t in texts)  # HUD confirmation


@pytest.mark.parametrize("cls_name", ["mac", "win"])
def test_set_hotkey_ignored_while_recording(cls_name, tmp_path, monkeypatch):
    cls = flow_app.FlowApp if cls_name == "mac" else win_shell.PrivaVoxApp
    app, old, _cbs, made = _stub_shell(cls, tmp_path, monkeypatch)
    app._recording_active = True             # dictation in flight

    cls.set_hotkey(app, "f12")

    assert not old.stopped and app._ptt is old and not made
    assert app.config.hotkey == "auto"       # pick ignored
    assert not (tmp_path / "settings.json").exists()
    texts = [c.args[0] for c in app._hud.show.call_args_list]
    assert any("Записът тече" in t for t in texts)      # HUD warning instead


@pytest.mark.parametrize("cls_name", ["mac", "win"])
def test_set_hotkey_before_boot_saves_without_restart(cls_name, tmp_path, monkeypatch):
    cls = flow_app.FlowApp if cls_name == "mac" else win_shell.PrivaVoxApp
    app, _old, _cbs, made = _stub_shell(cls, tmp_path, monkeypatch)
    app._ptt = None                          # _boot has not built one yet

    cls.set_hotkey(app, "f12")

    assert not made                          # nothing to restart (yet)
    assert app.config.hotkey == "f12"        # _boot will pick this up
    on_disk = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert on_disk["hotkey"] == "f12"


# --------------------------------------------------------------------------
# status-text hints follow the pick
# --------------------------------------------------------------------------

def test_hotkey_hints_follow_the_pick(tmp_path, monkeypatch):
    app, *_ = _stub_shell(flow_app.FlowApp, tmp_path, monkeypatch)
    monkeypatch.setattr(hotkey, "IS_MAC", True)   # mac half must not depend on the host OS
    assert app._hotkey_hint() == "дясната ⌥"      # auto → alt_r on mac
    app.config.hotkey = "cmd_r"
    assert app._hotkey_hint() == "дясната ⌘"
    app.config.hotkey = "f12"
    assert app._hotkey_hint() == "F12"

    wapp, *_ = _stub_shell(win_shell.PrivaVoxApp, tmp_path, monkeypatch)
    monkeypatch.setattr(hotkey, "IS_MAC", False)  # what a real Windows sees
    assert wapp._hotkey_hint() == "десния Ctrl"   # auto → ctrl_r on win
    wapp.config.hotkey = "alt_r"
    assert wapp._hotkey_hint() == "десния Alt"
