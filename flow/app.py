"""Flow menu bar app: the UX shell around the dictation pipeline.

Run with:  python -m flow.app   (Flow.app bundle does exactly this)

Adds on top of the plain daemon (python -m flow):
- menu bar icon with live state (warming / ready / recording / processing)
- native macOS permission prompts (Accessibility + Input Monitoring get
  requested programmatically, so the user never hunts for the "+" button)
- audio cues on record start / insert / failure
- Quit, open-dictionary and open-log menu items
- pickers: език на диктовката, клавиш за диктовка (live listener swap), AI модел

The pipeline itself lives in flow.__main__ / flow.* and is unchanged.
"""

from __future__ import annotations

# ---- Windows crash-logging preamble (MUST stay first; stdlib-only) ----------
# pythonw.exe has no console: sys.stdout/stderr are None, so any exception —
# including one raised by the heavy top-level imports below (numpy at line ~30,
# the win32 shell at line ~48, and everything they pull in) — is otherwise
# swallowed with no trace and PrivaVox.log is never created. We arm logging
# BEFORE those imports so ANY failure (Python exception or native hard crash)
# lands in PrivaVox.log.
#
# Guarded on sys.platform == "win32", so macOS is a byte-for-byte no-op (the
# Flow.app launcher owns the mac redirect). `python -m flow` (the daemon entry,
# flow/__main__.py) never imports this module, so it is unaffected. Uses only
# stdlib (os, sys, faulthandler, traceback) — nothing here can fail on a broken
# third-party install, which is the whole point.
import os as _os
import sys as _sys

if _sys.platform == "win32":
    import faulthandler as _faulthandler

    def _privavox_early_log():
        # Do NOT import flow.paths here — it pulls flow.platform_impl and could
        # itself be part of a broken import graph. Recompute the path inline
        # with the same %LOCALAPPDATA%\PrivaVox contract paths.py encodes
        # (paths.runtime_dir / log_path). Small, deliberate duplication.
        base = _os.environ.get("LOCALAPPDATA") or _os.path.join(
            _os.path.expanduser("~"), "AppData", "Local")
        d = _os.path.join(base, "PrivaVox")
        try:
            _os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return open(_os.path.join(d, "PrivaVox.log"),
                    "a", buffering=1, encoding="utf-8", errors="replace")

    try:
        _privavox_log = _privavox_early_log()
        # pythonw → sys.stdout/stderr are None; redirect them NOW so every
        # print() and the interpreter's own traceback have somewhere to go.
        if _sys.stdout is None:
            _sys.stdout = _privavox_log
        if _sys.stderr is None:
            _sys.stderr = _privavox_log
        # Native hard crashes (a bad DLL inside numpy/pystray/…) → C traceback
        # in the log instead of a silent exit.
        _faulthandler.enable(file=_privavox_log)

        def _privavox_excepthook(exc_type, exc, tb):
            import traceback
            _privavox_log.write(
                "\n[flow.app] FATAL uncaught exception at startup:\n")
            traceback.print_exception(exc_type, exc, tb, file=_privavox_log)
            _privavox_log.flush()

        # Import-time failures escaping this module body route through here.
        _sys.excepthook = _privavox_excepthook
    except Exception:
        # Logging setup must NEVER be what kills startup — swallow and go on.
        pass
# ---- end preamble -----------------------------------------------------------

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

import numpy as np

from . import __version__, paths
from .config import FlowConfig
from .dictionary import load_configured_dictionary
from .platform_impl import IS_MAC

if IS_MAC:
    import AppKit
    import objc
    import rumps

    from .hud import HUD
else:
    # The W2 pystray shell (flow/platform_win32/shell.py). Importing it
    # eagerly keeps a missing dependency (pystray/Pillow/…) loud at import
    # time — parity with the AppKit imports above; main() dispatches to it.
    from .platform_win32 import shell as _win_shell  # noqa: F401

    # `class _DockMenuHandler(AppKit.NSObject)` and `class FlowApp(rumps.App)`
    # are MODULE-LEVEL statements — they execute on every platform, so their
    # base classes must resolve off-mac too. These stand-ins let the module
    # import cleanly on Windows; nothing mac-only is ever instantiated there
    # (main() dispatches to the win32 shell first). Without this the import
    # dies with NameError: name 'AppKit' is not defined — and under pythonw
    # (no console) that death is silent.
    class _MacOnly:
        NSObject = object
        App = object

    AppKit = rumps = _MacOnly

_STATE_ICON = {
    "warming": "⏳",
    "perm": "⚠️",
    "error": "⚠️",
    "ready": "🎙",
    "recording": "🔴",
    "processing": "💭",
}

_SETTINGS_PANES = {
    "input": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
}

_LANGS = (("auto", "Автоматично разпознаване"),
          ("bg", "Само български"),
          ("en", "Само английски"))

# Curated push-to-talk keys for THIS platform (flow/hotkey.py knows more, but
# the menu offers only combinations that make sense on a mac keyboard)
_HOTKEYS = (("alt_r", "Дясна ⌥ (Option)"),
            ("cmd_r", "Дясна ⌘ (Command)"),
            ("f13", "F13"))
# how the key reads inside "задръж … и говори" status texts
_HOTKEY_HINTS = {"alt_r": "дясната ⌥", "cmd_r": "дясната ⌘", "f13": "F13"}


class _DockMenuHandler(AppKit.NSObject):
    """ObjC target for the Dock right-click menu items."""

    def initWithApp_(self, app):
        self = objc.super(_DockMenuHandler, self).init()
        self._app = app
        return self

    def pickLang_(self, sender):
        self._app.set_language_mode(str(sender.representedObject()))

    def pickHotkey_(self, sender):
        self._app.set_hotkey(str(sender.representedObject()))

    def pickModel_(self, sender):
        self._app.set_ollama_model(str(sender.representedObject()))

    def openDict_(self, sender):
        self._app._open_dictionary(None)

    def openLog_(self, sender):
        self._app._open_log(None)

    def quitFlow_(self, sender):
        self._app._graceful_quit(None)


def _norm_model(name: str) -> str:
    # Ollama resolves bare names to ":latest" — compare normalized
    return name if ":" in name else f"{name}:latest"


# ---- settings (module-level so the suite can test them headlessly) ----------

def load_settings(config: FlowConfig, path: str = "settings.json") -> None:
    """Tolerant settings.json → config merge (unknown/invalid keys ignored)."""
    from .hotkey import VALID_HOTKEYS

    try:
        with open(path) as f:
            s = json.load(f)
        if s.get("language_mode") in ("auto", "en", "bg"):
            config.language_mode = s["language_mode"]
        if isinstance(s.get("ollama_model"), str) and s["ollama_model"]:
            config.ollama_model = s["ollama_model"]
        if isinstance(s.get("speaker_rhotacism"), bool):
            config.speaker_rhotacism = s["speaker_rhotacism"]
        if s.get("hotkey") in VALID_HOTKEYS:
            config.hotkey = s["hotkey"]
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[flow.app] settings.json unreadable: {e!r}")


def save_settings(config: FlowConfig, path: str = "settings.json") -> None:
    """The mac shell's fixed key set (the win32 shell merge-writes instead)."""
    try:
        with open(path, "w") as f:
            json.dump({"language_mode": config.language_mode,
                       "ollama_model": config.ollama_model,
                       "speaker_rhotacism": config.speaker_rhotacism,
                       "hotkey": config.hotkey}, f)
    except Exception as e:
        print(f"[flow.app] settings.json write failed: {e!r}")


def _play(sound: str) -> None:
    subprocess.Popen(
        ["afplay", f"/System/Library/Sounds/{sound}.aiff"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def request_input_monitoring() -> bool:
    """Trigger the native Input Monitoring prompt (adds Flow to the list)."""
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDRequestAccess.restype = ctypes.c_bool
        iokit.IOHIDRequestAccess.argtypes = [ctypes.c_uint]
        return bool(iokit.IOHIDRequestAccess(1))  # kIOHIDRequestTypeListenEvent
    except Exception as e:  # pragma: no cover - depends on macOS
        print(f"[flow.app] IOHIDRequestAccess unavailable: {e!r}")
        return True


def request_accessibility() -> bool:
    """Trigger the native Accessibility prompt (adds Flow to the list)."""
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
    except Exception as e:  # pragma: no cover - depends on macOS
        print(f"[flow.app] AXIsProcessTrustedWithOptions unavailable: {e!r}")
        return True


def check_input_monitoring() -> bool:
    """Silent Input Monitoring check (no prompt): 0 == kIOHIDAccessTypeGranted."""
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.restype = ctypes.c_int
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint]
        return iokit.IOHIDCheckAccess(1) == 0  # kIOHIDRequestTypeListenEvent
    except Exception:  # pragma: no cover - depends on macOS
        return True


def check_accessibility() -> bool:
    """Silent Accessibility check (no prompt)."""
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore

        return bool(AXIsProcessTrusted())
    except Exception:  # pragma: no cover - depends on macOS
        return True


class FlowApp(rumps.App):
    def __init__(self, config: FlowConfig | None = None) -> None:
        icon = "menubar-icon.png" if os.path.exists("menubar-icon.png") else None
        super().__init__("PrivaVox", title=_STATE_ICON["warming"], icon=icon,
                         template=True, quit_button=None)
        self._dock_ready = False
        self.config = config or FlowConfig()
        self._load_settings()
        self._state = "warming"
        self._status_text = "Зареждане на моделите…"
        self._missing: list[str] = []

        self._status_item = rumps.MenuItem("Зареждане на моделите…")
        self._perm_item = rumps.MenuItem("Отвори настройките за поверителност", callback=self._open_settings)
        self._lang_menu = rumps.MenuItem("Език на диктовката")
        self._lang_items: dict[str, rumps.MenuItem] = {}
        for key, label in _LANGS:
            item = rumps.MenuItem(label, callback=self._pick_language)
            item._flow_lang = key
            self._lang_items[key] = item
            self._lang_menu.add(item)
        self._sync_language_checkmarks()
        self._hotkey_menu = rumps.MenuItem("Клавиш за диктовка")
        self._hotkey_items: dict[str, rumps.MenuItem] = {}
        for key, label in _HOTKEYS:
            item = rumps.MenuItem(label, callback=self._pick_hotkey)
            item._flow_hotkey = key
            self._hotkey_items[key] = item
            self._hotkey_menu.add(item)
        self._sync_hotkey_checkmarks()
        self._model_menu = rumps.MenuItem("AI модел")
        self._model_items: dict[str, rumps.MenuItem] = {}
        self._dict_item = rumps.MenuItem("Отвори личния речник", callback=self._open_dictionary)
        self._log_item = rumps.MenuItem("Покажи лога", callback=self._open_log)
        self._quit_item = rumps.MenuItem("Спри PrivaVox", callback=self._graceful_quit)
        self.menu = [self._status_item, self._perm_item, None, self._lang_menu,
                     self._hotkey_menu, self._model_menu, self._dict_item,
                     self._log_item, None, self._quit_item]

        self._hud = HUD()
        self._last_inserted = ""
        # recorder + focus-probe state; _rec_lock serializes hotkey callbacks
        # (listener thread) against the parallel focus probe (probe thread)
        self._rec_lock = threading.Lock()
        self._recording_active = False
        self._rec_gen = 0                # which recording a probe belongs to
        self._focus_verdict: tuple[str, str] = ("unknown", "not-probed")
        self._quitting = False
        self._worker_thread: threading.Thread | None = None
        # live push-to-talk listener + its callbacks (set once by _boot), so
        # the hotkey picker can stop/recreate the listener without a restart
        self._ptt = None
        self._ptt_callbacks: tuple | None = None
        self._dock_handler = _DockMenuHandler.alloc().initWithApp_(self)
        self._dock_menu = self._build_dock_menu()
        _install_dock_menu_delegate(self)
        # job = (audio, focus verdict snapshotted at on_stop); None = quit
        self._jobs: queue.Queue[tuple[np.ndarray, tuple[str, str]] | None] = queue.Queue()
        rumps.Timer(self._tick, 0.2).start()
        threading.Thread(target=self._boot, daemon=True).start()

    # ---- Dock menu ---------------------------------------------------------

    def _build_dock_menu(self) -> AppKit.NSMenu:
        menu = AppKit.NSMenu.alloc().init()
        self._dock_status_nsitem = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Стартиране…", None, "")
        menu.addItem_(self._dock_status_nsitem)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        lang_root = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Език на диктовката", None, "")
        sub = AppKit.NSMenu.alloc().init()
        self._dock_lang_items: dict[str, AppKit.NSMenuItem] = {}
        for key, label in _LANGS:
            it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "pickLang:", "")
            it.setTarget_(self._dock_handler)
            it.setRepresentedObject_(key)
            it.setState_(1 if key == self.config.language_mode else 0)
            sub.addItem_(it)
            self._dock_lang_items[key] = it
        lang_root.setSubmenu_(sub)
        menu.addItem_(lang_root)

        hotkey_root = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Клавиш за диктовка", None, "")
        hsub = AppKit.NSMenu.alloc().init()
        self._dock_hotkey_items: dict[str, AppKit.NSMenuItem] = {}
        current = self._hotkey_name()
        for key, label in _HOTKEYS:
            it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, "pickHotkey:", "")
            it.setTarget_(self._dock_handler)
            it.setRepresentedObject_(key)
            it.setState_(1 if key == current else 0)
            hsub.addItem_(it)
            self._dock_hotkey_items[key] = it
        hotkey_root.setSubmenu_(hsub)
        menu.addItem_(hotkey_root)

        model_root = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "AI модел", None, "")
        self._dock_model_menu = AppKit.NSMenu.alloc().init()
        self._dock_model_items: dict[str, AppKit.NSMenuItem] = {}
        model_root.setSubmenu_(self._dock_model_menu)
        menu.addItem_(model_root)

        for title, action in (("Отвори личния речник", "openDict:"),
                              ("Покажи лога", "openLog:")):
            it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            it.setTarget_(self._dock_handler)
            menu.addItem_(it)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        quit_it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Спри PrivaVox", "quitFlow:", "")
        quit_it.setTarget_(self._dock_handler)
        menu.addItem_(quit_it)
        return menu

    def set_ollama_model(self, name: str) -> None:
        from . import cleanup

        self.config.ollama_model = name
        self._sync_model_checkmarks()
        self._save_settings()
        print(f"[flow.app] ollama model -> {name}")
        self._hud.show(f"Модел: {name} — зареждане…", hide_after=None)

        def warm() -> None:
            try:
                secs = cleanup.warm_up(self.config)
                print(f"[flow.app] model {name} warmed in {secs:.1f}s")
                # only surface "ready" while idle-ish — a warm-up finishing
                # mid-dictation must not clobber recording/processing UI
                if self._state in ("warming", "ready", "perm"):
                    self._hud.show(f"Модел готов ({secs:.1f} s)", "green", hide_after=1.5)
                    self._set("ready", f"Модел: {name}")
            except Exception as e:
                reason = str(e) or repr(e)
                self._hud.show(f"Моделът не тръгна: {reason}", "red", hide_after=3.0)
                print(f"[flow.app] model warm-up failed: {reason}")

        threading.Thread(target=warm, daemon=True).start()

    def _list_ollama_models(self) -> list[str] | None:
        """Model names known to Ollama; None when the server can't be reached."""
        try:
            import requests

            base = self.config.ollama_url.split("/v1/")[0]
            r = requests.get(f"{base}/api/tags", timeout=5)
            return sorted(m["name"] for m in r.json().get("models", []))
        except Exception as e:
            print(f"[flow.app] can't list Ollama models: {e!r}")
            return None

    def _sync_model_checkmarks(self) -> None:
        for key, item in self._model_items.items():
            item.state = 1 if key == self.config.ollama_model else 0
        for key, it in self._dock_model_items.items():
            it.setState_(1 if key == self.config.ollama_model else 0)

    def _populate_model_menus(self, models: list[str]) -> None:
        """Fill both model submenus; runs on the main thread."""
        for name in models:
            r_item = rumps.MenuItem(name, callback=lambda s, n=name: self.set_ollama_model(n))
            self._model_items[name] = r_item
            self._model_menu.add(r_item)
            d_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                name, "pickModel:", "")
            d_item.setTarget_(self._dock_handler)
            d_item.setRepresentedObject_(name)
            self._dock_model_items[name] = d_item
            self._dock_model_menu.addItem_(d_item)
        self._sync_model_checkmarks()

    def set_language_mode(self, key: str) -> None:
        label = dict(_LANGS)[key]
        self.config.language_mode = key
        self._sync_language_checkmarks()
        for k, it in self._dock_lang_items.items():
            it.setState_(1 if k == key else 0)
        self._save_settings()
        print(f"[flow.app] language mode -> {key}")
        self._hud.show(f"Език: {label}", hide_after=1.2)

    # ---- hotkey picker ------------------------------------------------------

    def _hotkey_name(self) -> str:
        """config.hotkey with "auto" resolved to the platform default."""
        from . import hotkey as hotkey_mod

        try:
            return hotkey_mod.resolve_hotkey_name(self.config.hotkey)
        except ValueError:
            return self.config.hotkey

    def _hotkey_hint(self) -> str:
        """How "задръж … и говори" texts name the current key."""
        name = self._hotkey_name()
        return _HOTKEY_HINTS.get(name, name)

    def set_hotkey(self, key: str) -> None:
        """Menu pick: persist + live-swap the push-to-talk listener."""
        label = dict(_HOTKEYS).get(key, key)
        with self._rec_lock:
            if self._recording_active:
                # never kill an in-flight dictation from a menu click
                self._hud.show("⚠ Записът тече — пусни клавиша и опитай пак",
                               "orange", hide_after=2.5)
                return
            self.config.hotkey = key
            old, self._ptt = self._ptt, None
            if old is not None:
                old.stop()  # signal only — the old thread unwinds by itself
        self._sync_hotkey_checkmarks()
        for k, it in self._dock_hotkey_items.items():
            it.setState_(1 if k == key else 0)
        self._save_settings()
        print(f"[flow.app] hotkey -> {key}")
        if old is not None:
            self._start_ptt()  # same callbacks, fresh listener + thread
        # before _boot reaches the listener there is nothing to restart: the
        # first PushToTalk is built from config.hotkey, which is already set
        self._hud.show(f"Клавиш: {label}", hide_after=1.5)

    def _start_ptt(self) -> None:
        from . import hotkey as hotkey_mod

        on_start, on_stop, on_cancel = self._ptt_callbacks
        ptt = hotkey_mod.PushToTalk(on_start, on_stop, on_cancel, self.config)
        self._ptt = ptt
        threading.Thread(target=self._run_ptt, args=(ptt,), daemon=True).start()

    def _run_ptt(self, ptt) -> None:
        try:
            ptt.run_forever()
        except Exception as e:
            if self._ptt is ptt:  # a replaced listener winding down is no error
                self._set("error", "Hotkey слушателят падна — виж лога")
            print(f"[flow.app] hotkey listener failed: {e!r}")

    def _sync_hotkey_checkmarks(self) -> None:
        current = self._hotkey_name()
        for key, item in self._hotkey_items.items():
            item.state = 1 if key == current else 0

    def _pick_hotkey(self, sender) -> None:
        self.set_hotkey(sender._flow_hotkey)

    # ---- settings ---------------------------------------------------------

    def _load_settings(self) -> None:
        # settings.json lives in the app runtime dir (the process cwd)
        load_settings(self.config)

    def _save_settings(self) -> None:
        save_settings(self.config)

    def _sync_language_checkmarks(self) -> None:
        for key, item in self._lang_items.items():
            item.state = 1 if key == self.config.language_mode else 0

    def _pick_language(self, sender) -> None:
        self.set_language_mode(sender._flow_lang)

    # ---- UI plumbing ------------------------------------------------------

    def _set(self, state: str, text: str) -> None:
        self._state = state
        self._status_text = text
        print(f"[flow.app] {state}: {text}", flush=True)

    def _tick(self, _sender) -> None:
        if not self._dock_ready:
            # runtime Dock presence: avoids editing Info.plist (a re-sign would
            # invalidate the TCC grants keyed to the bundle signature)
            self._dock_ready = True
            nsapp = AppKit.NSApplication.sharedApplication()
            nsapp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            if os.path.exists("app-icon.icns"):
                img = AppKit.NSImage.alloc().initWithContentsOfFile_("app-icon.icns")
                if img:
                    nsapp.setApplicationIconImage_(img)
        # image icon carries the shape; emoji title carries the live state
        self.title = f"{_STATE_ICON.get(self._state, '🎙')} PrivaVox"
        self._status_item.title = self._status_text
        self._dock_status_nsitem.setTitle_(self._status_text)

    def _open_settings(self, _sender) -> None:
        for pane in self._missing or ["input"]:
            subprocess.Popen(["open", _SETTINGS_PANES[pane]])

    def _open_dictionary(self, _sender) -> None:
        subprocess.Popen(["open", "-t", self.config.dictionary_path])

    def _open_log(self, _sender) -> None:
        subprocess.Popen(["open", paths.log_path()])

    # ---- startup ----------------------------------------------------------

    def _boot(self) -> None:
        from . import audio as audio_mod
        from . import cleanup, focus, stt

        # Permissions first: fire the native macOS prompts immediately, then
        # warm the models while the user clicks through them.
        ax_ok = request_accessibility()
        im_ok = request_input_monitoring()

        self._set("warming", "Затопляне на Whisper…")
        try:
            stt.warm_up(self.config)
        except Exception as e:
            self._set("error", f"Whisper не тръгна: {e!r}"[:80])
            return

        self._set("warming", "Затопляне на BgGPT (Ollama)…")
        try:
            cleanup.warm_up(self.config)
        except Exception as e:
            print(f"[flow.app] LLM warm-up failed: {e!r}")
            models = self._list_ollama_models()
            if models is None:
                self._set("error", "Ollama не отговаря — brew services start ollama")
            elif _norm_model(self.config.ollama_model) not in {_norm_model(m) for m in models}:
                self._set("error",
                          f"Моделът {self.config.ollama_model} липсва — пусни 'Инсталирай PrivaVox.command'")
            else:
                self._set("error", f"BgGPT не тръгна: {e}"[:80])
            return

        dictionary = load_configured_dictionary(self.config)
        print(f"[flow.app] dictionary: {len(dictionary.terms)} term(s)")

        models = self._list_ollama_models()
        if models:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(self._populate_model_menus, models)

        # macOS pre-adds Flow to the permission lists via the prompts above;
        # here we wait for the toggles and relaunch ourselves — the user never
        # has to restart anything by hand.
        print(f"[flow.app] permissions: accessibility={ax_ok} input_monitoring={im_ok}")
        if not (ax_ok and im_ok):
            self._missing = [m for m, ok in
                             (("accessibility", ax_ok), ("input", im_ok)) if not ok]
            names = {"accessibility": "Accessibility", "input": "Input Monitoring"}
            what = " и ".join(names[m] for m in self._missing)
            self._set("perm", f"Липсва: {what} — включи PrivaVox там, ще продължа сам")
            for pane in self._missing:
                subprocess.Popen(["open", _SETTINGS_PANES[pane]])
            while not (check_accessibility() and check_input_monitoring()):
                time.sleep(2.0)
            bundle_path = os.environ.get("FLOW_BUNDLE_PATH")  # set by the launcher
            if bundle_path:
                # Relaunch through LaunchServices, NOT os.exec*: after exec the
                # process identity becomes the bare python binary and the TCC
                # grants (keyed to the Flow.app bundle) stop applying.
                # Single-instance lock: os._exit closes our flock fd, so the
                # kernel releases the lock with this process (any death does);
                # the child's `sleep 1` covers the gap before the new instance
                # acquires it.
                print(f"[flow.app] permissions granted — relaunching {bundle_path}")
                subprocess.Popen(["/bin/zsh", "-c", 'sleep 1; open "$FLOW_BUNDLE_PATH"'])
                os._exit(0)
            # dev run (python -m flow.app, no bundle): the fresh grants apply
            # to listeners this same process creates next — keep booting; if
            # the listener still fails, the error path below reports it.
            print("[flow.app] permissions granted — продължавам без рестарт (dev режим)")

        # Microphone: opening a short input stream triggers the mic prompt now
        # instead of mid-first-dictation. Run with a timeout — CoreAudio can
        # occasionally hang the open, and that must not brick the boot.
        recorder = audio_mod.Recorder(self.config)  # the shared dictation recorder

        def _mic_preflight() -> None:
            # THROWAWAY Recorder, never the shared one: a hung preflight
            # thread waking up later must not clobber a live dictation.
            probe = audio_mod.Recorder(self.config)
            try:
                probe.start()
                time.sleep(0.2)
                probe.cancel()
                print("[flow.app] mic preflight: OK")
            except Exception as e:
                print(f"[flow.app] mic preflight failed: {e!r}")

        pf = threading.Thread(target=_mic_preflight, daemon=True)
        pf.start()
        pf.join(timeout=5.0)
        if pf.is_alive():
            print("[flow.app] mic preflight hung (>5s) — continuing; "
                  "the mic prompt will appear on the first dictation")

        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()

        def _probe_focus(gen: int) -> None:
            # Runs in parallel with an already-recording mic (the AX round
            # trip costs 0.25–6 s worst case and must not delay audio).
            verdict, detail = focus.focused_text_target()
            with self._rec_lock:
                if gen != self._rec_gen or not self._recording_active:
                    return  # that recording is already gone — verdict is moot
                self._focus_verdict = (verdict, detail)
                if verdict == "no-text":
                    # bare desktop: nothing to dictate into — cancel the
                    # 50–300 ms of recording that already happened
                    self._recording_active = False
                    recorder.cancel()
                    print(f"[flow.app] recording cancelled: no text field ({detail})")
                    _play("Basso")
                    self._set("ready", "Няма избрано текстово поле — кликни в поле")
                    self._hud.show("⚠ Няма избрано текстово поле — кликни в поле и опитай пак",
                                   "orange", hide_after=2.5)
                elif verdict == "soft-no-text":
                    # doubtful target (cell/button/group): record anyway,
                    # but say so — the insert keeps a clipboard safety net
                    print(f"[flow.app] doubtful text target ({detail}) — recording continues")
                    self._hud.show("● Записва… ⚠ вероятно няма текстово поле", "orange")

        def on_start() -> None:
            # Mic FIRST — recording begins the instant the key goes down.
            # The focus probe used to run before this and its AX round-trip
            # (0.25 s Electron nudge, seconds on a hung app) clipped the
            # first word. Now it runs in parallel and cancels retroactively.
            with self._rec_lock:
                try:
                    recorder.start()
                except Exception as e:
                    self._recording_active = False
                    try:
                        # audio.py assigns _stream before .start(): a failed
                        # open may leave a half-open stream — kill the ghost
                        recorder.cancel()
                    except Exception:
                        pass
                    self._set("error", "Микрофонът отказа — виж Privacy → Microphone")
                    self._hud.show("Микрофонът отказа достъп", "red", hide_after=2.5)
                    print(f"[flow.app] mic error: {e!r}")
                    return
                self._recording_active = True
                self._rec_gen += 1
                gen = self._rec_gen
                self._focus_verdict = ("unknown", "probe-pending")
            self._set("recording", "Записва… (Esc отказва)")
            self._hud.show("● Записва…", "red")
            _play("Tink")
            threading.Thread(target=_probe_focus, args=(gen,), daemon=True).start()

        def on_stop(held_s: float) -> None:
            with self._rec_lock:
                if not self._recording_active:
                    return
                self._recording_active = False
                audio = recorder.stop()
                # snapshot the parallel probe's verdict: the worker must NOT
                # re-probe at insert time (second AX round-trip on the hot
                # path); a probe still pending simply means "unknown"
                verdict = self._focus_verdict
            if held_s < self.config.min_recording_s:
                self._set("ready", f"Готово — задръж {self._hotkey_hint()} и говори")
                self._hud.hide()
                return
            self._set("processing", "Обработва…")
            if recorder.truncated:
                # the warning pill doubles as the processing indicator —
                # a second show() would wipe it before it can be read
                print(f"[flow.app] recording capped at {self.config.max_recording_s:.0f}s")
                self._hud.show(f"Записът е орязан до {self.config.max_recording_s:.0f} s",
                               "orange", hide_after=2.5)
            else:
                self._hud.show("Обработва…")
            self._jobs.put((audio, verdict))

        def on_cancel() -> None:
            with self._rec_lock:
                if not self._recording_active:
                    return
                self._recording_active = False
                recorder.cancel()
            self._set("ready", "Отказано — готово за нова диктовка")
            self._hud.show("✕ Отказано", hide_after=1.0)

        self._set("ready", f"Готово — задръж {self._hotkey_hint()} и говори")
        self._hud.show(f"PrivaVox е готов — задръж {self._hotkey_hint()} и говори",
                       hide_after=3.0)
        _play("Glass")

        # callbacks live on self so the hotkey picker can rebuild the listener
        self._ptt_callbacks = (on_start, on_stop, on_cancel)
        self._start_ptt()

    # ---- dictation worker -------------------------------------------------

    def _worker(self) -> None:
        # Lazy imports: keeps the pipeline stack (flow.__main__ → numpy again,
        # flow.cleanup, flow.stt) off flow.app's module-load path. It runs on
        # this worker thread the first time a job arrives — mac behaviour is
        # unchanged (run_pipeline is only ever called here), and on win32 the
        # shell has its own lazy re-import (shell.py), so nothing is lost.
        from . import insert
        from .__main__ import run_pipeline

        while True:
            job = self._jobs.get()
            if job is None:
                return  # graceful quit: _graceful_quit() waits for this
            audio, (verdict, field_detail) = job
            try:
                result = run_pipeline(audio, self.config)
                if result.status == "too-short":
                    self._set("ready", "Твърде кратко — игнорирано")
                    self._hud.hide()
                elif result.status == "silence":
                    self._set("ready", "Тишина — нищо не е вмъкнато")
                    self._hud.show("Тишина — нищо не е вмъкнато", hide_after=1.6)
                elif result.status == "hallucination":
                    print(f"[flow.app] hallucination filtered: {result.raw_text!r}")
                    self._set("ready", "Шум/халюцинация — нищо не е вмъкнато")
                    self._hud.show("Само шум — нищо не е вмъкнато", hide_after=1.6)
                else:
                    t = result.timings
                    # consecutive dictations must not glue together:
                    # "…точка.Искам" → "…точка. Искам"
                    text = result.cleaned_text
                    if self._last_inserted and not self._last_inserted[-1].isspace():
                        text = " " + text
                    # ALWAYS paste — the focus verdict must never block an
                    # insert (Electron apps hide their AX tree → "unknown").
                    # The verdict was probed DURING recording and travels
                    # with the job: no second AX round-trip on the hot path.
                    status = insert.paste_text(text, self.config)
                    print(f"[flow.app] lang={result.language} (whisper detected: {result.detected_language})")
                    print(f"[flow.app] raw:     {result.raw_text}")
                    print(f"[flow.app] cleaned: {result.cleaned_text}")
                    if result.used_fallback:
                        print(f"[flow.app] cleanup fell back to raw transcript ({result.fallback_reason})")
                    print(f"[flow.app] insert: {status} | field: {field_detail} | total {t.get('total_s', 0):.2f}s")
                    if status == "clipboard-only":
                        # nothing was pasted: no success sound, no _last_inserted
                        self._last_inserted = ""
                        self._set("ready", "Няма Accessibility — текстът е в клипборда (Cmd+V)")
                        self._hud.show("⚠ Няма Accessibility — текстът е в клипборда, Cmd+V",
                                       "orange", hide_after=3.0)
                    elif verdict in ("no-text", "soft-no-text"):
                        # doubtful paste target: keep the text on the
                        # clipboard as a safety net (paste_text restored it)
                        insert.set_clipboard(result.cleaned_text)
                        self._last_inserted = ""
                        self._set("ready", "Няма поле? Текстът е и в клипборда (Cmd+V)")
                        self._hud.show("⚠ Няма поле? Текстът е и в клипборда — Cmd+V",
                                       "orange", hide_after=3.0)
                    elif result.used_fallback:
                        self._last_inserted = text
                        _play("Pop")
                        reason = (result.fallback_reason or "")[:40]
                        self._set("ready", f"Вмъкнато без чистене ({t.get('total_s', 0):.1f} s)")
                        self._hud.show(f"Вмъкнато без чистене ({reason})",
                                       "orange", hide_after=2.5)
                    else:
                        self._last_inserted = text
                        _play("Pop")
                        self._set("ready", f"Вмъкнато ({t.get('total_s', 0):.1f} s) — готово")
                        self._hud.show("✓ Вмъкнато", "green", hide_after=1.2)
            except Exception as e:
                _play("Basso")
                self._set("ready", "Грешка при обработка — виж лога")
                self._hud.show("Грешка — виж лога", "red", hide_after=2.5)
                print(f"[flow.app] pipeline error: {e!r}")

    # ---- quit ---------------------------------------------------------------

    def _graceful_quit(self, _sender=None) -> None:
        """Single quit path for BOTH the menu bar item and the Dock menu:
        an in-flight dictation gets up to 3 s to finish (or at least land
        on the clipboard) before the process dies."""
        if self._quitting:
            return
        self._quitting = True
        print("[flow.app] quit requested — draining dictation queue (max 3 s)")
        self._jobs.put(None)  # worker finishes queued jobs, then exits
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)
        rumps.quit_application()


def _install_dock_menu_delegate(app: "FlowApp") -> None:
    """Teach rumps' NSApplication delegate to serve our Dock right-click menu
    and to flash the HUD when the Dock icon is clicked."""
    from rumps.rumps import NSApp as _RumpsNSApp

    def applicationDockMenu_(self, _sender):
        return app._dock_menu

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _sender, _flag):
        app._hud.show(app._status_text, hide_after=2.0)
        return False

    objc.classAddMethods(_RumpsNSApp, [
        objc.selector(applicationDockMenu_,
                      selector=b"applicationDockMenu:", signature=b"@@:@"),
        objc.selector(applicationShouldHandleReopen_hasVisibleWindows_,
                      selector=b"applicationShouldHandleReopen:hasVisibleWindows:",
                      signature=b"Z@:@Z"),
    ])


def main() -> None:
    if not IS_MAC:
        # W2: the win32 shell owns its whole startup (single-instance guard
        # with a tkinter dialog — no osascript here — plus log redirection
        # for pythonw). The mac path below stays untouched.
        #
        # The top-of-module preamble already armed stdio + excepthook before
        # the heavy imports; this try/except is a second guard so a failure
        # inside win_shell.main() prints a clear marker and full traceback to
        # the log (now a real file) before re-raising. SystemExit passes
        # through untouched (single-instance guard uses sys.exit(0)).
        from .platform_win32 import shell as win_shell

        try:
            win_shell.main()
        except SystemExit:
            raise
        except BaseException:
            traceback.print_exc()
            print("[flow.app] FATAL in win_shell.main() — see traceback above")
            raise
        return

    from . import singleinstance

    if not singleinstance.acquire():
        print("[flow.app] Flow вече върви — втора инстанция не се стартира.")
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 'display dialog "PrivaVox вече върви." buttons {"OK"} '
                 'default button "OK" with title "PrivaVox" with icon caution'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[flow.app] duplicate-instance dialog failed: {e!r}")
        sys.exit(0)
    print(f"[flow.app] Flow v{__version__} menu bar app starting")
    FlowApp().run()


if __name__ == "__main__":
    main()
