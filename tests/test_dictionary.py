"""Phase 3 personal dictionary tests — headless: no microphone, no hotkey.

Covers: parsing (comments/blanks/unicode), the empty-dictionary byte-identical
guarantee for both hooks (STT initial_prompt, cleanup prompt), non-empty
dictionary content + token cap, language auto-detect being unaffected by a
mixed EN/BG dictionary, the cleanup question-guard still holding, and one
full-pipeline e2e run with a dictionary loaded.

Run:  .venv/bin/python -m pytest tests/test_dictionary.py -v
Run the whole suite (required before calling this done):
      .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from flow import cleanup, dictionary as dictionary_mod, stt
from flow.__main__ import run_file
from flow.config import FlowConfig
from flow.dictionary import (
    EMPTY,
    Dictionary,
    build_cleanup_block,
    build_stt_prompt,
    load_dictionary,
    parse_dictionary_text,
)

PROJECT = Path(__file__).resolve().parent.parent
AUDIO_DIR = PROJECT / "test_audio"
REFERENCES = json.loads((AUDIO_DIR / "references.json").read_text(encoding="utf-8"))
LLM_CASES = {
    case["id"]: case
    for case in json.loads((PROJECT / "tests" / "fixtures" / "llm_cases.json").read_text(encoding="utf-8"))
}


def _lang_of_text(text: str) -> str:
    cyrillic = len(re.findall(r"[Ѐ-ӿ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "bg" if cyrillic > latin else "en"


# --------------------------------------------------------------------------
# 1. Parsing: comments, blanks, unicode
# --------------------------------------------------------------------------


def test_parse_ignores_comments_and_blanks() -> None:
    text = """
    # a leading comment

    Wispr Flow
        # indented comment (still a comment after strip)
    BgGPT

    # trailing comment
    """
    assert parse_dictionary_text(text) == ("Wispr Flow", "BgGPT")


def test_parse_unicode_mixed_en_bg() -> None:
    text = "Борис Примеров\nWispr Flow\nBgGPT\nЕлена Тестова\n"
    terms = parse_dictionary_text(text)
    assert terms == ("Борис Примеров", "Wispr Flow", "BgGPT", "Елена Тестова")


def test_parse_strips_whitespace_and_dedupes() -> None:
    text = "  Wispr Flow  \nWispr Flow\n\tBgGPT\t\n"
    assert parse_dictionary_text(text) == ("Wispr Flow", "BgGPT")


def test_parse_empty_text_is_empty_dictionary() -> None:
    assert parse_dictionary_text("") == ()
    assert parse_dictionary_text("# only comments\n\n\n") == ()


def test_load_missing_file_is_empty_no_error() -> None:
    d = load_dictionary("/tmp/this-file-should-not-exist-flow-dict-test.txt")
    assert d.is_empty
    assert d.terms == ()


def test_load_real_starter_dictionary_file_is_empty_by_default() -> None:
    # The starter dictionary.txt ships with all examples commented out.
    d = load_dictionary(PROJECT / "dictionary.txt")
    assert d.is_empty, f"expected starter dictionary.txt to be empty by default, got {d.terms!r}"


# --------------------------------------------------------------------------
# 2. Empty dictionary -> byte-identical behavior
# --------------------------------------------------------------------------


def test_empty_dictionary_no_stt_prompt() -> None:
    assert build_stt_prompt(EMPTY) is None


def test_empty_dictionary_no_cleanup_block() -> None:
    assert build_cleanup_block(EMPTY) is None


def test_empty_dictionary_cleanup_prompt_byte_identical() -> None:
    msg_no_dict_arg = cleanup.build_user_message("hello world")
    msg_explicit_empty = cleanup.build_user_message("hello world", EMPTY)
    legacy_template_fill = cleanup.CLEANUP_PROMPT_TEMPLATE.format(
        transcript="hello world", dictionary_block=""
    )
    assert msg_no_dict_arg == msg_explicit_empty == legacy_template_fill


def test_empty_dictionary_stt_transcribe_no_initial_prompt_kwarg(monkeypatch) -> None:
    """With an empty dictionary, flow.stt.transcribe must not pass
    initial_prompt to mlx_whisper's transcribe() at all (None is not good
    enough — the kwarg must be absent, matching pre-Phase-3 call shape)."""
    captured: dict = {}

    def fake_transcribe(audio, *, path_or_hf_repo, language, temperature, condition_on_previous_text, **kwargs):
        captured.update(kwargs)
        return {"text": "hi"}

    monkeypatch.setattr(stt, "_whisper_transcribe", fake_transcribe)
    audio = stt.load_audio(str(AUDIO_DIR / "en_01_date_number.wav"))
    stt.transcribe(audio, language="en", config=FlowConfig(), dictionary=EMPTY)
    assert "initial_prompt" not in captured, f"initial_prompt was passed with an empty dictionary: {captured}"


def test_nonempty_dictionary_stt_transcribe_passes_initial_prompt(monkeypatch) -> None:
    captured: dict = {}

    def fake_transcribe(audio, *, path_or_hf_repo, language, temperature, condition_on_previous_text, **kwargs):
        captured.update(kwargs)
        return {"text": "hi"}

    monkeypatch.setattr(stt, "_whisper_transcribe", fake_transcribe)
    audio = stt.load_audio(str(AUDIO_DIR / "en_01_date_number.wav"))
    d = Dictionary(terms=("Wispr Flow", "BgGPT"))
    stt.transcribe(audio, language="en", config=FlowConfig(), dictionary=d)
    assert captured.get("initial_prompt") is not None
    assert "Wispr Flow" in captured["initial_prompt"]
    assert "BgGPT" in captured["initial_prompt"]


# --------------------------------------------------------------------------
# 3. Non-empty dictionary: prompt contains terms, capped length respected
# --------------------------------------------------------------------------


def test_stt_prompt_contains_terms() -> None:
    d = Dictionary(terms=("Борис Примеров", "Wispr Flow", "BgGPT", "Елена Тестова"))
    prompt = build_stt_prompt(d)
    assert prompt is not None
    for term in d.terms:
        assert term in prompt


def test_cleanup_block_contains_terms_and_soft_instruction() -> None:
    d = Dictionary(terms=("Борис Примеров", "Wispr Flow"))
    block = build_cleanup_block(d)
    assert block is not None
    assert "Борис Примеров" in block
    assert "Wispr Flow" in block
    # must instruct "use when it clearly refers", not force unconditionally
    assert "do NOT force" in block or "not force" in block.lower()


def test_stt_prompt_respects_token_cap() -> None:
    from mlx_whisper.tokenizer import get_tokenizer

    # A dictionary way bigger than the ~120 token cap.
    many_terms = tuple(f"ТерминИмеБрандNumber{i}Тест" for i in range(200))
    d = Dictionary(terms=many_terms)
    prompt = build_stt_prompt(d, max_tokens=120)
    assert prompt is not None
    tok = get_tokenizer(multilingual=True, language="en", task="transcribe")
    n_tokens = len(tok.encode(" " + prompt))
    assert n_tokens <= 120, f"prompt exceeded cap: {n_tokens} tokens"
    # cap must actually have dropped some terms (proves the cap engaged)
    assert many_terms[-1] not in prompt


def test_stt_prompt_cap_keeps_terms_that_fit() -> None:
    d = Dictionary(terms=("Wispr Flow", "BgGPT"))
    prompt = build_stt_prompt(d, max_tokens=120)
    assert prompt is not None
    assert "Wispr Flow" in prompt and "BgGPT" in prompt


# --------------------------------------------------------------------------
# 4. Language auto-detect unaffected by a mixed dictionary (1 EN + 1 BG clip)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("clip", ["en_01_date_number.wav", "bg_01_date_number.wav"])
def test_auto_detect_language_unchanged_with_dictionary(clip: str) -> None:
    config = FlowConfig()  # language_mode == "auto"
    audio = stt.load_audio(str(AUDIO_DIR / clip))
    mixed_dict = Dictionary(
        terms=("Борис Примеров", "Wispr Flow", "BgGPT", "Елена Тестова")
    )

    no_dict = stt.transcribe(audio, language=None, config=config, dictionary=EMPTY)
    with_dict = stt.transcribe(audio, language=None, config=config, dictionary=mixed_dict)

    expected_lang = REFERENCES[clip]["lang"]
    assert no_dict["language"] == expected_lang
    assert with_dict["language"] == no_dict["language"], (
        f"{clip}: dictionary flipped decode language: "
        f"{no_dict['language']!r} -> {with_dict['language']!r}"
    )
    assert with_dict["detected_language"] == no_dict["detected_language"], (
        f"{clip}: dictionary flipped whisper's raw language detection: "
        f"{no_dict['detected_language']!r} -> {with_dict['detected_language']!r}"
    )


# --------------------------------------------------------------------------
# 5. Cleanup guard still intact with a dictionary present
# --------------------------------------------------------------------------


def test_question_guard_intact_with_dictionary() -> None:
    d = Dictionary(terms=("Wispr Flow", "BgGPT", "Борис Примеров"))
    raw = LLM_CASES["en-question-not-answered"]["raw"]
    result = cleanup.clean_transcript(raw, FlowConfig(), dictionary=d)
    assert not result.used_fallback, f"unexpected fallback: {result.fallback_reason}"
    assert "?" in result.text, f"question mark lost: {result.text}"
    assert not re.search(r"\b(at\s+\d|am\b|pm\b)", result.text.lower()), (
        f"invented answer with dictionary present: {result.text}"
    )


def test_question_guard_intact_with_dictionary_bg() -> None:
    d = Dictionary(terms=("Wispr Flow", "BgGPT", "Борис Примеров"))
    raw = LLM_CASES["bg-question-not-answered"]["raw"]
    result = cleanup.clean_transcript(raw, FlowConfig(), dictionary=d)
    assert not result.used_fallback, f"unexpected fallback: {result.fallback_reason}"
    assert "?" in result.text, f"question mark lost: {result.text}"
    assert "париж" not in result.text.lower(), f"model answered the question: {result.text}"


# --------------------------------------------------------------------------
# 6. Full pipeline e2e with a dictionary loaded (monkeypatched config path)
# --------------------------------------------------------------------------


@pytest.fixture()
def dictionary_file(tmp_path, monkeypatch):
    """Write a temp dictionary.txt and point a fresh FlowConfig at it."""
    path = tmp_path / "dictionary.txt"
    path.write_text(
        "# starter\nБорис Примеров\nWispr Flow\nBgGPT\nЕлена Тестова\n",
        encoding="utf-8",
    )
    return path


def test_e2e_pipeline_with_dictionary_loaded(dictionary_file) -> None:
    config = FlowConfig()
    config.dictionary_path = str(dictionary_file)

    clip = "bg_02_name_address.wav"
    expected_lang = REFERENCES[clip]["lang"]
    result = run_file(str(AUDIO_DIR / clip), config)

    assert result.status == "ok", f"pipeline skipped {clip}: {result.status}"
    assert result.cleaned_text.strip(), f"empty cleaned text for {clip}"
    assert result.language == expected_lang
    assert _lang_of_text(result.cleaned_text) == expected_lang


def test_dictionary_reload_reflects_file_changes(dictionary_file) -> None:
    """load_configured_dictionary re-reads the file each call (no caching
    that would require a process restart to see edits during tests)."""
    config = FlowConfig()
    config.dictionary_path = str(dictionary_file)

    d1 = dictionary_mod.load_configured_dictionary(config)
    assert "Wispr Flow" in d1.terms

    dictionary_file.write_text("Само едно име\n", encoding="utf-8")
    d2 = dictionary_mod.load_configured_dictionary(config)
    assert d2.terms == ("Само едно име",)
