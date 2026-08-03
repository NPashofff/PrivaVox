"""faster-whisper (CTranslate2) STT engine — phase W2 of the Windows port.

Implements the SttEngine protocol from flow/stt.py. Production home is
Windows (picked by stt_engine "auto" there, or the explicit
"faster-whisper-cuda"/"faster-whisper-cpu" that Install-PrivaVox.ps1 writes
to settings.json), but the module is genuinely cross-platform: the CPU int8
path runs — and is unit-tested — on macOS as well.

Device policy (see flow/stt.py _FASTER_WHISPER_DEVICES):
  device="cuda"  → CUDA float16; if CUDA/cuDNN is unusable at load time the
                   engine falls back to CPU int8 LOUDLY (the installer's
                   contract: a missing cuDNN DLL must degrade, not crash).
  device="cpu"   → CPU int8.
  device="auto"  → try CUDA float16 first, else CPU int8 (dev runs without
                   installer-provisioned settings.json).

All language resolution, dictionary prompt building and hallucination
filtering stay in flow/stt.py — this module is raw model access only,
mirroring flow/platform_darwin/stt_mlx.py.
"""

from __future__ import annotations

import numpy as np

try:
    from faster_whisper import WhisperModel, decode_audio
except ImportError as e:  # pragma: no cover - depends on the environment
    raise ImportError(
        "PrivaVox: липсва пакетът faster-whisper (нужен за разпознаване на "
        "речта на тази платформа). Инсталирай зависимостите от "
        "requirements-runtime-win.txt или пусни 'Install-PrivaVox.bat'."
    ) from e

# (device, compute_type) attempts per configured device, in order. float16 on
# CUDA and int8 on CPU are the plan's choices (docs/windows-port-plan.md).
_DEVICE_ATTEMPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "cuda": (("cuda", "float16"), ("cpu", "int8")),
    "auto": (("cuda", "float16"), ("cpu", "int8")),
    "cpu": (("cpu", "int8"),),
}


class FasterWhisperEngine:
    """SttEngine implementation (protocol in flow/stt.py) backed by faster-whisper."""

    name = "faster-whisper"

    def __init__(self, model_repo: str, device: str = "auto") -> None:
        if device not in _DEVICE_ATTEMPTS:
            raise ValueError(
                f"unknown faster-whisper device {device!r}; one of "
                f"{sorted(_DEVICE_ATTEMPTS)}"
            )
        self.model_repo = model_repo
        self.device = device
        # Loaded lazily so constructing the engine (e.g. while wiring config)
        # never downloads weights; flow.stt.warm_up is the intended loader.
        self._model: WhisperModel | None = None
        self._loaded_as: tuple[str, str] | None = None  # (device, compute_type)

    # ---- model loading -----------------------------------------------------

    def _get_model(self) -> WhisperModel:
        if self._model is not None:
            return self._model
        attempts = _DEVICE_ATTEMPTS[self.device]
        last_error: Exception | None = None
        for device, compute_type in attempts:
            try:
                model = WhisperModel(
                    self.model_repo, device=device, compute_type=compute_type
                )
                # ctranslate2 raises for a truly unusable device at load;
                # reaching here means this (device, compute_type) works.
                if (device, compute_type) != attempts[0]:
                    print(
                        f"[flow.stt] faster-whisper: {attempts[0][0]} недостъпно "
                        f"({last_error!r}) — минавам на {device}/{compute_type}",
                        flush=True,
                    )
                self._model = model
                self._loaded_as = (device, compute_type)
                return model
            except Exception as e:  # noqa: BLE001 - ctranslate2 raises RuntimeError/ValueError
                last_error = e
                if (device, compute_type) == attempts[-1]:
                    raise
        raise RuntimeError(f"faster-whisper failed to load: {last_error!r}")

    # ---- SttEngine protocol --------------------------------------------------

    def load_audio(self, path: str) -> np.ndarray:
        """Decode an audio file to 16 kHz mono float32 (PyAV, no ffmpeg binary)."""
        return np.asarray(decode_audio(path, sampling_rate=16000))

    def detect_probs(self, audio: np.ndarray) -> dict[str, float]:
        """Whisper language ID on the first 30 s; returns {lang_code: prob}."""
        model = self._get_model()
        language, probability, all_probs = model.detect_language(
            audio=audio.astype(np.float32)
        )
        probs = {lang: float(p) for lang, p in (all_probs or [])}
        probs.setdefault(language, float(probability))  # top pick, always present
        return probs

    def transcribe(
        self, audio: np.ndarray, language: str, initial_prompt: str | None
    ) -> str:
        """One forced-language decode of a 16 kHz mono float32 buffer.

        Mirrors the mlx engine call shape: greedy decode (temperature 0,
        beam_size 1 — faster-whisper defaults to beam 5, which is slower on
        CPU and diverges from the mac decode), no conditioning on previous
        text, and the initial_prompt kwarg is only passed when a dictionary
        prompt actually exists.
        """
        decode_kwargs: dict[str, object] = {}
        if initial_prompt is not None:
            decode_kwargs["initial_prompt"] = initial_prompt
        segments, _info = self._get_model().transcribe(
            audio.astype(np.float32),
            language=language,
            temperature=0.0,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
            vad_filter=False,  # the energy gate upstream is the only gate
            **decode_kwargs,
        )
        # segments is a LAZY generator — consuming it is what runs the decode.
        return "".join(segment.text for segment in segments).strip()

    def warm_up(self, sample_rate: int) -> None:
        """Load the model and run one tiny inference (0.5 s of silence)."""
        dummy = np.zeros(int(0.5 * sample_rate), dtype=np.float32)
        segments, _info = self._get_model().transcribe(
            dummy, language="en", temperature=0.0, beam_size=1, best_of=1
        )
        for _segment in segments:  # exhaust the lazy generator = real decode
            pass
