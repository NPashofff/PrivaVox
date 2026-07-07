"""Phase 3 prompt-tuning tests — mild discourse-filler removal on LONG dictations.

Targets the Phase 1 quality observation (docs/phase1-mvp.md): on long,
already-punctuated rambling Bulgarian dictations the cleanup kept mild
discourse fillers — bg_03_rambling.wav came back verbatim, starting
"Ами, значи…". Short BG cases cleaned fine.

Two test groups, both against fixtures in tests/fixtures/prompt_tuning_cases.json:

1. Filler removal on long Whisper-style punctuated ramblings (EN + BG):
   the filler tokens must be ABSENT and the content keywords PRESENT.
2. ADVERSARIAL over-stripping guards: sentences where the filler-lookalike
   words are meaningful ("това значи много за мен" — значи = means;
   "нали ще дойдеш утре" — нали carries the question; "такова нещо не съм
   казвал" — такова = such; "I would like to" — like = verb) and must SURVIVE.

Run:  .venv/bin/python -m pytest tests/test_prompt_tuning.py -v
Needs: Ollama running on localhost:11434 with todorov/bggpt pulled.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from flow import cleanup
from flow.config import FlowConfig

PROJECT = Path(__file__).resolve().parent.parent
PT_CASES = {
    case["id"]: case
    for case in json.loads(
        (PROJECT / "tests" / "fixtures" / "prompt_tuning_cases.json").read_text(encoding="utf-8")
    )
}


def _clean(case_id: str) -> str:
    result = cleanup.clean_transcript(PT_CASES[case_id]["raw"], FlowConfig())
    assert not result.used_fallback, f"{case_id}: unexpected fallback: {result.fallback_reason}"
    return result.text


# --------------------------------------------------------------------------
# 1. Long rambling dictations: mild discourse fillers must be stripped
# --------------------------------------------------------------------------

def test_bg_long_rambling_fillers_removed() -> None:
    out = _clean("bg-rambling-long").lower()
    # Filler tokens must be gone — including the sentence opener "ами, значи".
    # In this fixture every occurrence of these words is a pure filler.
    for filler in ("ъъъ", "нали", "такова", "един вид"):
        assert filler not in out, f"filler {filler!r} survived: {out}"
    assert not re.search(r"\bами\b", out), f"filler 'ами' survived: {out}"
    assert not re.search(r"\bзначи\b", out), f"filler 'значи' survived: {out}"
    # Content must be preserved.
    for keyword in ("колеги", "проект", "две седмици", "доставчик", "презентаци"):
        assert keyword in out, f"content keyword {keyword!r} lost: {out}"


def test_en_long_rambling_fillers_removed() -> None:
    out = _clean("en-rambling-long").lower()
    # In this fixture every occurrence of these tokens is a pure filler.
    for filler in ("you know", "i mean"):
        assert filler not in out, f"filler {filler!r} survived: {out}"
    assert not re.search(r"\bum\b", out), f"filler 'um' survived: {out}"
    assert not re.search(r"\blike\b", out), f"filler 'like' survived: {out}"
    # The mild discourse opener "So," must not survive at the start.
    assert not re.match(r"\s*so\b", out), f"'So,' opener survived: {out}"
    # Content must be preserved.
    for keyword in ("onboarding", "documentation", "outdated", "database", "friday"):
        assert keyword in out, f"content keyword {keyword!r} lost: {out}"


# --------------------------------------------------------------------------
# 2. ADVERSARIAL: filler-lookalike words that carry meaning must SURVIVE
# --------------------------------------------------------------------------

def test_bg_meaningful_znachi_survives() -> None:
    # "това значи много за мен" — значи is the verb "means", not a filler.
    out = _clean("bg-meaningful-znachi").lower()
    assert re.search(r"\bзначи\b", out), f"meaningful 'значи' was stripped: {out}"
    assert "много" in out, f"content lost: {out}"


def test_bg_meaningful_nali_survives() -> None:
    # "нали ще дойдеш утре" — нали carries the question ("…, right?").
    out = _clean("bg-meaningful-nali").lower()
    assert re.search(r"\bнали\b", out), f"meaningful 'нали' was stripped: {out}"
    assert "дойдеш" in out and "утре" in out, f"content lost: {out}"


def test_bg_meaningful_takova_survives() -> None:
    # "такова нещо не съм казвал" — такова is the demonstrative "such".
    out = _clean("bg-meaningful-takova").lower()
    assert re.search(r"\bтакова\b", out), f"meaningful 'такова' was stripped: {out}"
    assert "не съм казвал" in out, f"negation lost: {out}"


def test_en_meaningful_like_survives() -> None:
    # "I would like to schedule…" — like is the verb, not a filler.
    out = _clean("en-meaningful-like").lower()
    assert re.search(r"\blike\b", out), f"meaningful 'like' was stripped: {out}"
    assert "schedule" in out and "monday" in out, f"content lost: {out}"
