"""Phase 1 pipeline tests — headless: no microphone, no hotkey, no Accessibility.

Run:  .venv/bin/python -m pytest tests/test_pipeline.py -v
Needs: Ollama running on localhost:11434 with todorov/bggpt pulled,
       mlx-whisper model in the HF cache (both verified in Phase 0).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from flow import cleanup, insert, stt
from flow.__main__ import run_file
from flow.config import FlowConfig

PROJECT = Path(__file__).resolve().parent.parent
AUDIO_DIR = PROJECT / "test_audio"
REFERENCES = json.loads((AUDIO_DIR / "references.json").read_text(encoding="utf-8"))
LLM_CASES = {
    case["id"]: case
    for case in json.loads(
        (PROJECT / "tests" / "fixtures" / "llm_cases.json").read_text(encoding="utf-8")
    )
}

SPEECH_CLIPS = sorted(name for name in REFERENCES if REFERENCES[name]["lang"])
MAX_WARM_TOTAL_S = 5.0

# Collected per-clip timings, dumped at session end for docs/phase1-mvp.md.
TIMINGS: dict[str, dict] = {}


def _lang_of_text(text: str) -> str:
    """Crude script check: 'bg' if Cyrillic letters dominate, else 'en'."""
    cyrillic = len(re.findall(r"[Ѐ-ӿ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "bg" if cyrillic > latin else "en"


@pytest.fixture(scope="session", autouse=True)
def warm_models():
    """Warm STT + LLM once so per-clip numbers below are warm-path numbers."""
    config = FlowConfig()
    stt_warm_s = stt.warm_up(config)
    llm_warm_s = cleanup.warm_up(config)
    TIMINGS["_warmup"] = {"stt_warm_s": round(stt_warm_s, 3), "llm_warm_s": round(llm_warm_s, 3)}
    yield
    out = PROJECT / "docs" / "phase1-timings.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(TIMINGS, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[timings] written to {out}")
    for clip, t in TIMINGS.items():
        print(f"[timings] {clip}: {t}")


# --------------------------------------------------------------------------
# 1. End-to-end file-driven pipeline on every speech clip
# --------------------------------------------------------------------------

@pytest.mark.parametrize("clip", SPEECH_CLIPS)
def test_e2e_clip(clip: str) -> None:
    expected_lang = REFERENCES[clip]["lang"]
    result = run_file(str(AUDIO_DIR / clip), FlowConfig())

    TIMINGS[clip] = {
        "lang": result.language,
        "status": result.status,
        **result.timings,
        "raw": result.raw_text,
        "cleaned": result.cleaned_text,
        "used_fallback": result.used_fallback,
    }

    assert result.status == "ok", f"pipeline skipped {clip}: {result.status}"
    assert result.cleaned_text.strip(), f"empty cleaned text for {clip}"
    assert result.language == expected_lang, (
        f"{clip}: decode language {result.language} != expected {expected_lang}"
    )
    assert _lang_of_text(result.cleaned_text) == expected_lang, (
        f"{clip}: cleaned text script does not match {expected_lang}: {result.cleaned_text!r}"
    )
    total = result.timings["total_s"]
    assert total < MAX_WARM_TOTAL_S, f"{clip}: warm e2e took {total:.2f}s (limit {MAX_WARM_TOTAL_S}s)"


# --------------------------------------------------------------------------
# 2. Silence must never produce text (gate and/or hallucination filter)
# --------------------------------------------------------------------------

def test_silence_yields_nothing() -> None:
    result = run_file(str(AUDIO_DIR / "silence_2s.wav"), FlowConfig())
    assert result.status in ("silence", "too-short", "hallucination")
    assert not result.has_text


def test_hallucination_filter_known_phrases() -> None:
    for phrase in ("Thank you.", " Благодаря. ", "Субтитри от МЕДИЯ ТРАНС", ".", "", "  ?! "):
        assert stt.is_hallucination(phrase), f"filter missed {phrase!r}"
    for phrase in ("Thank you for the report, Maria.", "Благодаря ти за доклада."):
        assert not stt.is_hallucination(phrase), f"filter over-triggered on {phrase!r}"


# --------------------------------------------------------------------------
# 3. BG self-correction — the Phase 0 failures, now with few-shot prompt
# --------------------------------------------------------------------------

def _clean(case_id: str) -> str:
    result = cleanup.clean_transcript(LLM_CASES[case_id]["raw"], FlowConfig())
    assert not result.used_fallback, f"{case_id}: unexpected fallback: {result.fallback_reason}"
    return result.text


def test_en_self_correction_still_passes() -> None:
    out = _clean("en-self-correction").lower()
    assert "wednesday" in out and "tuesday" not in out, out


def test_bg_self_correction_day() -> None:
    out = _clean("bg-self-correction").lower()
    assert "сряда" in out, f"corrected day missing: {out}"
    assert "вторник" not in out, f"rejected day still present: {out}"
    assert "чакай" not in out, f"correction marker still present: {out}"
    assert "иван" in out and "мария" in out, f"names lost: {out}"


@pytest.mark.xfail(
    strict=False,
    reason="bare-'не' numeric boundary is Ollama prompt-cache-regime sensitive: "
    "fresh server → 'осем' (correct) 3/3, cached-prefix regime → 'шест' 3/3, "
    "byte-identical requests at temperature 0. XPASSes when healthy; treat an "
    "xfail here with an otherwise-green suite as the known canary. "
    "See docs/phase3-dictionary.md §Ollama state-dependence.",
)
def test_bg_self_correction_number() -> None:
    out = _clean("bg-mixed-numbers-names").lower()
    assert "осем" in out or re.search(r"\b8\b", out), f"corrected number missing: {out}"
    assert "шест" not in out and not re.search(r"\b6\b", out), f"rejected number still present: {out}"
    assert "иван петров" in out, f"name lost: {out}"
    assert "юли" in out, f"date lost: {out}"


# --------------------------------------------------------------------------
# 4. Cleanup guard: questions stay questions, and are never answered
# --------------------------------------------------------------------------

def test_question_not_answered_en() -> None:
    out = _clean("en-question-not-answered")
    assert "?" in out, f"question mark lost: {out}"
    assert "meeting" in out.lower(), out
    # must not invent an answer (a time like "3 pm" / "at 10")
    assert not re.search(r"\b(at\s+\d|am\b|pm\b)", out.lower()), f"invented answer: {out}"


def test_question_not_answered_bg() -> None:
    out = _clean("bg-question-not-answered")
    assert "?" in out, f"question mark lost: {out}"
    assert "париж" not in out.lower(), f"model answered the question: {out}"


# --------------------------------------------------------------------------
# 5. Clipboard round-trip (no Cmd-V is posted)
# --------------------------------------------------------------------------

def test_clipboard_save_set_restore() -> None:
    sentinel = "flow-clipboard-test-АБВ-123"
    original = insert.get_clipboard()
    try:
        insert.set_clipboard(sentinel)
        assert insert.get_clipboard() == sentinel
    finally:
        insert.set_clipboard(original)
    assert insert.get_clipboard() == original
