"""PrivaVox Windows shell: pystray system tray + the dictation pipeline UX.

The win32 counterpart of flow/app.py's FlowApp (rumps), launched by
flow.app.main() when sys.platform == "win32":

- system tray icon; live state in the tooltip/left-click HUD (Windows has no
  text in the tray, so the emoji state lives in the tooltip title)
- menu: status line, език на диктовката (auto/bg/en), клавиш за диктовка
  (ctrl_r/alt_r/f12 — live restart of the push-to-talk listener), AI модел
  (динамично от Ollama /api/tags), личен речник, лог, изход
- settings.json load/save (merge-write: language_mode, ollama_model, hotkey,
  stt_engine, stt_model — the last two are provisioned by
  Install-PrivaVox.ps1 and must survive every save; speaker_rhotacism, the
  opt-in accessibility flag, is loaded here and survives saves via the merge)
- the same boot sequence and dictation worker as the mac shell
- NO permissions flow: Windows has no TCC — SendInput and the keyboard hook
  just work; only the microphone consent prompt appears (on first stream
  open, which the boot preflight triggers deliberately)

IMPORTANT (W-followup): the _boot/_worker/on_start/on_stop/on_cancel bodies
deliberately DUPLICATE flow/app.py's semantics instead of refactoring a
shared core out of it — phase W2 must not touch the validated mac shell.
When both shells are live, extract the common pipeline-driver (tracked in
docs/windows-port-plan.md, W2 notes).

Runs only on Windows in production, but imports cleanly on macOS (pystray
loads its darwin backend), which is what the mac test suite smoke-checks.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np

from .. import __version__, paths
from ..config import FlowConfig
from ..dictionary import load_configured_dictionary
from ..i18n import t
from .hud_win import HUD

_STATE_ICON = {
    "warming": "⏳",
    "error": "⚠️",
    "ready": "🎙",
    "recording": "🔴",
    "processing": "💭",
}

def _langs():
    # built fresh per call so t() reflects the active UI language (the labels
    # appear both in the menu and in the "Език: …" status HUD)
    return (("auto", t("Автоматично разпознаване", "Auto-detect")),
            ("bg", t("Само български", "Bulgarian only")),
            ("en", t("Само английски", "English only")))


# Curated push-to-talk keys for THIS platform (flow/hotkey.py knows more, but
# the menu offers only combinations that make sense on a Windows keyboard;
# right Alt is AltGr on BG/EU layouts — it types characters, hence the warning)
def _hotkeys():
    return (("ctrl_r", t("Дясна Ctrl", "Right Ctrl")),
            ("alt_r", t("Дясна Alt (внимание: AltGr)", "Right Alt (note: AltGr)")),
            ("f12", t("F12", "F12")))


# the two selectable UI languages, for the "Език на интерфейса" switcher
_UI_LANGS = (("bg", "Български"), ("en", "English"))


# how the key reads inside "задръж … и говори" status texts
def _hotkey_hints():
    return {"ctrl_r": t("десния Ctrl", "right Ctrl"),
            "alt_r": t("десния Alt", "right Alt"),
            "f12": t("F12", "F12")}


# Back-compat module constants: some tests iterate _HOTKEYS / read _HOTKEY_HINTS
# directly (headless, before set_language). They resolve to the default-BG
# labels/hints, matching the pre-i18n literals byte-for-byte.
_HOTKEYS = _hotkeys()
_HOTKEY_HINTS = _hotkey_hints()

_STT_ENGINES = ("auto", "mlx", "faster-whisper",
                "faster-whisper-cuda", "faster-whisper-cpu")

# mac sound name → winsound.MessageBeep type, so the duplicated worker code
# below stays line-comparable with flow/app.py (_play("Pop") etc.).
_SOUNDS = {
    "Tink": 0x00000040,   # MB_ICONASTERISK — record start
    "Pop": 0x00000000,    # MB_OK           — inserted
    "Glass": 0x00000040,  # MB_ICONASTERISK — ready
    "Basso": 0x00000010,  # MB_ICONHAND     — error
}


def _play(sound: str) -> None:
    try:
        import winsound

        winsound.MessageBeep(_SOUNDS.get(sound, 0x00000000))
    except Exception:
        pass  # sounds are garnish — never let them break a dictation


def _norm_model(name: str) -> str:
    # Ollama resolves bare names to ":latest" — compare normalized
    return name if ":" in name else f"{name}:latest"


def _setup_logging() -> None:
    """Redirect stdout/stderr to %LOCALAPPDATA%\\PrivaVox\\PrivaVox.log when run
    under pythonw.exe (the Install-PrivaVox.ps1 contract: the APP creates the
    log). Detected empirically on a real Windows box: under pythonw sys.stdout
    /sys.stderr are NOT None — they are valid-looking TextIOWrappers whose
    writes silently vanish (fstat passes, print() succeeds), so an
    `is not None` guard never redirects and the log is never written. The
    reliable signal is the interpreter's own name."""
    headless = os.path.basename(sys.executable).lower() == "pythonw.exe"
    if not headless and sys.stdout is not None and sys.stderr is not None:
        return  # console run (dev): keep printing there
    os.makedirs(paths.runtime_dir(), exist_ok=True)
    log = open(paths.log_path(), "a", buffering=1, encoding="utf-8", errors="replace")
    if headless or sys.stdout is None:
        sys.stdout = log
    if headless or sys.stderr is None:
        sys.stderr = log


# ---- settings (module-level so the mac suite can test them headlessly) ------

def _settings_path() -> str:
    """Absolute settings.json in the runtime dir — never CWD-relative. The app
    is started via `-m flow.app` with a WorkingDirectory set by the shortcut,
    but resolving settings against the runtime dir removes the dependency
    entirely (a wrong CWD otherwise silently loses the user's language/model
    picks)."""
    return os.path.join(paths.runtime_dir(), "settings.json")


def load_settings(config: FlowConfig, path: str = "settings.json") -> None:
    """Tolerant settings.json → config merge (unknown/invalid keys ignored).

    stt_engine / stt_model are the Install-PrivaVox.ps1 provisioning keys;
    language_mode / ollama_model are the user's own picks from the menu;
    speaker_rhotacism is the opt-in accessibility flag (bool, default off).
    """
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[flow.app] settings.json unreadable: {e!r}")
        return
    if not isinstance(s, dict):
        print(f"[flow.app] settings.json ignored: not an object ({type(s).__name__})")
        return
    if s.get("language_mode") in ("auto", "en", "bg"):
        config.language_mode = s["language_mode"]
    if s.get("ui_language") in ("bg", "en"):
        config.ui_language = s["ui_language"]
    if isinstance(s.get("ollama_model"), str) and s["ollama_model"]:
        config.ollama_model = s["ollama_model"]
    if s.get("stt_engine") in _STT_ENGINES:
        config.stt_engine = s["stt_engine"]
    elif "stt_engine" in s:
        print(f"[flow.app] settings.json: unknown stt_engine {s['stt_engine']!r} ignored")
    if isinstance(s.get("stt_model"), str) and s["stt_model"]:
        config.stt_model = s["stt_model"]
    if isinstance(s.get("speaker_rhotacism"), bool):
        config.speaker_rhotacism = s["speaker_rhotacism"]
    if "hotkey" in s:
        from ..hotkey import VALID_HOTKEYS

        if s["hotkey"] in VALID_HOTKEYS:
            config.hotkey = s["hotkey"]
        else:
            print(f"[flow.app] settings.json: unknown hotkey {s['hotkey']!r} ignored")


def save_settings(config: FlowConfig, path: str = "settings.json") -> None:
    """Merge-write: existing keys survive (the installer owns stt_* and may
    add more later); we refresh the five contract keys from config."""
    s: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            s = loaded
    except Exception:
        pass  # missing/corrupt file: write a fresh one
    s["language_mode"] = config.language_mode
    s["ui_language"] = config.ui_language
    s["ollama_model"] = config.ollama_model
    s["hotkey"] = config.hotkey
    s["stt_engine"] = config.stt_engine
    s["stt_model"] = config.stt_model
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception as e:
        print(f"[flow.app] settings.json write failed: {e!r}")


def _load_tray_image():
    """Tray icon, best first: the full-color app icon (assets/app-icon.png —
    the black menu-bar template glyph is invisible on a dark taskbar), then
    the glyph, then a drawn brand-color placeholder — never icon-less."""
    from PIL import Image, ImageDraw

    # Absolute paths, not CWD-relative: the app may be launched with a
    # different working directory than the runtime dir.
    rt = paths.runtime_dir()
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for candidate in (os.path.join(rt, "app-icon.png"),               # installer copy
                      os.path.join(repo, "assets", "app-icon.png"),   # repo dev run
                      os.path.join(rt, "menubar-icon.png"),           # older runtime dirs
                      os.path.join(repo, "assets", "menubar-icon.png")):
        if os.path.exists(candidate):
            try:
                print(f"[flow.app] tray icon: {candidate}")
                return Image.open(candidate)
            except Exception as e:
                print(f"[flow.app] tray icon {candidate!r} unreadable: {e!r}")
    print("[flow.app] tray icon: falling back to drawn placeholder")
    # solid brand colors (visible on dark AND light taskbars): purple
    # squircle + a minimal teal waveform
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 61, 61), radius=14, fill=(60, 52, 137, 255))
    for x0, y0, x1, y1 in ((16, 24, 23, 39), (28, 15, 35, 48), (40, 24, 47, 39)):
        d.rounded_rectangle((x0, y0, x1, y1), radius=3, fill=(93, 202, 165, 255))
    return img


class PrivaVoxApp:
    def __init__(self, config: FlowConfig | None = None) -> None:
        import pystray

        self.config = config or FlowConfig()
        load_settings(self.config, _settings_path())
        from .. import i18n
        i18n.set_language(self.config.ui_language)
        self._state = "warming"
        self._status_text = t("Зареждане на моделите…", "Loading models…")
        self._models: list[str] = []

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
        # job = (audio, focus verdict snapshotted at on_stop); None = quit
        self._jobs: queue.Queue[tuple[np.ndarray, tuple[str, str]] | None] = queue.Queue()

        self._icon = pystray.Icon(
            "PrivaVox",
            icon=_load_tray_image(),
            title=t("PrivaVox — зареждане…", "PrivaVox — loading…"),
            menu=self._build_menu(pystray),
        )

    # ---- menu ---------------------------------------------------------------

    def _build_menu(self, pystray):
        # pystray validates an action's arity via co_argcount, which COUNTS
        # positional params that have defaults — so `lambda icon, item, k=key:`
        # reads as 3 args and pystray raises ValueError. Making the captured
        # value keyword-ONLY (after `*`) keeps it out of co_argcount (it lands
        # in co_kwonlyargcount) while still binding the loop variable.
        lang_items = [
            pystray.MenuItem(
                label,
                lambda icon, item, *, k=key: self.set_language_mode(k),
                checked=lambda item, k=key: self.config.language_mode == k,
                radio=True,
            )
            for key, label in _langs()
        ]
        hotkey_items = [
            pystray.MenuItem(
                label,
                lambda icon, item, *, k=key: self.set_hotkey(k),
                checked=lambda item, k=key: self._hotkey_name() == k,
                radio=True,
            )
            for key, label in _hotkeys()
        ]
        ui_lang_items = [
            pystray.MenuItem(
                label,
                lambda icon, item, *, k=key: self.set_ui_language(k),
                checked=lambda item, k=key: self.config.ui_language == k,
                radio=True,
            )
            for key, label in _UI_LANGS
        ]
        return pystray.Menu(
            pystray.MenuItem(lambda item: self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("Език на диктовката", "Dictation language"),
                             pystray.Menu(*lang_items)),
            pystray.MenuItem(t("Език на интерфейса / UI language", "UI language / Език на интерфейса"),
                             pystray.Menu(*ui_lang_items)),
            pystray.MenuItem(t("Клавиш за диктовка", "Dictation key"),
                             pystray.Menu(*hotkey_items)),
            pystray.MenuItem(t("AI модел", "AI model"), pystray.Menu(self._model_items)),
            pystray.MenuItem(t("Отвори личния речник", "Open personal dictionary"),
                             self._open_dictionary),
            pystray.MenuItem(t("Покажи лога", "Show log"), self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("Спри PrivaVox", "Quit PrivaVox"), self._graceful_quit),
            # hidden default item: LEFT tray click flashes the status HUD —
            # the win32 stand-in for the mac Dock-icon click
            pystray.MenuItem(t("Статус", "Status"), self._show_status_hud,
                             default=True, visible=False),
        )

    def _model_items(self):
        import pystray

        if not self._models:
            yield pystray.MenuItem(
                t("(списъкът идва от Ollama…)", "(list comes from Ollama…)"),
                None, enabled=False)
            return
        for name in self._models:
            yield pystray.MenuItem(
                name,
                lambda icon, item, *, n=name: self.set_ollama_model(n),
                checked=lambda item, n=name: self.config.ollama_model == n,
                radio=True,
            )

    def _update_menu(self) -> None:
        # win32 pystray builds the HMENU once — dynamic text/checkmarks only
        # refresh via update_menu(); safe to call before run() too.
        try:
            self._icon.update_menu()
        except Exception as e:
            print(f"[flow.app] tray menu update failed: {e!r}")

    def _show_status_hud(self, _icon=None, _item=None) -> None:
        self._hud.show(self._status_text, hide_after=2.0)

    # ---- model / language switching (mirrors flow/app.py) --------------------

    def set_ollama_model(self, name: str) -> None:
        from .. import cleanup

        self.config.ollama_model = name
        self._update_menu()
        save_settings(self.config, _settings_path())
        print(f"[flow.app] ollama model -> {name}")
        self._hud.show(t(f"Модел: {name} — зареждане…", f"Model: {name} — loading…"),
                       hide_after=None)

        def warm() -> None:
            try:
                secs = cleanup.warm_up(self.config)
                print(f"[flow.app] model {name} warmed in {secs:.1f}s")
                # only surface "ready" while idle-ish — a warm-up finishing
                # mid-dictation must not clobber recording/processing UI
                if self._state in ("warming", "ready"):
                    self._hud.show(t(f"Модел готов ({secs:.1f} s)", f"Model ready ({secs:.1f} s)"),
                                   "green", hide_after=1.5)
                    self._set("ready", t(f"Модел: {name}", f"Model: {name}"))
            except Exception as e:
                reason = str(e) or repr(e)
                self._hud.show(t(f"Моделът не тръгна: {reason}", f"Model failed to start: {reason}"),
                               "red", hide_after=3.0)
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

    def set_language_mode(self, key: str) -> None:
        label = dict(_langs())[key]
        self.config.language_mode = key
        self._update_menu()
        save_settings(self.config, _settings_path())
        print(f"[flow.app] language mode -> {key}")
        self._hud.show(t(f"Език: {label}", f"Language: {label}"), hide_after=1.2)

    # ---- UI-language switcher (new; mirrors set_language_mode) ----------------

    def set_ui_language(self, key: str) -> None:
        """Menu pick: switch the interface language, persist it, and refresh
        the menu + status so every t(...) label reflects the new language."""
        from .. import i18n

        self.config.ui_language = key
        i18n.set_language(key)
        save_settings(self.config, _settings_path())
        print(f"[flow.app] ui language -> {key}")
        # When idle, re-render the ready prompt in the new language; an
        # in-flight recording/processing status is left untouched (its dynamic
        # text isn't reconstructable) — the next _set() emits the new language.
        # _update_menu() (via _set, and again below) rebuilds every menu label.
        if self._state == "ready":
            self._set("ready", t(f"Готово — задръж {self._hotkey_hint()} и говори",
                                 f"Ready — hold {self._hotkey_hint()} and speak"))
        else:
            self._update_menu()
        self._hud.show(t(f"Език на интерфейса: {dict(_UI_LANGS)[key]}",
                         f"UI language: {dict(_UI_LANGS)[key]}"), hide_after=1.2)

    # ---- hotkey picker (mirrors flow/app.py set_hotkey) -----------------------

    def _hotkey_name(self) -> str:
        """config.hotkey with "auto" resolved to the platform default."""
        from .. import hotkey as hotkey_mod

        try:
            return hotkey_mod.resolve_hotkey_name(self.config.hotkey)
        except ValueError:
            return self.config.hotkey

    def _hotkey_hint(self) -> str:
        """How "задръж … и говори" texts name the current key."""
        name = self._hotkey_name()
        return _hotkey_hints().get(name, name)

    def set_hotkey(self, key: str) -> None:
        """Menu pick: persist + live-swap the push-to-talk listener."""
        label = dict(_hotkeys()).get(key, key)
        with self._rec_lock:
            if self._recording_active:
                # never kill an in-flight dictation from a menu click
                self._hud.show(t("⚠ Записът тече — пусни клавиша и опитай пак",
                                 "⚠ Recording in progress — release the key and try again"),
                               "orange", hide_after=2.5)
                return
            self.config.hotkey = key
            old, self._ptt = self._ptt, None
            if old is not None:
                old.stop()  # signal only — the old thread unwinds by itself
        self._update_menu()
        save_settings(self.config, _settings_path())
        print(f"[flow.app] hotkey -> {key}")
        if old is not None:
            self._start_ptt()  # same callbacks, fresh listener + thread
        # before _boot reaches the listener there is nothing to restart: the
        # first PushToTalk is built from config.hotkey, which is already set
        self._hud.show(t(f"Клавиш: {label}", f"Key: {label}"), hide_after=1.5)

    def _start_ptt(self) -> None:
        from .. import hotkey as hotkey_mod

        on_start, on_stop, on_cancel = self._ptt_callbacks
        ptt = hotkey_mod.PushToTalk(on_start, on_stop, on_cancel, self.config)
        self._ptt = ptt
        threading.Thread(target=self._run_ptt, args=(ptt,), daemon=True).start()

    def _run_ptt(self, ptt) -> None:
        try:
            ptt.run_forever()
        except Exception as e:
            if self._ptt is ptt:  # a replaced listener winding down is no error
                self._set("error", t("Hotkey слушателят падна — виж лога",
                                     "Hotkey listener crashed — see the log"))
            print(f"[flow.app] hotkey listener failed: {e!r}")

    # ---- UI plumbing ----------------------------------------------------------

    def _set(self, state: str, text: str) -> None:
        self._state = state
        self._status_text = text
        print(f"[flow.app] {state}: {text}", flush=True)
        # tray tooltip carries the live state (127-char Win32 limit)
        try:
            self._icon.title = f"{_STATE_ICON.get(state, '🎙')} PrivaVox — {text}"[:120]
        except Exception:
            pass  # icon not created yet / shutting down
        self._update_menu()

    def _open_dictionary(self, _icon=None, _item=None) -> None:
        self._open_path(os.path.abspath(self.config.dictionary_path))

    def _open_log(self, _icon=None, _item=None) -> None:
        self._open_path(paths.log_path())

    @staticmethod
    def _open_path(path: str) -> None:
        try:
            os.startfile(path)  # Windows: default handler (Notepad for .txt)
        except AttributeError:  # mac dev run of the win32 shell
            subprocess.Popen(["open", "-t", path])
        except Exception as e:
            print(f"[flow.app] can't open {path!r}: {e!r}")

    # ---- startup (duplicates flow/app.py _boot minus the TCC permissions) -----

    def _boot(self) -> None:
        from .. import audio as audio_mod
        from .. import cleanup, focus, stt

        self._set("warming", t("Затопляне на Whisper…", "Warming up Whisper…"))
        try:
            stt.warm_up(self.config)
        except Exception as e:
            self._set("error", t(f"Whisper не тръгна: {e!r}", f"Whisper failed to start: {e!r}")[:80])
            return

        self._set("warming", t("Затопляне на BgGPT (Ollama)…", "Warming up BgGPT (Ollama)…"))
        try:
            cleanup.warm_up(self.config)
        except Exception as e:
            print(f"[flow.app] LLM warm-up failed: {e!r}")
            models = self._list_ollama_models()
            if models is None:
                # mac wording, Windows remedy (brew does not exist here)
                self._set("error", t("Ollama не отговаря — стартирай приложението Ollama",
                                     "Ollama isn't responding — start the Ollama app"))
            elif _norm_model(self.config.ollama_model) not in {_norm_model(m) for m in models}:
                self._set("error",
                          t(f"Моделът {self.config.ollama_model} липсва — пусни 'Install-PrivaVox.bat'",
                            f"Model {self.config.ollama_model} is missing — run 'Install-PrivaVox.bat'"))
            else:
                self._set("error", t(f"BgGPT не тръгна: {e}", f"BgGPT failed to start: {e}")[:80])
            return

        dictionary = load_configured_dictionary(self.config)
        print(f"[flow.app] dictionary: {len(dictionary.terms)} term(s)")

        models = self._list_ollama_models()
        if models:
            self._models = models
            self._update_menu()

        # Microphone: opening a short input stream triggers the Windows mic
        # consent prompt now instead of mid-first-dictation. Run with a
        # timeout — a hung device open must not brick the boot.
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
            # Runs in parallel with an already-recording mic. On Windows v1
            # flow/focus.py always answers ("unknown", …) — the verdict is a
            # no-op passenger, kept so the insert-time semantics (clipboard
            # safety net) stay identical to the mac worker below.
            verdict, detail = focus.focused_text_target()
            with self._rec_lock:
                if gen != self._rec_gen or not self._recording_active:
                    return  # that recording is already gone — verdict is moot
                self._focus_verdict = (verdict, detail)
                if verdict == "no-text":
                    # unreachable off-mac in v1 (probe never says no-text)
                    self._recording_active = False
                    recorder.cancel()
                    print(f"[flow.app] recording cancelled: no text field ({detail})")
                    _play("Basso")
                    self._set("ready", t("Няма избрано текстово поле — кликни в поле",
                                         "No text field selected — click into a field"))
                    self._hud.show(t("⚠ Няма избрано текстово поле — кликни в поле и опитай пак",
                                     "⚠ No text field selected — click into a field and try again"),
                                   "orange", hide_after=2.5)
                elif verdict == "soft-no-text":
                    print(f"[flow.app] doubtful text target ({detail}) — recording continues")
                    self._hud.show(t("● Записва… ⚠ вероятно няма текстово поле",
                                     "● Recording… ⚠ probably no text field"), "orange")

        def on_start() -> None:
            # Mic FIRST — recording begins the instant the key goes down;
            # the probe runs in parallel and cancels retroactively (mac note).
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
                    self._set("error", t("Микрофонът отказа — виж Settings → Privacy → Microphone",
                                         "Microphone refused — see Settings → Privacy → Microphone"))
                    self._hud.show(t("Микрофонът отказа достъп", "Microphone denied access"),
                                   "red", hide_after=2.5)
                    print(f"[flow.app] mic error: {e!r}")
                    return
                self._recording_active = True
                self._rec_gen += 1
                gen = self._rec_gen
                self._focus_verdict = ("unknown", "probe-pending")
            self._set("recording", t("Записва… (Esc отказва)", "Recording… (Esc cancels)"))
            self._hud.show(t("● Записва…", "● Recording…"), "red")
            _play("Tink")
            threading.Thread(target=_probe_focus, args=(gen,), daemon=True).start()

        def on_stop(held_s: float) -> None:
            with self._rec_lock:
                if not self._recording_active:
                    return
                self._recording_active = False
                audio = recorder.stop()
                # snapshot the parallel probe's verdict: the worker must NOT
                # re-probe at insert time (no second round-trip on the hot
                # path); a probe still pending simply means "unknown"
                verdict = self._focus_verdict
            if held_s < self.config.min_recording_s:
                self._set("ready", t(f"Готово — задръж {self._hotkey_hint()} и говори",
                                     f"Ready — hold {self._hotkey_hint()} and speak"))
                self._hud.hide()
                return
            self._set("processing", t("Обработва…", "Processing…"))
            if recorder.truncated:
                # the warning pill doubles as the processing indicator —
                # a second show() would wipe it before it can be read
                print(f"[flow.app] recording capped at {self.config.max_recording_s:.0f}s")
                self._hud.show(t(f"Записът е орязан до {self.config.max_recording_s:.0f} s",
                                 f"Recording capped at {self.config.max_recording_s:.0f} s"),
                               "orange", hide_after=2.5)
            else:
                self._hud.show(t("Обработва…", "Processing…"))
            self._jobs.put((audio, verdict))

        def on_cancel() -> None:
            with self._rec_lock:
                if not self._recording_active:
                    return
                self._recording_active = False
                recorder.cancel()
            self._set("ready", t("Отказано — готово за нова диктовка",
                                 "Cancelled — ready for a new dictation"))
            self._hud.show(t("✕ Отказано", "✕ Cancelled"), hide_after=1.0)

        self._set("ready", t(f"Готово — задръж {self._hotkey_hint()} и говори",
                             f"Ready — hold {self._hotkey_hint()} and speak"))
        self._hud.show(t(f"PrivaVox е готов — задръж {self._hotkey_hint()} и говори",
                         f"PrivaVox is ready — hold {self._hotkey_hint()} and speak"),
                       hide_after=3.0)
        _play("Glass")

        # callbacks live on self so the hotkey picker can rebuild the listener
        self._ptt_callbacks = (on_start, on_stop, on_cancel)
        self._start_ptt()

    # ---- dictation worker (duplicates flow/app.py _worker; Ctrl+V wording) ----

    def _worker(self) -> None:
        from .. import insert
        from ..__main__ import run_pipeline

        while True:
            job = self._jobs.get()
            if job is None:
                return  # graceful quit: _graceful_quit() waits for this
            audio, (verdict, field_detail) = job
            try:
                result = run_pipeline(audio, self.config)
                if result.status == "too-short":
                    self._set("ready", t("Твърде кратко — игнорирано", "Too short — ignored"))
                    self._hud.hide()
                elif result.status == "silence":
                    self._set("ready", t("Тишина — нищо не е вмъкнато",
                                         "Silence — nothing inserted"))
                    self._hud.show(t("Тишина — нищо не е вмъкнато",
                                     "Silence — nothing inserted"), hide_after=1.6)
                elif result.status == "hallucination":
                    print(f"[flow.app] hallucination filtered: {result.raw_text!r}")
                    self._set("ready", t("Шум/халюцинация — нищо не е вмъкнато",
                                         "Noise/hallucination — nothing inserted"))
                    self._hud.show(t("Само шум — нищо не е вмъкнато",
                                     "Just noise — nothing inserted"), hide_after=1.6)
                else:
                    # NB: keep the timings under a name other than `t` — the
                    # module-level t() translator is used all over this method,
                    # and a local `t` would shadow it for the whole function.
                    timings = result.timings
                    # consecutive dictations must not glue together:
                    # "…точка.Искам" → "…точка. Искам"
                    text = result.cleaned_text
                    if self._last_inserted and not self._last_inserted[-1].isspace():
                        text = " " + text
                    # ALWAYS paste — the focus verdict must never block an
                    # insert. The verdict was probed DURING recording and
                    # travels with the job (on Windows v1 it is "unknown").
                    status = insert.paste_text(text, self.config)
                    print(f"[flow.app] lang={result.language} (whisper detected: {result.detected_language})")
                    print(f"[flow.app] raw:     {result.raw_text}")
                    print(f"[flow.app] cleaned: {result.cleaned_text}")
                    if result.used_fallback:
                        print(f"[flow.app] cleanup fell back to raw transcript ({result.fallback_reason})")
                    print(f"[flow.app] insert: {status} | field: {field_detail} | total {timings.get('total_s', 0):.2f}s")
                    if status == "clipboard-only":
                        # nothing was pasted: no success sound, no _last_inserted
                        self._last_inserted = ""
                        self._set("ready", t("Вмъкването не мина — текстът е в клипборда (Ctrl+V)",
                                             "Insert failed — text is on the clipboard (Ctrl+V)"))
                        self._hud.show(t("⚠ Вмъкването не мина — текстът е в клипборда, Ctrl+V",
                                         "⚠ Insert failed — text is on the clipboard, Ctrl+V"),
                                       "orange", hide_after=3.0)
                    elif verdict in ("no-text", "soft-no-text"):
                        # doubtful paste target: keep the text on the
                        # clipboard as a safety net (paste_text restored it)
                        insert.set_clipboard(result.cleaned_text)
                        self._last_inserted = ""
                        self._set("ready", t("Няма поле? Текстът е и в клипборда (Ctrl+V)",
                                             "No field? The text is also on the clipboard (Ctrl+V)"))
                        self._hud.show(t("⚠ Няма поле? Текстът е и в клипборда — Ctrl+V",
                                         "⚠ No field? The text is also on the clipboard — Ctrl+V"),
                                       "orange", hide_after=3.0)
                    elif result.used_fallback:
                        self._last_inserted = text
                        _play("Pop")
                        reason = (result.fallback_reason or "")[:40]
                        self._set("ready", t(f"Вмъкнато без чистене ({timings.get('total_s', 0):.1f} s)",
                                             f"Inserted without cleanup ({timings.get('total_s', 0):.1f} s)"))
                        self._hud.show(t(f"Вмъкнато без чистене ({reason})",
                                         f"Inserted without cleanup ({reason})"),
                                       "orange", hide_after=2.5)
                    else:
                        self._last_inserted = text
                        _play("Pop")
                        self._set("ready", t(f"Вмъкнато ({timings.get('total_s', 0):.1f} s) — готово",
                                             f"Inserted ({timings.get('total_s', 0):.1f} s) — done"))
                        self._hud.show(t("✓ Вмъкнато", "✓ Inserted"), "green", hide_after=1.2)
            except Exception as e:
                _play("Basso")
                self._set("ready", t("Грешка при обработка — виж лога",
                                     "Processing error — see the log"))
                self._hud.show(t("Грешка — виж лога", "Error — see the log"),
                               "red", hide_after=2.5)
                print(f"[flow.app] pipeline error: {e!r}")

    # ---- quit ------------------------------------------------------------------

    def _graceful_quit(self, _icon=None, _item=None) -> None:
        """Single quit path (menu item): an in-flight dictation gets up to
        3 s to finish (or at least land on the clipboard) before the process
        dies — same drain semantics as the mac shell."""
        if self._quitting:
            return
        self._quitting = True
        print("[flow.app] quit requested — draining dictation queue (max 3 s)")
        self._jobs.put(None)  # worker finishes queued jobs, then exits
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)
        self._hud.close()
        self._icon.stop()

    # ---- run ---------------------------------------------------------------------

    def run(self) -> None:
        def setup(icon) -> None:
            icon.visible = True
            threading.Thread(target=self._boot, daemon=True).start()

        self._icon.run(setup)


def _duplicate_instance_dialog() -> None:
    """tkinter stand-in for the mac osascript dialog (no osascript here)."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("PrivaVox",
                               t("PrivaVox вече върви.", "PrivaVox is already running."))
        root.destroy()
    except Exception as e:
        print(f"[flow.app] duplicate-instance dialog failed: {e!r}")


def main() -> None:
    from .. import singleinstance

    _setup_logging()
    if not singleinstance.acquire():
        print("[flow.app] PrivaVox вече върви — втора инстанция не се стартира.")
        _duplicate_instance_dialog()
        sys.exit(0)
    print(f"[flow.app] PrivaVox v{__version__} tray app starting (win32 shell)")
    # Belt-and-suspenders: _setup_logging() above (and the flow.app preamble)
    # have redirected stdout/stderr to PrivaVox.log by now, so a crash while
    # constructing or running the tray app lands in the log with a full
    # traceback instead of dying silently under pythonw.
    try:
        PrivaVoxApp().run()
    except Exception:
        import traceback as _tb
        print("[flow.app] FATAL in PrivaVoxApp().run():")
        _tb.print_exc()
        raise


if __name__ == "__main__":
    main()
