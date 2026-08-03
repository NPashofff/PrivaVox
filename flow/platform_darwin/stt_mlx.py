"""mlx-whisper STT engine (Apple Silicon, macOS).

Extracted verbatim from flow/stt.py in phase W1 of the Windows port
(docs/windows-port-plan.md). The Whisper model is cached at module level by
mlx_whisper's own ModelHolder, so the first transcribe() call loads the
weights and every later call is warm.

All language resolution, dictionary prompt building and hallucination
filtering stay in flow/stt.py — this module is raw model access only.
"""

from __future__ import annotations

from typing import Any, Callable

import mlx.core as mx
import numpy as np
from mlx_whisper import audio as whisper_audio
from mlx_whisper.decoding import detect_language as _detect_language
from mlx_whisper.transcribe import ModelHolder
from mlx_whisper.transcribe import transcribe as _whisper_transcribe


def load_audio(path: str) -> np.ndarray:
    """Decode any audio file to 16 kHz mono float32 via ffmpeg."""
    return np.asarray(whisper_audio.load_audio(path))


def _get_model(model_repo: str):
    return ModelHolder.get_model(model_repo, mx.float16)


class MlxWhisperEngine:
    """SttEngine implementation (protocol in flow/stt.py) backed by mlx-whisper."""

    name = "mlx"

    def __init__(
        self,
        model_repo: str,
        decode_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        # decode_fn is a test seam: flow.stt passes a late-bound wrapper so
        # that monkeypatching flow.stt._whisper_transcribe (the pre-W1 patch
        # point, used by tests/test_dictionary.py) keeps intercepting decodes.
        self.model_repo = model_repo
        self._decode = decode_fn if decode_fn is not None else _whisper_transcribe

    def load_audio(self, path: str) -> np.ndarray:
        return load_audio(path)

    def detect_probs(self, audio: np.ndarray) -> dict[str, float]:
        """Run Whisper language ID on the first 30 s; returns {lang_code: prob}."""
        model = _get_model(self.model_repo)
        mel = whisper_audio.log_mel_spectrogram(
            audio, n_mels=model.dims.n_mels, padding=whisper_audio.N_SAMPLES
        )
        mel_segment = whisper_audio.pad_or_trim(
            mel, whisper_audio.N_FRAMES, axis=-2
        ).astype(mx.float16)
        _, probs = _detect_language(model, mel_segment)
        return probs

    def transcribe(
        self, audio: np.ndarray, language: str, initial_prompt: str | None
    ) -> str:
        """One forced-language decode of a 16 kHz mono float32 buffer.

        With no dictionary prompt the initial_prompt kwarg is never passed at
        all (None is not good enough) — the call shape must stay byte-identical
        to pre-Phase-3 behavior.
        """
        decode_kwargs: dict[str, Any] = {}
        if initial_prompt is not None:
            decode_kwargs["initial_prompt"] = initial_prompt
        result = self._decode(
            audio,
            path_or_hf_repo=self.model_repo,
            language=language,
            temperature=0.0,
            condition_on_previous_text=False,
            **decode_kwargs,
        )
        return str(result["text"]).strip()

    def warm_up(self, sample_rate: int) -> None:
        """Load the model and run one tiny inference (0.5 s of silence)."""
        dummy = np.zeros(int(0.5 * sample_rate), dtype=np.float32)
        self._decode(
            dummy, path_or_hf_repo=self.model_repo, language="en", temperature=0.0
        )
