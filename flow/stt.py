"""Speech-to-text: language pinning, dictionary biasing and a hallucination filter.

Engine-independent logic lives here. The actual Whisper implementation sits
behind the SttEngine protocol (below), picked by platform + config:

  - macOS:   mlx-whisper           — flow/platform_darwin/stt_mlx.py
  - Windows: faster-whisper (W2)   — flow/platform_win32/stt_faster_whisper.py

FlowConfig.stt_engine selects the engine ("auto" → per-platform default).
Engine instances are cached per (engine, model_repo); the heavy model caching
itself stays inside the engines (on macOS mlx_whisper's own ModelHolder), so
the first transcribe() call loads the weights and every later call is warm.
"""

from __future__ import annotations

import re
import time
from typing import Any, Protocol, TypedDict

import numpy as np

from .config import DEFAULT, FlowConfig
from .dictionary import Dictionary, build_stt_prompt, load_configured_dictionary
from .platform_impl import IS_MAC

if IS_MAC:
    # Eager re-export (test seam): tests monkeypatch
    # flow.stt._whisper_transcribe, and _mlx_decode below resolves this
    # module attribute at call time, so the patch keeps intercepting decodes.
    from .platform_darwin.stt_mlx import _whisper_transcribe  # noqa: F401


class SttResult(TypedDict):
    text: str
    language: str          # language actually used for decoding ("en"/"bg"/...)
    detected_language: str  # Whisper's top pick before en/bg forcing
    detect_time_s: float
    decode_time_s: float


class SttEngine(Protocol):
    """Platform STT backend: raw model access only, no language/filter logic.

    Implementations: MlxWhisperEngine (flow/platform_darwin/stt_mlx.py);
    FasterWhisperEngine on Windows arrives in phase W2.
    """

    name: str

    def load_audio(self, path: str) -> np.ndarray:
        """Decode an audio file to 16 kHz mono float32."""
        ...

    def transcribe(
        self, audio: np.ndarray, language: str, initial_prompt: str | None
    ) -> str:
        """One decode with a forced language; None prompt means no biasing."""
        ...

    def detect_probs(self, audio: np.ndarray) -> dict[str, float]:
        """Whisper language ID on the first 30 s; returns {lang_code: prob}."""
        ...

    def warm_up(self, sample_rate: int) -> None:
        """Load the model and run one tiny inference."""
        ...


def _mlx_decode(*args: Any, **kwargs: Any) -> Any:
    """Late-bound mlx decode: resolves _whisper_transcribe on THIS module at
    call time — tests replace it via monkeypatch (the pre-W1 patch point)."""
    return _whisper_transcribe(*args, **kwargs)


# Default model for the faster-whisper engine when config.stt_model == "auto"
# (the same repo Install-PrivaVox.ps1 pre-fetches on Windows).
FASTER_WHISPER_DEFAULT_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"

# stt_engine values → (engine implementation, forced device or None=auto).
_FASTER_WHISPER_DEVICES = {
    "faster-whisper": "auto",       # CUDA if usable, else CPU (see the engine)
    "faster-whisper-cuda": "cuda",  # what the installer writes on NVIDIA boxes
    "faster-whisper-cpu": "cpu",    # …and on everything else
}


def resolve_engine_kind(config: FlowConfig = DEFAULT) -> str:
    """Map config.stt_engine to a concrete engine kind.

    "auto" → per-platform default; explicit values ("mlx", "faster-whisper",
    "faster-whisper-cuda", "faster-whisper-cpu") are honored on ANY platform,
    so a Mac can run the faster-whisper CPU path (that is the W2 test rig).
    """
    if config.stt_engine != "auto":
        return config.stt_engine
    return "mlx" if IS_MAC else "faster-whisper"


def resolve_model(config: FlowConfig = DEFAULT) -> str:
    """Concrete STT model for the resolved engine.

    config.stt_model == "auto" picks the engine's default (mlx → the existing
    config.stt_model_repo; faster-whisper → FASTER_WHISPER_DEFAULT_MODEL);
    any other value (HF repo or faster-whisper size name like "small") is
    passed through as-is.
    """
    if config.stt_model != "auto":
        return config.stt_model
    if resolve_engine_kind(config) == "mlx":
        return config.stt_model_repo
    return FASTER_WHISPER_DEFAULT_MODEL


_engines: dict[tuple[str, str], SttEngine] = {}


def _get_engine(config: FlowConfig, model_repo: str | None = None) -> SttEngine:
    kind = resolve_engine_kind(config)
    repo = model_repo if model_repo is not None else resolve_model(config)
    key = (kind, repo)
    engine = _engines.get(key)
    if engine is None:
        engine = _make_engine(kind, repo)
        _engines[key] = engine
    return engine


def _make_engine(kind: str, model_repo: str) -> SttEngine:
    if kind == "mlx":
        from .platform_darwin.stt_mlx import MlxWhisperEngine

        return MlxWhisperEngine(model_repo, decode_fn=_mlx_decode)
    if kind in _FASTER_WHISPER_DEVICES:
        # Lives under platform_win32/ (its production home) but is genuinely
        # cross-platform: the CPU path runs — and is tested — on macOS too.
        from .platform_win32.stt_faster_whisper import FasterWhisperEngine

        return FasterWhisperEngine(model_repo, device=_FASTER_WHISPER_DEVICES[kind])
    raise ValueError(
        f"unknown stt_engine {kind!r} (expected 'auto', 'mlx', 'faster-whisper', "
        f"'faster-whisper-cuda' or 'faster-whisper-cpu')"
    )


def load_audio(path: str) -> np.ndarray:
    """Decode any audio file to 16 kHz mono float32 via ffmpeg."""
    return _get_engine(DEFAULT).load_audio(path)


def detect_language_probs(
    audio: np.ndarray, model_repo: str, config: FlowConfig = DEFAULT
) -> dict[str, float]:
    """Run Whisper language ID on the first 30 s; returns {lang_code: prob}.

    `config` only picks the engine (stt_engine); the model comes from
    `model_repo`, kept as an explicit parameter for API compatibility.
    """
    return _get_engine(config, model_repo=model_repo).detect_probs(audio)


def resolve_language(
    audio: np.ndarray, language_mode: str, config: FlowConfig = DEFAULT
) -> tuple[str, str, float]:
    """Pick the decode language per config.

    Returns (language_to_use, whisper_top_detection, detect_time_s).
    In "auto" mode: use Whisper's detection, but if the top pick is neither
    of the supported languages, force whichever supported one is more probable.
    """
    if language_mode in config.supported_languages:
        return language_mode, language_mode, 0.0
    t0 = time.monotonic()
    probs = detect_language_probs(audio, resolve_model(config), config)
    detect_time = time.monotonic() - t0
    top = max(probs, key=probs.get)  # type: ignore[arg-type]
    if top in config.supported_languages:
        return top, top, detect_time
    forced = max(config.supported_languages, key=lambda lang: probs.get(lang, 0.0))
    return forced, top, detect_time


def transcribe(
    audio: np.ndarray | str,
    language: str | None = None,
    config: FlowConfig = DEFAULT,
    dictionary: Dictionary | None = None,
) -> SttResult:
    """Transcribe a numpy buffer (16 kHz mono float32) or an audio file path.

    language: "en"/"bg" to force, None to use config.language_mode (default auto).
    dictionary: personal dictionary (flow.dictionary.Dictionary) whose terms
        bias decoding via Whisper's `initial_prompt`. None (default) loads
        config.dictionary_path; pass flow.dictionary.EMPTY to force no
        biasing. An empty dictionary never passes `initial_prompt` at all —
        byte-identical to pre-Phase-3 behavior.

    Language detection (resolve_language, above) always runs on the raw mel
    spectrogram BEFORE this function's single decode call and never sees
    `initial_prompt` — so dictionary biasing cannot influence auto-detect.
    The prompt is only attached to the decode call itself, which always
    receives an already-resolved, forced `language=lang`.
    """
    engine = _get_engine(config)
    if isinstance(audio, str):
        audio = engine.load_audio(audio)
    mode = language if language is not None else config.language_mode
    lang, detected, detect_time = resolve_language(audio, mode, config)

    if dictionary is None:
        dictionary = load_configured_dictionary(config)
    initial_prompt = build_stt_prompt(dictionary)

    t0 = time.monotonic()
    text = engine.transcribe(audio, lang, initial_prompt)
    decode_time = time.monotonic() - t0
    return SttResult(
        text=text,
        language=lang,
        detected_language=detected,
        detect_time_s=detect_time,
        decode_time_s=decode_time,
    )


def warm_up(config: FlowConfig = DEFAULT) -> float:
    """Load the model and run one tiny inference so the first real call is warm.

    Returns the wall time spent (seconds).
    """
    t0 = time.monotonic()
    _get_engine(config).warm_up(config.sample_rate)
    return time.monotonic() - t0


# --- Hallucination / empty-output filter -----------------------------------
#
# whisper-large-v3-turbo deterministically hallucinates sign-off phrases on
# silence / near-silence (verified in Phase 0: "Thank you." on silence_2s.wav).
# These are byproducts of caption training data, and must never be inserted.

_KNOWN_HALLUCINATIONS: frozenset[str] = frozenset(
    {
        # English caption artifacts
        "thank you", "thank you thank you", "thanks", "thank you for watching",
        "thanks for watching", "you", "bye", "bye bye", "the end",
        "subtitles by the amara org community",
        # Bulgarian caption artifacts
        "благодаря", "благодаря ви", "благодаря за гледането",
        "благодаря ви за гледането", "абонирайте се", "до нови срещи", "мерси",
    }
)
_HALLUCINATION_PREFIXES: tuple[str, ...] = (
    "субтитри",       # "Субтитри от ..." / "Субтитри: ..."
    "subtitles by",
    "превод и субтитри",
)
# Distinctive subtitle-credit markers that appear ANYWHERE in the output (the
# hallucinated names vary and often come first, e.g. "А.Семкин Корректор…"), so
# a prefix check misses them. These are Russian caption/credit words that do
# not occur in genuine Bulgarian/English dictation — "корректор"/"субтитров"
# are Russian spellings (Bulgarian is "коректор", "субтитрите").
_HALLUCINATION_SUBSTRINGS: tuple[str, ...] = (
    "редактор субтитров",
    "субтитров",
    "корректор",
    "субтитры",
    "amara",
)


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)  # drop punctuation
    return re.sub(r"\s+", " ", text).strip()


def is_hallucination(text: str) -> bool:
    """True for empty output, bare punctuation, or known silence hallucinations."""
    normalized = _normalize(text)
    if not normalized:  # empty or punctuation-only
        return True
    if normalized in _KNOWN_HALLUCINATIONS:
        return True
    if any(normalized.startswith(p) for p in _HALLUCINATION_PREFIXES):
        return True
    return any(s in normalized for s in _HALLUCINATION_SUBSTRINGS)
