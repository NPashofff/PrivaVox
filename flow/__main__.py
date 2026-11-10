"""Flow entry point.

Daemon:      python -m flow [--dry-run] [--lang auto|en|bg]
File mode:   python -m flow --file path.wav [--lang en|bg]   (prints, never pastes)
ASR sidecar: python -m flow.server [--port N]   (OpenAI-compatible HTTP API; see flow/server.py)
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import __version__, cleanup, stt
from .config import DEFAULT, FlowConfig
from .dictionary import load_configured_dictionary


@dataclass
class PipelineResult:
    status: str                    # "ok" | "too-short" | "silence" | "hallucination"
    language: str | None = None    # decode language actually used
    detected_language: str | None = None
    raw_text: str = ""
    cleaned_text: str = ""
    used_fallback: bool = False
    fallback_reason: str | None = None
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def has_text(self) -> bool:
        return self.status == "ok" and bool(self.cleaned_text)


def run_pipeline(
    audio: np.ndarray, config: FlowConfig = DEFAULT, min_duration_s: float | None = None
) -> PipelineResult:
    """Gate -> STT -> hallucination filter -> LLM cleanup. Never raises on content."""
    from . import audio as audio_mod  # numpy-only helpers; sounddevice import is harmless

    t_start = time.monotonic()
    timings: dict[str, float] = {}
    min_dur = config.min_recording_s if min_duration_s is None else min_duration_s

    duration = audio_mod.duration_s(audio, config.sample_rate)
    if duration < min_dur:
        return PipelineResult(status="too-short", timings={"total_s": 0.0})

    energy = audio_mod.rms(audio)
    if energy < config.energy_threshold:
        return PipelineResult(status="silence", timings={"total_s": 0.0})

    stt_result = stt.transcribe(audio, language=None, config=config)
    timings["stt_detect_s"] = round(stt_result["detect_time_s"], 3)
    timings["stt_decode_s"] = round(stt_result["decode_time_s"], 3)

    raw_text = stt_result["text"]
    if stt.is_hallucination(raw_text):
        timings["total_s"] = round(time.monotonic() - t_start, 3)
        return PipelineResult(
            status="hallucination",
            language=stt_result["language"],
            detected_language=stt_result["detected_language"],
            raw_text=raw_text,
            timings=timings,
        )

    cleaned = cleanup.clean_transcript(raw_text, config)
    timings["cleanup_s"] = round(cleaned.latency_s, 3)
    timings["total_s"] = round(time.monotonic() - t_start, 3)

    return PipelineResult(
        status="ok",
        language=stt_result["language"],
        detected_language=stt_result["detected_language"],
        raw_text=raw_text,
        cleaned_text=cleaned.text,
        used_fallback=cleaned.used_fallback,
        fallback_reason=cleaned.fallback_reason,
        timings=timings,
    )


def _log_result(result: PipelineResult) -> None:
    t = result.timings
    print(f"[flow] lang={result.language} (whisper detected: {result.detected_language})")
    print(f"[flow] raw:     {result.raw_text}")
    print(f"[flow] cleaned: {result.cleaned_text}")
    if result.used_fallback:
        print(f"[flow] cleanup fell back to raw transcript ({result.fallback_reason})")
    print(
        "[flow] timings: lang-detect %.2fs | stt %.2fs | cleanup %.2fs | total %.2fs"
        % (
            t.get("stt_detect_s", 0.0),
            t.get("stt_decode_s", 0.0),
            t.get("cleanup_s", 0.0),
            t.get("total_s", 0.0),
        )
    )


# --------------------------------------------------------------------------
# File mode (used by tests): transcribe+clean a file and PRINT the result.
# --------------------------------------------------------------------------

def run_file(path: str, config: FlowConfig) -> PipelineResult:
    audio = stt.load_audio(path)
    result = run_pipeline(audio, config)
    return result


def _file_mode(path: str, config: FlowConfig) -> int:
    dictionary = load_configured_dictionary(config)
    print(f"[flow] dictionary: {len(dictionary.terms)} term(s) loaded from {config.dictionary_path!r}")
    result = run_file(path, config)
    if result.status != "ok":
        print(f"[flow] nothing to insert (status: {result.status})")
        if result.raw_text:
            print(f"[flow] filtered STT output was: {result.raw_text!r}")
        return 1
    _log_result(result)
    return 0


# --------------------------------------------------------------------------
# Daemon mode
# --------------------------------------------------------------------------

def _print_startup(config: FlowConfig, dry_run: bool) -> None:
    from . import insert

    print(f"[flow] Flow v{__version__} — fully-local dictation (EN/BG)")
    print(f"[flow] warming STT model {config.stt_model_repo!r} ...", flush=True)
    stt_warm = stt.warm_up(config)
    print(f"[flow]   STT ready in {stt_warm:.2f}s")
    print(f"[flow] warming LLM {config.ollama_model!r} via Ollama ...", flush=True)
    try:
        llm_warm = cleanup.warm_up(config)
        print(f"[flow]   LLM ready in {llm_warm:.2f}s")
    except cleanup.CleanupWarmupError as e:
        # Stay up (STT still works, tests use this path) but say it loudly:
        # every dictation will be inserted raw until Ollama serves the model.
        print(f"[flow]   LLM warm-up FAILED: {e}")
        print(f"[flow]   cleanup is DOWN — dictations will be inserted RAW until "
              f"Ollama serves {config.ollama_model!r}")
    print("[flow] hotkey: hold RIGHT OPTION to record, release to insert; Esc while recording cancels")
    print(f"[flow] language mode: {config.language_mode}")
    dictionary = load_configured_dictionary(config)
    print(f"[flow] dictionary: {len(dictionary.terms)} term(s) loaded from {config.dictionary_path!r}")
    if dry_run:
        print("[flow] DRY RUN: results are printed, nothing is pasted")
    print("[flow] permissions (System Settings -> Privacy & Security):")
    print("[flow]   - Microphone      -> your terminal app (macOS prompts on first recording)")
    print("[flow]   - Input Monitoring -> your terminal app (needed for the hotkey)")
    print("[flow]   - Accessibility    -> your terminal app (needed to auto-paste Cmd-V)")
    if not dry_run:
        if insert.can_post_events():
            print("[flow] Accessibility: OK — dictations will be pasted automatically")
        else:
            print("[flow] Accessibility: MISSING — dictated text will stay on the clipboard;")
            print("[flow]   натисни Cmd-V ръчно, или дай Accessibility права и рестартирай")
    print("[flow] ready.\n")


def _daemon(config: FlowConfig, dry_run: bool) -> int:
    from . import audio as audio_mod
    from . import hotkey as hotkey_mod
    from . import insert
    from . import singleinstance

    # one Flow per machine, no matter how it was started (Flow.app,
    # python -m flow.app, python -m flow, Flow.command) — двойни вмъквания
    # от два daemon-а са най-лошият failure mode. File mode (--file) never
    # takes the lock: tests run alongside a live daemon.
    if not singleinstance.acquire():
        print("[flow] Друга инстанция на Flow вече върви (flow.lock е зает).")
        print("[flow] Спри я първо (менюто ѝ или `pkill -f 'python.*-m flow'`) и опитай пак.")
        return 1

    _print_startup(config, dry_run)

    recorder = audio_mod.Recorder(config)
    jobs: queue.Queue[np.ndarray | None] = queue.Queue()

    def worker() -> None:
        while True:
            audio = jobs.get()
            if audio is None:
                return
            try:
                result = run_pipeline(audio, config)
                if result.status == "too-short":
                    print("[flow] recording too short — ignored (debounce)")
                elif result.status == "silence":
                    print("[flow] recording is silence — nothing inserted")
                elif result.status == "hallucination":
                    print(f"[flow] STT output looked like a hallucination ({result.raw_text!r}) — nothing inserted")
                else:
                    _log_result(result)
                    if dry_run:
                        print(f"[flow] dry-run — would paste: {result.cleaned_text}")
                    else:
                        t0 = time.monotonic()
                        status = insert.paste_text(result.cleaned_text, config)
                        print(f"[flow] insert: {status} ({time.monotonic() - t0:.2f}s)")
            except Exception as e:  # keep the daemon alive on per-dictation errors
                print(f"[flow] pipeline error: {e!r}")
            finally:
                print()

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    def on_start() -> None:
        try:
            recorder.start()
            print("[flow] ● recording... (release to insert, Esc to cancel)")
        except Exception as e:
            print(f"[flow] could not open microphone: {e!r}")
            print("[flow] check System Settings -> Privacy & Security -> Microphone")

    def on_stop(held_s: float) -> None:
        audio = recorder.stop()
        if recorder.truncated:
            print(f"[flow] recording capped at {config.max_recording_s:.0f}s")
        if held_s < config.min_recording_s:
            print("[flow] tap too short — ignored (debounce)")
            return
        print(f"[flow] ■ processing {audio_mod.duration_s(audio, config.sample_rate):.1f}s of audio ...")
        jobs.put(audio)

    def on_cancel() -> None:
        recorder.cancel()
        print("[flow] ✕ cancelled — nothing inserted")

    ptt = hotkey_mod.PushToTalk(on_start, on_stop, on_cancel, config)
    try:
        ptt.run_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[flow] hotkey listener failed: {e!r}")
        print("[flow] most likely missing permission: System Settings -> Privacy & Security")
        print("[flow]   -> Input Monitoring (and Accessibility) for your terminal app, then restart")
        return 1
    finally:
        jobs.put(None)
    print("\n[flow] bye")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flow", description=__doc__)
    parser.add_argument("--file", help="run the pipeline on an audio file and print the result")
    parser.add_argument("--lang", choices=["auto", "en", "bg"], default=None,
                        help="language mode (default: config, 'auto')")
    parser.add_argument("--dry-run", action="store_true",
                        help="daemon mode: print results instead of pasting")
    args = parser.parse_args(argv)

    config = FlowConfig()
    if args.lang is not None:
        config.language_mode = args.lang

    if args.file:
        return _file_mode(args.file, config)
    return _daemon(config, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
