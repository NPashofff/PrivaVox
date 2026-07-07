"""Central configuration for the Flow dictation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FlowConfig:
    # --- Hotkey -----------------------------------------------------------
    # Push-to-talk key. Resolved to a pynput key object in flow.hotkey.
    # "auto" picks the platform default there: "alt_r" (right Option) on
    # macOS — behavior identical to the old fixed default — and "ctrl_r"
    # (right Ctrl) on Windows, where right Alt is AltGr on BG keyboards and
    # types characters. Explicit names ("alt_r", "ctrl_r", …) are honored
    # everywhere.
    hotkey: str = "auto"

    # --- Audio ------------------------------------------------------------
    sample_rate: int = 16_000          # Hz, what Whisper expects
    channels: int = 1                  # mono
    min_recording_s: float = 0.4       # shorter recordings are discarded (tap debounce)
    max_recording_s: float = 60.0      # recording buffer is capped at this length
    # RMS energy gate: recordings whose overall RMS (float32, full scale 1.0)
    # is below this are treated as silence and never sent to STT.
    # Reference points on this machine: speech clips ~0.15, digital silence 0.0.
    energy_threshold: float = 0.005

    # --- STT ----------------------------------------------------------------
    stt_model_repo: str = "mlx-community/whisper-large-v3-turbo"
    # STT engine behind flow/stt.py: "auto" picks per platform (darwin →
    # "mlx", win32 → "faster-whisper"). Explicit values are honored on ANY
    # platform (e.g. the faster-whisper CPU path on a Mac — that is the W2
    # test): "mlx", "faster-whisper" (device auto: CUDA if usable, else CPU),
    # "faster-whisper-cuda" (float16), "faster-whisper-cpu" (int8). The
    # -cuda/-cpu values are what Install-PrivaVox.ps1 writes to settings.json.
    stt_engine: str = "auto"
    # STT model: "auto" resolves per engine in flow/stt.py (mlx →
    # stt_model_repo above; faster-whisper →
    # "deepdml/faster-whisper-large-v3-turbo-ct2"). Set explicitly to force a
    # HF repo or a faster-whisper size name ("small" is the weak-CPU choice
    # Install-PrivaVox.ps1 offers).
    stt_model: str = "auto"
    # "auto": use Whisper's detected language; if the top detection is neither
    # "en" nor "bg", force whichever of the two is more probable.
    # "en" / "bg": always force that language.
    language_mode: str = "auto"
    # UI language for menus/HUD, set by the installer, default Bulgarian.
    ui_language: str = "bg"

    # --- Cleanup LLM (Ollama) ----------------------------------------------
    ollama_url: str = "http://localhost:11434/v1/chat/completions"
    ollama_model: str = "todorov/bggpt"   # BgGPT-Gemma-3-4B-IT Q4_K_M
    ollama_timeout_s: float = 30.0        # per-request timeout; on expiry we fall back to raw
    ollama_keep_alive: str = "30m"        # keep the model resident between dictations

    # --- Insertion ----------------------------------------------------------
    clipboard_restore_delay_s: float = 0.3  # wait after Cmd-V before restoring clipboard
    key_event_delay_s: float = 0.03         # small gap between key down and key up
    # direct-typing insertion (remote sessions): pause between per-character
    # Unicode keystrokes — RDP forwards ONE char per event over the network,
    # so a small gap keeps ordering reliable without being humanly slow
    type_chunk_delay_s: float = 0.008

    # Languages this pipeline supports (used by the auto-detect fallback).
    supported_languages: tuple[str, ...] = field(default=("en", "bg"))

    # --- ASR sidecar (flow/server.py) ---------------------------------------
    # localhost-only HTTP server exposing an OpenAI-compatible
    # /v1/audio/transcriptions endpoint. Never bind to 0.0.0.0.
    server_host: str = "127.0.0.1"
    server_port: int = 8880

    # --- Personal dictionary (Phase 3) --------------------------------------
    # Plain-text file, one term per line ("#" comments and blank lines
    # ignored), EN+BG mixed. A missing file means an empty dictionary (no
    # error). See flow/dictionary.py and docs/phase3-dictionary.md.
    dictionary_path: str = "dictionary.txt"

    # --- Accessibility: rhotacism compensation (Phase 3) --------------------
    # Optional compensation for speakers with rhotacism, an impediment where
    # an intended "р"/"r" can reach Whisper as "л"/"в" (Bulgarian) or
    # "w"/"l" (English), or be dropped. When True, a compact speaker-profile
    # block is added to the cleanup prompt (flow/cleanup.py, RHOTACISM_BLOCK)
    # so the LLM can restore phonetically-plausible substitutions from
    # context. Off by default; opt in via settings.json
    # ({"speaker_rhotacism": true} — both app shells load it) or by
    # constructing FlowConfig(speaker_rhotacism=True). When False, the
    # cleanup prompt is byte-identical to the tuned prompt (mirrors the
    # empty-dictionary guarantee).
    speaker_rhotacism: bool = False


DEFAULT = FlowConfig()
