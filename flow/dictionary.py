"""Personal dictionary (Phase 3): user-specific names/brands/jargon.

Loads a plain-text word list and turns it into two different hints:

  1. An STT `initial_prompt` for mlx-whisper (biases decoding toward the
     listed terms — see flow.stt.transcribe). Whisper's own docstring calls
     this out explicitly: "custom vocabularies or proper nouns to make it
     more likely to predict those words correctly."
  2. A block appended to the BgGPT cleanup prompt (flow.cleanup) that tells
     the model which exact spellings to prefer *when the transcript clearly
     refers to them* — not to force them in unconditionally.

Design constraints (see docs/phase3-dictionary.md for the full writeup):
  - Empty dictionary -> both hooks are byte-identical to pre-Phase-3
    behavior: no `initial_prompt` kwarg is passed to mlx_whisper at all, and
    the cleanup prompt is unchanged. This keeps the tuned-prompt tests in
    tests/test_prompt_tuning.py meaningful (see flow/cleanup.py docstring).
  - The STT prompt is capped to ~120 tokens using Whisper's own multilingual
    tokenizer (whisper's total prompt window is 224 tokens; capping leaves
    headroom for condition_on_previous_text-style continuation, even though
    we currently call with condition_on_previous_text=False).
  - Loading is tolerant: a missing file is an empty dictionary, not an
    error, so a fresh checkout / clean install works with zero setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import DEFAULT, FlowConfig

# Whisper's decoder prompt window is 224 tokens total (prev-text prompt +
# initial_prompt + special tokens + generated text share it). We cap our
# injected term list well under that so there is always room left for the
# actual transcription; see docs/phase3-dictionary.md "Token cap" section.
MAX_PROMPT_TOKENS = 120

STT_PROMPT_PREFIX = "Речник/Glossary:"
CLEANUP_BLOCK_HEADER = (
    "Known terms — if the transcript clearly refers to one of these, use "
    "this exact spelling; do NOT force any of them in otherwise:"
)


@dataclass(frozen=True)
class Dictionary:
    terms: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return len(self.terms) == 0


EMPTY = Dictionary(terms=())


def parse_dictionary_text(text: str) -> tuple[str, ...]:
    """Parse dictionary file contents: one term per line, '#' comments and
    blank lines ignored. Leading/trailing whitespace on each term is
    stripped. Order is preserved; exact duplicates are dropped."""
    terms: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped not in seen:
            seen.add(stripped)
            terms.append(stripped)
    return tuple(terms)


def load_dictionary(path: str | Path) -> Dictionary:
    """Load a dictionary file. Missing file -> empty dictionary, no error."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY
    return Dictionary(terms=parse_dictionary_text(text))


def load_configured_dictionary(config: FlowConfig = DEFAULT) -> Dictionary:
    return load_dictionary(config.dictionary_path)


@lru_cache(maxsize=1)
def _tokenizer():
    # Whisper's multilingual BPE tokenizer. Cheap to build (no model
    # weights loaded) but not free (~0.5s) — cached across calls.
    # mlx_whisper only exists on macOS; on Windows (faster-whisper engine,
    # phase W2) return None and let _count_tokens fall back to a conservative
    # estimate — mac behavior stays byte-identical (same tokenizer as ever).
    try:
        from mlx_whisper.tokenizer import get_tokenizer
    except ImportError:
        return None

    return get_tokenizer(multilingual=True, language="en", task="transcribe")


def _count_tokens(text: str) -> int:
    """Whisper-token count of `text` — exact where mlx_whisper is installed,
    else a conservative estimate: measured Whisper-BPE density on Bulgarian
    dictionary-style lines is 0.47–0.67 tokens/char (worst: щ/ъ clusters,
    foreign names), so 0.75 tokens/char overestimates in practice and only
    cuts the term list a bit earlier. Backstop for pathological inputs:
    faster-whisper itself trims initial_prompt to max_length//2-1 tokens, so
    an undercount can shorten the biasing but never break the decode."""
    tok = _tokenizer()
    if tok is not None:
        return len(tok.encode(text))
    return max(1, -(-3 * len(text) // 4))  # ceil(0.75 * len), no float dance


def _cap_to_tokens(terms: tuple[str, ...], prefix: str, max_tokens: int) -> tuple[str, ...]:
    """Keep terms (in order) while `prefix: term1, term2, ...` stays within
    max_tokens, using Whisper's own tokenizer so the cap is exact."""
    if not terms:
        return terms
    kept: list[str] = []
    for term in terms:
        candidate = prefix + " " + ", ".join([*kept, term])
        if _count_tokens(" " + candidate) > max_tokens:
            break
        kept.append(term)
    return tuple(kept)


def build_stt_prompt(dictionary: Dictionary, max_tokens: int = MAX_PROMPT_TOKENS) -> str | None:
    """Build the initial_prompt string for mlx_whisper. None when the
    dictionary is empty, so callers can skip passing the kwarg entirely
    (byte-identical behavior to pre-Phase-3)."""
    if dictionary.is_empty:
        return None
    capped = _cap_to_tokens(dictionary.terms, STT_PROMPT_PREFIX, max_tokens)
    if not capped:
        return None
    return STT_PROMPT_PREFIX + " " + ", ".join(capped)


def build_cleanup_block(dictionary: Dictionary) -> str | None:
    """Build the block appended to the cleanup prompt. None when the
    dictionary is empty (byte-identical cleanup prompt to pre-Phase-3)."""
    if dictionary.is_empty:
        return None
    return CLEANUP_BLOCK_HEADER + " " + ", ".join(dictionary.terms) + "."
