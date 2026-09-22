"""Phase 3 rhotacism compensation tests — headless: no microphone, no hotkey.

A speaker with rhotacism cannot pronounce "р"/"r" clearly, so Whisper
mis-hears р-heavy words. config.speaker_rhotacism (an opt-in accessibility
feature, off by default — these tests enable it explicitly) adds
RHOTACISM_BLOCK to the cleanup prompt (flow/cleanup.py); these tests cover:

1. Gating: OFF -> prompt byte-identical to the tuned prompt (mirrors the
   empty-dictionary guarantee); ON -> block purely additive, in the slot
   after the tuned examples; dictionary + rhotacism coexist.
2. Text-level corrections (cleanup stage only): distorted р->л/в words are
   restored in BG, w/l->r in EN; a corrected question keeps its "?".
3. ADVERSARIAL: legitimate л/в/w/l words must pass through untouched
   ("молив" never becomes "морив", "walk" never becomes "rock"/"work").
4. The 8 llm_cases fixtures re-run against the ON prompt (regression:
   corrections, question guards, no translation still hold with the block).
5. Interaction: dictionary + rhotacism together, text-level e2e.
6. Audio-level: full pipeline on TTS clips synthesized from DISTORTED
   spellings with `say -v Daria` (the TTS pronounces the distortion, so the
   impediment reaches Whisper). Assertions cover only what proved stable
   across repeated runs; known non-recoverable case, documented in
   docs/phase3-rhotacism.md: word-final "утле" does not survive Daria+Whisper
   (comes back as "опле"), so the invoice clip only asserts фактулата ->
   фактурата. "плати" (a real word) is deliberately NOT expected to become
   "прати" — under-correction by design.

All correction/adversarial fixtures deliberately differ from the examples
inside RHOTACISM_BLOCK so they measure generalization, not memorization.

Run:  .venv/bin/python -m pytest tests/test_rhotacism.py -v
Needs: Ollama running on localhost:11434 with todorov/bggpt pulled; the
audio tests additionally need macOS `say` with the Daria (bg_BG) voice and
the mlx-whisper model cache (they skip if Daria is unavailable).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from flow import cleanup
from flow.__main__ import run_file
from flow.config import FlowConfig
from flow.dictionary import EMPTY, Dictionary

PROJECT = Path(__file__).resolve().parent.parent
LLM_CASES = {
    case["id"]: case
    for case in json.loads(
        (PROJECT / "tests" / "fixtures" / "llm_cases.json").read_text(encoding="utf-8")
    )
}


def _clean_on(raw: str, dictionary=EMPTY) -> str:
    """Clean with rhotacism compensation ON (opt-in: enabled explicitly)."""
    config = FlowConfig(speaker_rhotacism=True)
    result = cleanup.clean_transcript(raw, config, dictionary=dictionary)
    assert not result.used_fallback, f"unexpected fallback: {result.fallback_reason}"
    return result.text


# --------------------------------------------------------------------------
# 1. Gating: OFF -> byte-identical prompt; ON -> purely additive block
# --------------------------------------------------------------------------


def test_off_prompt_byte_identical_to_tuned() -> None:
    """Mirrors the empty-dictionary guarantee: with the flag off the prompt
    must be byte-identical to the tuned template."""
    msg_default = cleanup.build_user_message("hello world")
    msg_explicit_off = cleanup.build_user_message("hello world", EMPTY, rhotacism=False)
    legacy_template_fill = cleanup.CLEANUP_PROMPT_TEMPLATE.format(
        transcript="hello world", dictionary_block=""
    )
    assert msg_default == msg_explicit_off == legacy_template_fill


def test_on_block_is_purely_additive() -> None:
    off = cleanup.build_user_message("hello world", EMPTY, rhotacism=False)
    on = cleanup.build_user_message("hello world", EMPTY, rhotacism=True)
    assert cleanup.RHOTACISM_BLOCK in on
    # removing the inserted block (with its framing newlines) must reproduce
    # the OFF prompt exactly — nothing else may change
    assert on.replace("\n" + cleanup.RHOTACISM_BLOCK + "\n", "", 1) == off


def test_on_block_sits_after_tuned_examples_before_final_instruction() -> None:
    on = cleanup.build_user_message("hello world", EMPTY, rhotacism=True)
    last_tuned_example = on.index("Резервирай маса за девет вечерта.")
    block = on.index("Speaker note — mild rhotacism")
    final_instruction = on.index("Now clean this transcript")
    assert last_tuned_example < block < final_instruction


def test_config_flag_defaults_off() -> None:
    # The public default: rhotacism compensation is opt-in.
    assert FlowConfig().speaker_rhotacism is False


def test_clean_transcript_honors_config_flag(monkeypatch) -> None:
    """config.speaker_rhotacism must reach build_user_message unchanged."""
    seen: list[bool] = []
    real = cleanup.build_user_message

    def spy(transcript, dictionary=None, rhotacism=False):
        seen.append(rhotacism)
        return real(transcript, dictionary, rhotacism)

    monkeypatch.setattr(cleanup, "build_user_message", spy)
    cleanup.clean_transcript("hello world", FlowConfig(speaker_rhotacism=False), dictionary=EMPTY)
    cleanup.clean_transcript("hello world", FlowConfig(), dictionary=EMPTY)  # default: off
    cleanup.clean_transcript("hello world", FlowConfig(speaker_rhotacism=True), dictionary=EMPTY)
    assert seen == [False, False, True]


# --------------------------------------------------------------------------
# 2. Text-level corrections (cleanup stage only, block ON)
# --------------------------------------------------------------------------


def test_bg_labota_restored_to_rabota() -> None:
    out = _clean_on("утре съм на лабота до късно").lower()
    assert "работа" in out, f"'лабота' not restored: {out}"
    assert "лабота" not in out, f"distorted word survived: {out}"


def test_bg_two_restorations_in_one_sentence() -> None:
    out = _clean_on("затволи плозолеца в кухнята").lower()
    assert "затвори" in out, f"'затволи' not restored: {out}"
    assert "прозоре" in out, f"'плозолеца' not restored: {out}"
    assert "плозо" not in out, f"distorted word survived: {out}"


def test_bg_real_but_wrong_word_restored() -> None:
    # "плясна" is a real verb form, but contextually wrong as an adjective;
    # restoring р gives "прясна", which fits.
    out = _clean_on("купих плясна риба от пазара").lower()
    assert "прясна" in out, f"'плясна' not restored: {out}"
    assert "риба" in out and "пазара" in out, f"content lost: {out}"


def test_bg_corrected_question_keeps_question_mark() -> None:
    out = _clean_on("кога ще донесеш доклада утле").lower()
    assert "утре" in out, f"'утле' not restored: {out}"
    assert "утле" not in out, f"distorted word survived: {out}"
    assert "?" in out, f"question mark lost during correction: {out}"


def test_en_w_restored_to_r() -> None:
    out = _clean_on("can you wepeat the last sentence pwease").lower()
    assert "repeat" in out and "please" in out, f"w->r not restored: {out}"
    assert "wepeat" not in out and "pwease" not in out, f"distortion survived: {out}"


def test_en_multiple_w_restorations() -> None:
    out = _clean_on("the pwintew is bwoken again").lower()
    assert "printer" in out and "broken" in out, f"w->r not restored: {out}"
    assert "pwintew" not in out and "bwoken" not in out, f"distortion survived: {out}"


# --------------------------------------------------------------------------
# 3. ADVERSARIAL: legitimate л/в/w/l words must NOT be "corrected"
# --------------------------------------------------------------------------


def test_bg_moliv_untouched() -> None:
    out = _clean_on("подай ми молива и виж навън").lower()
    assert "молива" in out, f"legitimate 'молива' was altered: {out}"
    assert "виж" in out, f"legitimate 'виж' was altered: {out}"
    assert "морив" not in out and "риж" not in out, f"over-correction: {out}"


def test_bg_kelner_vino_untouched() -> None:
    out = _clean_on("келнерът донесе виното").lower()
    assert "келнер" in out and "виното" in out, f"legitimate words altered: {out}"
    assert "кернер" not in out and "риното" not in out, f"over-correction: {out}"


def test_bg_lampa_untouched() -> None:
    out = _clean_on("запали лампата в хола").lower()
    assert "лампата" in out and "хола" in out, f"legitimate words altered: {out}"
    assert "рампата" not in out, f"over-correction: {out}"


def test_en_walk_untouched() -> None:
    out = _clean_on("we will walk to the lake").lower()
    assert "walk" in out and "lake" in out, f"legitimate words altered: {out}"
    assert "work" not in out and "rock" not in out and "rake" not in out, (
        f"over-correction: {out}"
    )


# --------------------------------------------------------------------------
# 4. The 8 llm_cases fixtures re-run against the ON prompt (regressions)
# --------------------------------------------------------------------------

# case id -> (substrings that must be present, regexes that must not match)
_LLM_ON_EXPECTATIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "en-filler-heavy": (("thursday", "afternoon"), (r"\bum+\b", r"\buh\b", r"\bthe the\b")),
    "en-self-correction": (("wednesday", "maria"), (r"\btuesday\b",)),
    "en-question-not-answered": (("?", "meeting"), (r"\bat\s+\d", r"\bam\b", r"\bpm\b")),
    "en-instruction-looking-dictation": (("deploy", "friday"), (r"^dear\b", r"\bregards\b")),
    "bg-filler-heavy": (("четвъртък", "следобед"), (r"ъъ",)),
    "bg-self-correction": (("сряда", "иван", "мария"), (r"\bвторник\b", r"\bчакай\b")),
    "bg-question-not-answered": (("?", "франция"), (r"париж",)),
    "bg-mixed-numbers-names": (("осем", "иван петров", "юли"), (r"\bшест\b", r"\b6\b")),
}


@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param(
            cid,
            marks=pytest.mark.xfail(
                strict=False,
                reason="Ollama prompt-cache-regime sensitivity on the bare-'не' "
                "numeric boundary; see docs/phase3-dictionary.md "
                "§Ollama state-dependence",
            ),
        )
        if cid == "bg-mixed-numbers-names"
        else cid
        for cid in sorted(_LLM_ON_EXPECTATIONS)
    ],
)
def test_llm_case_still_passes_with_block_on(case_id: str) -> None:
    out = _clean_on(LLM_CASES[case_id]["raw"]).lower()
    must_have, must_not_match = _LLM_ON_EXPECTATIONS[case_id]
    for word in must_have:
        assert word in out, f"{case_id}: {word!r} missing with rhotacism block on: {out}"
    for rx in must_not_match:
        assert not re.search(rx, out), f"{case_id}: {rx!r} matched with rhotacism block on: {out}"


# --------------------------------------------------------------------------
# 5. Interaction: dictionary + rhotacism together
# --------------------------------------------------------------------------


def test_both_blocks_present_and_ordered() -> None:
    d = Dictionary(terms=("Елена Тестова", "Wispr Flow"))
    msg = cleanup.build_user_message("hello world", d, rhotacism=True)
    rhot = msg.index("Speaker note — mild rhotacism")
    known = msg.index("Known terms")
    final = msg.index("Now clean this transcript")
    assert rhot < known < final, "blocks missing or out of order"
    # both fully intact
    assert cleanup.RHOTACISM_BLOCK in msg
    assert "Елена Тестова" in msg and "Wispr Flow" in msg


def test_dictionary_and_rhotacism_compose_e2e_text() -> None:
    """A distorted name is restored to its dictionary spelling while the
    rhotacism block fixes 'утле' in the same sentence (the name carries the
    same per-word single р→л distortions the original fixture had)."""
    d = Dictionary(terms=("Борис Примеров", "Wispr Flow"))
    out = _clean_on("изплати доклада на болис примелов утле следобед", dictionary=d)
    assert "Борис Примеров" in out, f"dictionary spelling not applied: {out}"
    assert "утре" in out.lower(), f"'утле' not restored with both blocks on: {out}"
    assert "болис" not in out.lower(), f"distorted name survived: {out}"


# --------------------------------------------------------------------------
# 6. Audio-level: full pipeline on TTS-synthesized DISTORTED clips
# --------------------------------------------------------------------------

# clip name -> the distorted text `say -v Daria` pronounces
_DISTORTED_CLIP_TEXTS = {
    "door": "отволи влатата и ми подай молива",
    "invoice": "плати ми фактулата утле",
    "window": "затволи плозолеца в кухнята",
}


def _daria_available() -> bool:
    if shutil.which("say") is None:
        return False
    try:
        proc = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=15
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return any(line.startswith("Daria") for line in proc.stdout.splitlines())


@pytest.fixture(scope="module")
def distorted_clips(tmp_path_factory) -> dict[str, Path]:
    if not _daria_available():
        pytest.skip("macOS `say` with the Daria (bg_BG) voice is unavailable")
    out_dir = tmp_path_factory.mktemp("rhotacism_tts")
    paths: dict[str, Path] = {}
    for name, text in _DISTORTED_CLIP_TEXTS.items():
        path = out_dir / f"rhot_bg_{name}.aiff"
        subprocess.run(["say", "-v", "Daria", "-o", str(path), text], check=True, timeout=30)
        paths[name] = path
    return paths


@pytest.mark.parametrize(
    "name,must_have,must_not",
    [
        # Distortion survives Daria+Whisper verbatim; both words restored,
        # legitimate "молива" kept.
        ("door", ("отвори", "вратата", "молива"), ("отволи", "влатата")),
        # Whisper renders "утле" as "опле" (not recoverable — documented);
        # "плати" is a real word and stays (under-correction by design).
        # Only фактулата -> фактурата is asserted.
        ("invoice", ("фактурата",), ("фактулата",)),
        # Whisper splits the distorted verb into "Затво ли" and keeps
        # "плозолеца"; the LLM still recovers both words.
        ("window", ("затвори", "прозоре"), ("плозо",)),
    ],
)
def test_audio_distorted_clip_corrected(distorted_clips, name, must_have, must_not) -> None:
    result = run_file(str(distorted_clips[name]), FlowConfig(speaker_rhotacism=True))
    # raw vs cleaned is reported for docs/phase3-rhotacism.md (visible with -s
    # or on failure)
    print(f"\n[rhotacism-audio] {name}: raw={result.raw_text!r} cleaned={result.cleaned_text!r}")
    assert result.status == "ok", f"{name}: pipeline skipped: {result.status}"
    assert result.language == "bg", f"{name}: decoded as {result.language}"
    low = result.cleaned_text.lower()
    for word in must_have:
        assert word in low, (
            f"{name}: {word!r} missing (raw={result.raw_text!r}, cleaned={result.cleaned_text!r})"
        )
    for word in must_not:
        assert word not in low, (
            f"{name}: distorted {word!r} survived (raw={result.raw_text!r}, "
            f"cleaned={result.cleaned_text!r})"
        )
