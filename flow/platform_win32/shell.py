"""PrivaVox Windows shell: pystray system tray + the dictation pipeline UX.

The win32 counterpart of flow/app.py's FlowApp (rumps), launched by
flow.app.main() when sys.platform == "win32":

- system tray icon; live state in the tooltip/left-click HUD (Windows has no
  text in the tray, so the emoji state lives in the tooltip title)
- menu: status line, език на диктовката (auto/bg/en), AI модел (динамично от
  Ollama /api/tags), личен речник, лог, изход
- settings.json load/save (merge-write: language_mode, ollama_model,
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
from .hud_win import HUD

_STATE_ICON = {
    "warming": "⏳",
    "error": "⚠️",
    "ready": "🎙",
    "recording": "🔴",
    "processing": "💭",
}

_LANGS = (("auto", "Автоматично разпознаване"),
          ("bg", "Само български"),
          ("en", "Само английски"))

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
    """pythonw.exe runs with sys.stdout/sys.stderr = None; the first print()
    would crash. Redirect both to %LOCALAPPDATA%\\PrivaVox\\PrivaVox.log —
    the Install-PrivaVox.ps1 contract says the APP creates the log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return  # console run (dev): keep printing there
    os.makedirs(paths.runtime_dir(), exist_ok=True)
    log = open(paths.log_path(), "a", buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


# ---- settings (module-level so the mac suite can test them headlessly) ------

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


def save_settings(config: FlowConfig, path: str = "settings.json") -> None:
    """Merge-write: existing keys survive (the installer owns stt_* and may
    add more later); we refresh the four contract keys from config."""
    s: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            s = loaded
    except Exception:
        pass  # missing/corrupt file: write a fresh one
    s["language_mode"] = config.language_mode
    s["ollama_model"] = config.ollama_model
    s["stt_engine"] = config.stt_engine
    s["stt_model"] = config.stt_model
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception as e:
        print(f"[flow.app] settings.json write failed: {e!r}")


def _load_tray_image():
    """assets/menubar-icon.png via Pillow; a drawn white mic as fallback."""
    from PIL import Image, ImageDraw

    for candidate in ("menubar-icon.png",                       # runtime dir (installer copy)
                      os.path.join("assets", "menubar-icon.png")):  # repo dev run
        if os.path.exists(candidate):
            try:
                return Image.open(candidate)
            except Exception as e:
                print(f"[flow.app] tray icon {candidate!r} unreadable: {e!r}")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((22, 6, 42, 38), radius=10, fill=(255, 255, 255, 255))
    d.arc((14, 18, 50, 50), start=0, end=180, fill=(255, 255, 255, 255), width=4)
    d.rectangle((30, 50, 34, 56), fill=(255, 255, 255, 255))
    d.rectangle((22, 56, 42, 60), fill=(255, 255, 255, 255))
    return img


class PrivaVoxApp:
    def __init__(self, config: FlowConfig | None = None) -> None:
        import pystray

        self.config = config or FlowConfig()
        load_settings(self.config)
        self._state = "warming"
        self._status_text = "Зареждане на моделите…"
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
        # job = (audio, focus verdict snapshotted at on_stop); None = quit
        self._jobs: queue.Queue[tuple[np.ndarray, tuple[str, str]] | None] = queue.Queue()

        self._icon = pystray.Icon(
            "PrivaVox",
            icon=_load_tray_image(),
            title="PrivaVox — зареждане…",
            menu=self._build_menu(pystray),
        )

    # ---- menu ---------------------------------------------------------------

    def _build_menu(self, pystray):
        lang_items = [
            pystray.MenuItem(
                label,
                lambda icon, item, k=key: self.set_language_mode(k),
                checked=lambda item, k=key: self.config.language_mode == k,
                radio=True,
            )
            for key, label in _LANGS
        ]
        return pystray.Menu(
            pystray.MenuItem(lambda item: self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Език на диктовката", pystray.Menu(*lang_items)),
            pystray.MenuItem("AI модел", pystray.Menu(self._model_items)),
            pystray.MenuItem("Отвори личния речник", self._open_dictionary),
            pystray.MenuItem("Покажи лога", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Спри PrivaVox", self._graceful_quit),
            # hidden default item: LEFT tray click flashes the status HUD —
            # the win32 stand-in for the mac Dock-icon click
            pystray.MenuItem("Статус", self._show_status_hud,
                             default=True, visible=False),
        )

    def _model_items(self):
        import pystray

        if not self._models:
            yield pystray.MenuItem("(списъкът идва от Ollama…)", None, enabled=False)
            return
        for name in self._models:
            yield pystray.MenuItem(
                name,
                lambda icon, item, n=name: self.set_ollama_model(n),
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
        save_settings(self.config)
        print(f"[flow.app] ollama model -> {name}")
        self._hud.show(f"Модел: {name} — зареждане…", hide_after=None)

        def warm() -> None:
            try:
                secs = cleanup.warm_up(self.config)
                print(f"[flow.app] model {name} warmed in {secs:.1f}s")
                # only surface "ready" while idle-ish — a warm-up finishing
                # mid-dictation must not clobber recording/processing UI
                if self._state in ("warming", "ready"):
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

    def set_language_mode(self, key: str) -> None:
        label = dict(_LANGS)[key]
        self.config.language_mode = key
        self._update_menu()
        save_settings(self.config)
        print(f"[flow.app] language mode -> {key}")
        self._hud.show(f"Език: {label}", hide_after=1.2)

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
        from .. import cleanup, focus, hotkey as hotkey_mod, stt

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
                # mac wording, Windows remedy (brew does not exist here)
                self._set("error", "Ollama не отговаря — стартирай приложението Ollama")
            elif _norm_model(self.config.ollama_model) not in {_norm_model(m) for m in models}:
                self._set("error",
                          f"Моделът {self.config.ollama_model} липсва — пусни 'Инсталирай PrivaVox.bat'")
            else:
                self._set("error", f"BgGPT не тръгна: {e}"[:80])
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
                    self._set("ready", "Няма избрано текстово поле — кликни в поле")
                    self._hud.show("⚠ Няма избрано текстово поле — кликни в поле и опитай пак",
                                   "orange", hide_after=2.5)
                elif verdict == "soft-no-text":
                    print(f"[flow.app] doubtful text target ({detail}) — recording continues")
                    self._hud.show("● Записва… ⚠ вероятно няма текстово поле", "orange")

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
                    self._set("error", "Микрофонът отказа — виж Settings → Privacy → Microphone")
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
                # re-probe at insert time (no second round-trip on the hot
                # path); a probe still pending simply means "unknown"
                verdict = self._focus_verdict
            if held_s < self.config.min_recording_s:
                self._set("ready", "Готово — задръж десния Ctrl и говори")
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

        self._set("ready", "Готово — задръж десния Ctrl и говори")
        self._hud.show("PrivaVox е готов — задръж десния Ctrl и говори", hide_after=3.0)
        _play("Glass")

        ptt = hotkey_mod.PushToTalk(on_start, on_stop, on_cancel, self.config)
        try:
            ptt.run_forever()
        except Exception as e:
            self._set("error", "Hotkey слушателят падна — виж лога")
            print(f"[flow.app] hotkey listener failed: {e!r}")

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
                    # insert. The verdict was probed DURING recording and
                    # travels with the job (on Windows v1 it is "unknown").
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
                        self._set("ready", "Вмъкването не мина — текстът е в клипборда (Ctrl+V)")
                        self._hud.show("⚠ Вмъкването не мина — текстът е в клипборда, Ctrl+V",
                                       "orange", hide_after=3.0)
                    elif verdict in ("no-text", "soft-no-text"):
                        # doubtful paste target: keep the text on the
                        # clipboard as a safety net (paste_text restored it)
                        insert.set_clipboard(result.cleaned_text)
                        self._last_inserted = ""
                        self._set("ready", "Няма поле? Текстът е и в клипборда (Ctrl+V)")
                        self._hud.show("⚠ Няма поле? Текстът е и в клипборда — Ctrl+V",
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
        messagebox.showwarning("PrivaVox", "PrivaVox вече върви.")
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
    PrivaVoxApp().run()


if __name__ == "__main__":
    main()
