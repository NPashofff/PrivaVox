#!/usr/bin/env python3
"""
Phase 0 STT benchmark for the Flow project.

Transcribes every clip in test_audio/ with ONE candidate mlx-whisper model per
invocation, recording wall time, real-time factor (RTF), and word error rate
(WER) vs. the reference transcripts in test_audio/references.json.

Designed to run one model per process so a crash (OOM, network failure during
a first-time HF download, etc.) on one model never loses results already
collected for another model. Results are merged into a single JSON file,
keyed by model name, so re-running a model simply overwrites that model's
entry without disturbing the others.

Usage (run with the project venv):
    .venv/bin/python scripts/bench_stt.py --model mlx-community/whisper-large-v3-turbo
    .venv/bin/python scripts/bench_stt.py --model mlx-community/whisper-small-mlx
    .venv/bin/python scripts/bench_stt.py --model mlx-community/whisper-large-v3-mlx

Optional:
    --output PATH   results JSON path (default: docs/phase0-stt-results.json)

Writes/merges raw JSON results to docs/phase0-stt-results.json for
docs/phase0-stt.md to be written from, and prints a human-readable summary to
stdout as it goes (flush=True throughout) so progress survives a crash.
"""

from __future__ import annotations

import argparse
import json
import string
import sys
import time
import unicodedata
import wave
from pathlib import Path

import mlx_whisper

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_AUDIO_DIR = PROJECT_ROOT / "test_audio"
REFERENCES_PATH = TEST_AUDIO_DIR / "references.json"
DEFAULT_RESULTS_PATH = PROJECT_ROOT / "docs" / "phase0-stt-results.json"

# Speech clips only (excludes the silence clip, which is handled separately).
SPEECH_CLIPS = [
    "en_01_date_number.wav",
    "en_02_name_email.wav",
    "en_03_rambling.wav",
    "bg_01_date_number.wav",
    "bg_02_name_address.wav",
    "bg_03_rambling.wav",
]
SILENCE_CLIP = "silence_2s.wav"

# For the "no language hint" auto-detect check: 1 EN + 1 BG clip.
AUTO_DETECT_CLIPS = ["en_01_date_number.wav", "bg_01_date_number.wav"]


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate)


def normalize_text(text: str) -> str:
    """Normalize for WER: Unicode NFC, lowercase, strip punctuation, collapse whitespace.

    Works for Cyrillic (Bulgarian) as well as Latin (English) since Python's
    str.lower() and unicodedata are script-aware, and we strip a broad set of
    punctuation including common Cyrillic-context marks.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.lower()
    # Strip standard ASCII punctuation plus common typographic/Cyrillic-context
    # punctuation not in string.punctuation (curly quotes, guillemets, dashes).
    extra_punct = "«»„“”‘’—–…"
    all_punct = string.punctuation + extra_punct
    text = "".join(ch for ch in text if ch not in all_punct)
    text = " ".join(text.split())  # collapse whitespace
    return text


def word_levenshtein(ref_words: list[str], hyp_words: list[str]) -> int:
    """Standard word-level Levenshtein (edit) distance via DP."""
    n, m = len(ref_words), len(hyp_words)
    # dp[i][j] = edit distance between ref_words[:i] and hyp_words[:j]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j],  # deletion
                    dp[i][j - 1],  # insertion
                    dp[i - 1][j - 1],  # substitution
                )
    return dp[n][m]


def compute_wer(reference: str, hypothesis: str) -> float:
    """Word Error Rate as a percentage: edit_distance / len(ref_words) * 100."""
    ref_norm = normalize_text(reference)
    hyp_norm = normalize_text(hypothesis)
    ref_words = ref_norm.split()
    hyp_words = hyp_norm.split()
    if not ref_words:
        return 0.0 if not hyp_words else 100.0
    distance = word_levenshtein(ref_words, hyp_words)
    return 100.0 * distance / len(ref_words)


def transcribe_one(
    audio_path: Path, model: str, language: str | None
) -> tuple[dict, float]:
    """Run mlx_whisper.transcribe, return (result_dict, wall_time_seconds)."""
    kwargs = {"path_or_hf_repo": model, "verbose": None}
    if language is not None:
        kwargs["language"] = language
    t0 = time.perf_counter()
    result = mlx_whisper.transcribe(str(audio_path), **kwargs)
    wall_time = time.perf_counter() - t0
    return result, wall_time


def load_existing_results(results_path: Path) -> dict:
    if results_path.exists():
        try:
            return json.loads(results_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(
                f"WARNING: could not parse existing {results_path} ({exc}); "
                "starting fresh for this file.",
                file=sys.stderr,
                flush=True,
            )
    return {"models": {}, "auto_detect": {}, "silence": {}, "durations_seconds": {}}


def save_results(results_path: Path, all_results: dict) -> None:
    """Write results atomically (temp file + rename) so a crash mid-write
    never corrupts previously-saved results from other models."""
    results_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = results_path.with_suffix(results_path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp_path.replace(results_path)


def run_model(model: str, results_path: Path) -> None:
    references = json.loads(REFERENCES_PATH.read_text(encoding="utf-8"))

    durations = {
        name: wav_duration_seconds(TEST_AUDIO_DIR / name)
        for name in SPEECH_CLIPS + [SILENCE_CLIP]
    }

    # Load (don't overwrite) any prior results from other models.
    all_results = load_existing_results(results_path)
    all_results.setdefault("models", {})
    all_results.setdefault("auto_detect", {})
    all_results.setdefault("silence", {})
    all_results.setdefault("durations_seconds", {})
    all_results["durations_seconds"].update(durations)

    print(f"\n{'=' * 70}", flush=True)
    print(f"MODEL: {model}", flush=True)
    print(f"{'=' * 70}", flush=True)

    model_result: dict = {"pass_1_cold": {}, "pass_2_warm": {}}

    # --- Pass 1: first clip is a true cold start (model download+load
    # happens inside this very first mlx_whisper.transcribe call, since
    # mlx_whisper caches the loaded model in a module-level dict after
    # that). Clips 2-6 in pass 1 are already warm (same as pass 2) but are
    # kept in "pass_1_cold" for continuity/ordering; the authoritative
    # cold-start figure is cold_start_first_clip_time_s, isolated below.
    print(
        "\n-- Pass 1 (clip 1 = cold start: download-if-needed + model load "
        "+ first inference; clips 2-6 already warm) --",
        flush=True,
    )
    pass1_clip_results = {}
    first_clip_time = None
    for i, clip_name in enumerate(SPEECH_CLIPS):
        ref = references[clip_name]
        lang = ref["lang"]
        audio_path = TEST_AUDIO_DIR / clip_name
        print(
            f"  [{i+1}/{len(SPEECH_CLIPS)}] {clip_name} (lang={lang}) ...",
            end=" ",
            flush=True,
        )
        result, wall_time = transcribe_one(audio_path, model, language=lang)
        if i == 0:
            first_clip_time = wall_time
        duration = durations[clip_name]
        rtf = duration / wall_time if wall_time > 0 else float("inf")
        wer = compute_wer(ref["text"], result["text"])
        pass1_clip_results[clip_name] = {
            "hypothesis": result["text"],
            "wall_time_s": wall_time,
            "rtf": rtf,
            "wer_pct": wer,
        }
        print(f"time={wall_time:.2f}s rtf={rtf:.2f}x wer={wer:.1f}%", flush=True)
    model_result["pass_1_cold"] = pass1_clip_results
    model_result["cold_start_first_clip_time_s"] = first_clip_time
    model_result["cold_start_note"] = (
        "cold_start_first_clip_time_s includes one-time HF snapshot_download "
        "(if not already cached) + mlx model load + first inference; clips "
        "2-6 of pass_1_cold are warm (model already resident)."
    )

    # --- Pass 2 (warm, model already loaded/cached by mlx_whisper's
    # internal model cache) ---
    print("\n-- Pass 2 (warm, full suite) --", flush=True)
    pass2_clip_results = {}
    for i, clip_name in enumerate(SPEECH_CLIPS):
        ref = references[clip_name]
        lang = ref["lang"]
        audio_path = TEST_AUDIO_DIR / clip_name
        print(
            f"  [{i+1}/{len(SPEECH_CLIPS)}] {clip_name} (lang={lang}) ...",
            end=" ",
            flush=True,
        )
        result, wall_time = transcribe_one(audio_path, model, language=lang)
        duration = durations[clip_name]
        rtf = duration / wall_time if wall_time > 0 else float("inf")
        wer = compute_wer(ref["text"], result["text"])
        pass2_clip_results[clip_name] = {
            "hypothesis": result["text"],
            "wall_time_s": wall_time,
            "rtf": rtf,
            "wer_pct": wer,
        }
        print(f"time={wall_time:.2f}s rtf={rtf:.2f}x wer={wer:.1f}%", flush=True)
    model_result["pass_2_warm"] = pass2_clip_results

    all_results["models"][model] = model_result
    save_results(results_path, all_results)  # checkpoint after core passes

    # --- Language auto-detection check (no language hint) ---
    print("\n-- Auto-detect check (no language hint) --", flush=True)
    auto_detect_results = {}
    for clip_name in AUTO_DETECT_CLIPS:
        ref = references[clip_name]
        audio_path = TEST_AUDIO_DIR / clip_name
        result, wall_time = transcribe_one(audio_path, model, language=None)
        detected = result.get("language")
        expected = ref["lang"]
        match = detected == expected
        auto_detect_results[clip_name] = {
            "expected_lang": expected,
            "detected_lang": detected,
            "match": match,
            "hypothesis": result["text"],
            "wall_time_s": wall_time,
        }
        print(
            f"  {clip_name}: expected={expected} detected={detected} "
            f"{'OK' if match else 'MISMATCH'}",
            flush=True,
        )
    all_results["auto_detect"][model] = auto_detect_results
    save_results(results_path, all_results)  # checkpoint

    # --- Silence / hallucination check ---
    print("\n-- Silence clip check (hallucination test) --", flush=True)
    silence_path = TEST_AUDIO_DIR / SILENCE_CLIP
    # No language hint: most representative of real-world VAD-gated failure
    # that slips through to the model.
    result, wall_time = transcribe_one(silence_path, model, language=None)
    hallucinated_text = result["text"].strip()
    has_hallucination = len(hallucinated_text) > 0
    silence_result = {
        "wall_time_s": wall_time,
        "hypothesis": hallucinated_text,
        "hallucinated": has_hallucination,
        "detected_language": result.get("language"),
    }
    all_results["silence"][model] = silence_result
    print(
        f"  hallucinated={has_hallucination} "
        f"text={hallucinated_text!r} time={wall_time:.2f}s",
        flush=True,
    )

    save_results(results_path, all_results)  # final checkpoint for this model
    print(f"\n\nResults for {model} merged into {results_path}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Phase 0 STT benchmark for ONE mlx-whisper model."
    )
    parser.add_argument(
        "--model",
        required=True,
        help=(
            "HF repo id of the mlx-whisper model to benchmark, e.g. "
            "mlx-community/whisper-large-v3-turbo"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RESULTS_PATH,
        help=f"Results JSON path (default: {DEFAULT_RESULTS_PATH})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        run_model(args.model, args.output)
    except Exception as exc:  # noqa: BLE001 - top-level crash guard
        print(
            f"\nFATAL: benchmark for model {args.model!r} failed: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        print(
            "Any results from earlier checkpoints in this run (if any) were "
            "already merged into the output file before the failure.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
