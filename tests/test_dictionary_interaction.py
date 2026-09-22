"""Dictionary × tuned-prompt interaction regression tests (Phase 3 fix).

Discovered during the rhotacism work (docs/phase3-rhotacism.md, side-finding):
a NON-EMPTY dictionary block ALONE — appended after the tuned few-shot
examples with no anchor — dilutes their recency anchoring and regresses two
tuned behaviors: the leading-"нали" question guard and the leading-"So,"
opener stripping. The fix (flow/cleanup.py DICTIONARY_ANCHOR) appends one
re-anchoring Raw/Cleaned pair whenever the dictionary block is present,
mirroring the technique RHOTACISM_BLOCK's last pair uses. Empty-dictionary
prompts remain byte-identical (asserted in test_dictionary.py); the
rhotacism-only prompt is untouched by the fix (the anchor rides only with
the dictionary block).

The нали fixture and the anchor pair's own sentence deliberately differ, so
these tests measure generalization, not memorization.

Run:  .venv/bin/python -m pytest tests/test_dictionary_interaction.py -v
Needs: Ollama live on localhost:11434 with todorov/bggpt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from flow import cleanup
from flow.config import FlowConfig
from flow.dictionary import Dictionary

PROJECT = Path(__file__).resolve().parent.parent

# Small mixed dictionary; realistic terms, unrelated to the fixtures below.
DICT = Dictionary(terms=("Борис Примеров", "BgGPT", "Melexis", "Уиспър Флоу"))

_TUNING = {
    c["id"]: c
    for c in json.loads(
        (PROJECT / "tests" / "fixtures" / "prompt_tuning_cases.json").read_text("utf-8")
    )
}
_LLM_CASES = json.loads(
    (PROJECT / "tests" / "fixtures" / "llm_cases.json").read_text("utf-8")
)

# Key behavioral markers per llm_cases fixture (subset with crisp signals).
_MARKERS = {
    "en-self-correction": lambda o: "wednesday" in o and "tuesday" not in o,
    "bg-self-correction": lambda o: "сряда" in o and "вторник" not in o,
    "bg-mixed-numbers-names": lambda o: "осем" in o and "шест" not in o,
    "en-question-not-answered": lambda o: "?" in o,
    "bg-question-not-answered": lambda o: "?" in o and "париж" not in o,
}


def _clean(raw: str, rhotacism: bool) -> str:
    cfg = FlowConfig(speaker_rhotacism=rhotacism)
    res = cleanup.clean_transcript(raw, cfg, dictionary=DICT)
    assert not res.used_fallback, f"unexpected fallback: {res.fallback_reason}"
    return res.text


@pytest.mark.parametrize("rhotacism", [False, True], ids=["dict-only", "dict+rhot"])
@pytest.mark.parametrize(
    "raw,keywords",
    [
        # The canonical tuned fixture (bg-meaningful-nali). Its contract
        # (prompt_tuning_cases.json expectations) is: "нали" MUST survive and
        # content is preserved — NOT that a "?" is restored: the tuned
        # baseline itself emits a trailing "." here. The interaction test
        # therefore asserts the actual guarantee, not an aspirational one.
        ("нали ще дойдеш утре на срещата в десет", ("дойдеш", "утре", "срещата", "десет")),
        # A second phrasing; the no-dictionary baseline actually DROPS "нали"
        # on this one, so keeping it with the dictionary block present is
        # strictly better than baseline.
        ("нали ще дойдеш утре след работа", ("дойдеш", "утре", "работа")),
    ],
    ids=["canonical-fixture", "second-phrasing"],
)
def test_leading_nali_survives_dictionary(raw: str, keywords: tuple[str, ...], rhotacism: bool) -> None:
    out = _clean(raw, rhotacism)
    low = out.lower()
    assert "нали" in low, f"'нали' guard lost with dictionary block: {out}"
    for keyword in keywords:
        assert keyword in low, f"content keyword {keyword!r} lost: {out}"


@pytest.mark.parametrize("rhotacism", [False, True], ids=["dict-only", "dict+rhot"])
def test_leading_so_stripped_with_dictionary(rhotacism: bool) -> None:
    out = _clean(_TUNING["en-rambling-long"]["raw"], rhotacism).lower()
    assert not re.match(r"\s*so\b", out), f"'So,' opener survived: {out}"
    for keyword in ("onboarding", "documentation", "friday"):
        assert keyword in out, f"content keyword {keyword!r} lost: {out}"


_CACHE_REGIME_XFAIL = pytest.mark.xfail(
    strict=False,
    reason="Ollama prompt-cache-regime sensitivity on the bare-'не' numeric "
    "boundary; see docs/phase3-dictionary.md §Ollama state-dependence",
)


@_CACHE_REGIME_XFAIL
@pytest.mark.parametrize("rhotacism", [False, True], ids=["dict-only", "dict+rhot"])
def test_bare_ne_numeric_boundary_with_dictionary(rhotacism: bool) -> None:
    case = next(c for c in _LLM_CASES if c["id"] == "bg-mixed-numbers-names")
    out = _clean(case["raw"], rhotacism).lower()
    assert "осем" in out and "шест" not in out, f"bare-'не' boundary regressed: {out}"


@pytest.mark.parametrize("rhotacism", [False, True], ids=["dict-only", "dict+rhot"])
@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param(cid, marks=_CACHE_REGIME_XFAIL)
        if cid == "bg-mixed-numbers-names"
        else cid
        for cid in sorted(_MARKERS)
    ],
)
def test_llm_cases_hold_with_dictionary(case_id: str, rhotacism: bool) -> None:
    case = next(c for c in _LLM_CASES if c["id"] == case_id)
    out = _clean(case["raw"], rhotacism).lower()
    assert _MARKERS[case_id](out), f"{case_id} regressed with dictionary block: {out}"
