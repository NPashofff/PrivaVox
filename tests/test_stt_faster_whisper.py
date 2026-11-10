"""W2: faster-whisper engine — resolution matrix + a REAL CPU smoke test.

The smoke test runs the actual faster-whisper "tiny" model (~75 MB, cached in
the HF cache after the first run) on macOS CPU int8 — the point of W2 is that
the Windows STT path is genuinely exercised on the dev Mac. The default suite
must NOT download the turbo model (only "tiny" here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flow import stt
from flow.config import FlowConfig
from flow.dictionary import EMPTY

faster_whisper = pytest.importorskip(
    "faster_whisper", reason="faster-whisper not installed (W2 dev dependency)"
)

from flow.platform_win32.stt_faster_whisper import (  # noqa: E402
    _DEVICE_ATTEMPTS,
    FasterWhisperEngine,
)

AUDIO = Path(__file__).resolve().parent.parent / "test_audio"
TINY = "tiny"  # smallest real model; the ONLY one this suite may download


# --------------------------------------------------------------------------
# 1. Engine/model resolution matrix (platform × stt_engine × stt_model)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("is_mac,engine,expected_kind", [
    (True, "auto", "mlx"),
    (False, "auto", "faster-whisper"),
    (True, "mlx", "mlx"),
    (False, "mlx", "mlx"),                            # explicit wins anywhere
    (True, "faster-whisper", "faster-whisper"),
    (True, "faster-whisper-cpu", "faster-whisper-cpu"),
    (False, "faster-whisper-cpu", "faster-whisper-cpu"),
    (True, "faster-whisper-cuda", "faster-whisper-cuda"),
    (False, "faster-whisper-cuda", "faster-whisper-cuda"),
])
def test_engine_kind_matrix(monkeypatch, is_mac, engine, expected_kind):
    monkeypatch.setattr(stt, "IS_MAC", is_mac)
    assert stt.resolve_engine_kind(FlowConfig(stt_engine=engine)) == expected_kind


@pytest.mark.parametrize("is_mac,engine,model,expected", [
    # "auto" model resolves per engine
    (True, "auto", "auto", "mlx-community/whisper-large-v3-turbo"),
    (False, "auto", "auto", stt.FASTER_WHISPER_DEFAULT_MODEL),
    (True, "faster-whisper-cpu", "auto", stt.FASTER_WHISPER_DEFAULT_MODEL),
    (False, "mlx", "auto", "mlx-community/whisper-large-v3-turbo"),
    # explicit model passes through untouched (installer writes "small")
    (False, "faster-whisper-cpu", "small", "small"),
    (True, "auto", "some/custom-repo", "some/custom-repo"),
])
def test_model_resolution_matrix(monkeypatch, is_mac, engine, model, expected):
    monkeypatch.setattr(stt, "IS_MAC", is_mac)
    config = FlowConfig(stt_engine=engine, stt_model=model)
    assert stt.resolve_model(config) == expected


def test_unknown_engine_kind_raises():
    with pytest.raises(ValueError, match="unknown stt_engine"):
        stt._make_engine("whisper-cpp", "tiny")


def test_unknown_device_raises():
    with pytest.raises(ValueError, match="unknown faster-whisper device"):
        FasterWhisperEngine(TINY, device="tpu")


def test_device_attempts_shape():
    # cpu never tries cuda; cuda/auto degrade to cpu int8 (installer contract:
    # missing cuDNN must degrade, not crash)
    assert _DEVICE_ATTEMPTS["cpu"] == (("cpu", "int8"),)
    assert _DEVICE_ATTEMPTS["cuda"][0] == ("cuda", "float16")
    assert _DEVICE_ATTEMPTS["cuda"][-1] == ("cpu", "int8")
    assert _DEVICE_ATTEMPTS["auto"] == _DEVICE_ATTEMPTS["cuda"]


# --------------------------------------------------------------------------
# 2. Real CPU smoke test (tiny model) — the genuine Windows-path exercise
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tiny_engine() -> FasterWhisperEngine:
    return FasterWhisperEngine(TINY, device="cpu")


def test_cpu_engine_transcribes_real_audio(tiny_engine):
    audio = tiny_engine.load_audio(str(AUDIO / "en_01_date_number.wav"))
    assert audio.dtype.name == "float32" and audio.ndim == 1 and audio.size > 16000
    text = tiny_engine.transcribe(audio, "en", None)
    assert text, "empty transcript from the tiny model"
    assert "march" in text.lower(), f"unexpected transcript: {text!r}"
    assert tiny_engine._loaded_as == ("cpu", "int8")


def test_cpu_engine_detect_probs(tiny_engine):
    audio = tiny_engine.load_audio(str(AUDIO / "en_01_date_number.wav"))
    probs = tiny_engine.detect_probs(audio)
    assert isinstance(probs, dict) and probs
    assert all(isinstance(k, str) and isinstance(v, float) for k, v in probs.items())
    assert max(probs, key=probs.get) == "en"


def test_cpu_engine_warm_up(tiny_engine):
    tiny_engine.warm_up(16000)  # must not raise; loads model + one decode


def test_cuda_config_falls_back_to_cpu_here():
    # No CUDA on the dev Mac (nor on CUDA-less Windows boxes): the "cuda"
    # engine must degrade LOUDLY to cpu/int8 instead of crashing — the
    # Install-PrivaVox.ps1 contract for missing cuDNN/cuBLAS DLLs.
    engine = FasterWhisperEngine(TINY, device="cuda")
    engine.warm_up(16000)
    assert engine._loaded_as == ("cpu", "int8")


def test_initial_prompt_is_accepted(tiny_engine):
    audio = tiny_engine.load_audio(str(AUDIO / "en_01_date_number.wav"))
    text = tiny_engine.transcribe(audio, "en", "PrivaVox, Primerov")
    assert text  # biasing must not break the decode


# --------------------------------------------------------------------------
# 3. Through the flow.stt facade with an explicit engine on macOS
# --------------------------------------------------------------------------

def test_facade_honors_explicit_faster_whisper_cpu_on_mac():
    config = FlowConfig(stt_engine="faster-whisper-cpu", stt_model=TINY)
    result = stt.transcribe(
        str(AUDIO / "en_01_date_number.wav"),
        language="en", config=config, dictionary=EMPTY,
    )
    assert result["language"] == "en"
    assert result["text"], "facade produced an empty transcript"
    assert ("faster-whisper-cpu", TINY) in stt._engines


def test_facade_auto_language_detect_via_faster_whisper():
    # language=None + language_mode="auto" exercises detect_probs through the
    # facade (resolve_language must query the SAME engine/model, not mlx).
    config = FlowConfig(stt_engine="faster-whisper-cpu", stt_model=TINY,
                        language_mode="auto")
    result = stt.transcribe(
        str(AUDIO / "en_01_date_number.wav"),
        language=None, config=config, dictionary=EMPTY,
    )
    assert result["detected_language"] == "en"
    assert result["language"] == "en"
    assert result["detect_time_s"] > 0.0
