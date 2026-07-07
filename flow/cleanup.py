"""Transcript cleanup via a local BgGPT model served by Ollama.

Uses only the standard library (urllib) against Ollama's OpenAI-compatible
/v1/chat/completions endpoint, temperature 0, with the whole prompt in a
single USER turn (BgGPT/Gemma chat templates are unreliable with system roles).

The prompt carries few-shot examples specifically to fix the Phase 0 bug:
BgGPT-Gemma-3-4B resolved English self-corrections ("Tuesday no wait
Wednesday" -> Wednesday) but kept BOTH values in Bulgarian. See
docs/phase0-llm.md and docs/phase1-mvp.md.

Phase 3 tuned the prompt further for the Phase 1 quality observation: long,
already-punctuated rambling dictations kept mild discourse fillers
(bg_03_rambling.wav came back verbatim, starting "Ами, значи…"). The
instruction now names the discourse fillers explicitly (including sentence
openers) with keep-when-meaningful carve-outs (значи = "means",
нали-as-question, такова = "such", like-as-verb), and a sixth few-shot
example shows a long punctuated BG rambling being cleaned. See
docs/phase3-prompt-tuning.md and tests/test_prompt_tuning.py.

Phase 3 also added the personal dictionary (flow/dictionary.py). When the
dictionary is non-empty, a short "known terms" block is appended AFTER all
few-shot examples and BEFORE the final "Now clean this transcript"
instruction — this preserves the tuned instruction/example ordering exactly
(see docs/phase3-prompt-tuning.md for what is fragile there: the long BG
example must not be last; the "нали" carve-outs; the "X не Y" boundary
example). When the dictionary is empty, the prompt is byte-identical to the
pre-Phase-3 template — see build_user_message().

Phase 3 finally added rhotacism compensation (config.speaker_rhotacism, an
optional accessibility feature, off by default): a speaker with rhotacism
cannot pronounce "р"/"r" clearly, so Whisper mis-hears р-heavy words
("отвори" -> "отволи", "report" -> "wepowt"). When the feature is enabled,
RHOTACISM_BLOCK teaches the model to restore "р"/"r" from context — and,
just as important, to leave legitimate л/в/w/l words alone. The block goes through the SAME insertion slot as the dictionary
block (before it, i.e. right after the tuned examples), carrying three of
its own example pairs; the last pair doubles as a re-anchor for the fragile
bare-"не" numeric correction (see the RHOTACISM_BLOCK comment). The tuned
template text is untouched; with the flag off the prompt is byte-identical
to the tuned prompt. See docs/phase3-rhotacism.md.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import DEFAULT, FlowConfig
from .dictionary import Dictionary, build_cleanup_block, load_configured_dictionary

# --- The cleanup prompt ------------------------------------------------------
#
# Few-shot examples (2 EN + 4 BG) cover: filler removal, duplicated words,
# self-correction resolution (day-of-week and numeric, in both languages),
# question preservation, and (Phase 3) a long, already-punctuated BG rambling
# where discourse fillers are stripped but meaningful lookalikes ("което
# значи", "нали ще успееш?") survive. The examples deliberately use different
# names / days / numbers than the test fixtures so the fixtures still measure
# generalization, not memorization.
#
# Tuning notes (Phase 3, all at temperature 0 — see docs/phase3-prompt-tuning.md):
# - The long BG example must NOT be the last one: with it last, a long English
#   input came back TRANSLATED into Bulgarian. A short BG correction example
#   closes the block instead.
# - "нали" must not be listed as a strippable sentence-START filler, or a
#   leading "нали ще дойдеш…" question gets deleted wholesale; only the
#   parenthetical ", нали," between commas is safe to name.
# - The bare "X не Y" numeric correction ("шест не осем" -> осем) is
#   boundary-sensitive: it is held in place by the example "купи ъъ ами три
#   не пет ябълки" (filler directly before the number pair, mirroring real
#   dictation); stating the rule in prose instead made it WORSE.

CLEANUP_PROMPT_TEMPLATE = """\
You are a dictation cleanup tool. Rewrite the raw transcript: remove filler \
words and false starts, apply the speaker's self-corrections, fix punctuation \
and capitalization, format naturally. When the speaker corrects themselves \
("X no wait Y", "X actually Y", "X не чакай Y", "X всъщност Y", "X не Y"), \
keep ONLY the final corrected value Y — the rejected value X and the \
correction words must NOT appear in the output. Remove discourse fillers \
everywhere — even in long transcripts that already have punctuation, and even \
at the start of a sentence (English: um, uh, like, you know, I mean, well, a \
leading "So,"; Bulgarian: ъъъ, ъъ, ами, значи, такова, един вид, and a \
parenthetical ", нали," between commas). Only remove such a word when it is a \
meaningless filler: keep "значи" when it means "means" ("това значи много"), \
keep "такова" when it means "such" ("такова нещо"), keep "like" when used as \
a verb ("I would like"), and keep "нали" when it asks a real question — \
"Нали ще се видим довечера?" keeps its "нали". The output \
language must be the language of the raw transcript — never translate \
(English stays English, Bulgarian stays Bulgarian). Preserve meaning exactly. \
Never answer questions or add content. Output ONLY the cleaned text.

Examples:

Raw: um can you send me the the budget file by monday no wait actually by friday
Cleaned: Can you send me the budget file by Friday?

Raw: uh what's the address for the for the office party
Cleaned: What's the address for the office party?

Raw: ъъъ обади се ами на георги утре не чакай всъщност в петък след обяд
Cleaned: Обади се на Георги в петък след обяд.

Raw: купи ъъ ами три не пет ябълки от магазина и ги дай на елена
Cleaned: Купи пет ябълки от магазина и ги дай на Елена.

Raw: Ами, значи, ъъъ мислех да те питам дали може, нали, да преместим тренировката, защото колата ми, такова, е на сервиз до сряда, което значи, че един вид нямам как да идвам, така че хайде да я направим в събота от десет не от единайсет, нали ще успееш?
Cleaned: Мислех да те питам дали може да преместим тренировката, защото колата ми е на сервиз до сряда, което значи, че нямам как да идвам, така че хайде да я направим в събота от единайсет, нали ще успееш?

Raw: значи ами резервирай маса за в в осем не чакай всъщност за девет вечерта
Cleaned: Резервирай маса за девет вечерта.
{dictionary_block}
Now clean this transcript. Output ONLY the cleaned text, nothing else.

Raw: {transcript}
Cleaned:"""

# --- Rhotacism compensation block (Phase 3, config.speaker_rhotacism) --------
#
# Inserted through the same slot as the dictionary block (rhotacism first,
# then dictionary), i.e. after the tuned few-shot examples and before the
# final "Now clean this transcript" instruction. The tuned template text and
# example order stay byte-for-byte untouched; the block is purely additive
# and gated.
#
# The block's shape is the survivor of 8 iterations (full log in
# docs/phase3-rhotacism.md); everything below is load-bearing:
# - Placement: prepending the block's pairs BEFORE the tuned examples broke
#   the "не чакай всъщност" correction and the "So," opener stripping
#   (primacy dilution); inline-prose examples in this slot broke the "нали"
#   question guard and the rambling openers. Only this slot + real
#   Raw/Cleaned pairs kept all tuned behaviors.
# - The LAST pair re-anchors the fragile bare-"не" numeric correction
#   ("шест не осем" -> осем, held by the tuned ябълки example) which any
#   pair appended after the tuned six otherwise regresses (recency
#   dilution). It packs, in one line: fillers before the number pair (the
#   tuned anchor's surface shape), a bare-"не" correction composed with a
#   rhotacism restore ("два не тли" -> три), a legitimate л-word kept
#   (лева), and a QUESTION kept a question — with a statement pair last
#   instead, corrected questions lost their "?" and suspicious words got
#   dropped rather than restored.
# - The BG pair fixes two words AND keeps "молива" — demonstrating the
#   keep-legitimate-words rule inside the same sentence beat stating it in
#   prose (a prose "never delete words / keep questions" clause regressed
#   the "So," opener instead of helping).
# - All three pairs deliberately differ from the test fixtures
#   (tests/test_rhotacism.py) so the tests measure generalization.

RHOTACISM_BLOCK = """\
Speaker note — mild rhotacism: this speaker cannot pronounce "р"/"r" \
clearly, so an intended "р"/"r" may reach the transcript as "л" or "в" \
(Bulgarian), as "w" or "l" (English), or be dropped. If a transcribed word \
is not a real word or clearly does not fit the sentence, and restoring \
"р"/"r" in it gives a common word that fits, write the restored word. Never \
change a word that is already correct and fits the sentence — legitimate \
л/в/w/l words stay exactly as written ("молив" stays "молив", "walk" stays \
"walk").

Raw: отволи влатата и ми подай молива
Cleaned: Отвори вратата и ми подай молива.

Raw: could you wead the wepowt befowe lunch
Cleaned: Could you read the report before lunch?

Raw: ъъ имаш ли ами два не тли лева да ми дадеш
Cleaned: Имаш ли три лева да ми дадеш?"""

# --- Dictionary re-anchor pair (Phase 3 interaction fix) ----------------------
#
# A non-empty dictionary block is the LAST optional part of the prompt, and —
# like any text appended after the tuned examples — it dilutes their recency
# anchoring (side-finding of the rhotacism work; reproduced by
# tests/test_dictionary_interaction.py: the leading-"нали" question guard
# regressed in both dict-only and dict+rhotacism runs, "So," stripping in the
# combined run). This single Raw/Cleaned pair rides with the dictionary block
# (appended right after it, so it is again the final pair) and re-teaches, in
# one sentence: a leading-"нали" question kept as a question, fillers
# stripped, and the bare-"не" numeric correction boundary. It is deliberately
# NON-rhotic (must not teach р-restores when speaker_rhotacism is off) and
# differs from the tuned examples, the rhotacism pairs, and the test fixtures.
# The rhotacism-only prompt is byte-identical to its validated form — the
# anchor is added only when the dictionary block is present.

DICTIONARY_ANCHOR = """\
Raw: ъъъ нали ще минеш ами през офиса да вземеш два не четири стола
Cleaned: Нали ще минеш през офиса да вземеш четири стола?"""


@dataclass
class CleanupResult:
    text: str                       # what should be inserted
    raw_model_output: str | None    # untouched model output (None on transport error)
    used_fallback: bool             # True -> `text` is the raw transcript
    fallback_reason: str | None
    latency_s: float


class CleanupWarmupError(RuntimeError):
    """warm_up() could not run the model (server down, model missing, bad
    response); str() carries the _call_ollama error string."""


def build_user_message(
    transcript: str, dictionary: Dictionary | None = None, rhotacism: bool = False
) -> str:
    """Build the cleanup prompt. dictionary=None (default) loads
    config.dictionary_path implicitly via the caller; pass flow.dictionary.EMPTY
    explicitly for no dictionary block. An empty dictionary produces a prompt
    byte-identical to the pre-Phase-3 template (the block placeholder resolves
    to "", reconstructing the original blank line before "Now clean...").

    rhotacism: mirrors config.speaker_rhotacism (callers pass it through).
    True inserts RHOTACISM_BLOCK ahead of the dictionary block (same slot);
    the default False keeps the bare tuned prompt, byte-identical to
    pre-rhotacism."""
    parts: list[str] = []
    if rhotacism:
        parts.append(RHOTACISM_BLOCK)
    dict_block = build_cleanup_block(dictionary) if dictionary is not None else None
    if dict_block:
        parts.append(dict_block)
        # Re-anchor the tuned behaviors: the dictionary block is the last
        # optional part, so it must be followed by a final example pair
        # (see DICTIONARY_ANCHOR comment; tests/test_dictionary_interaction.py).
        parts.append(DICTIONARY_ANCHOR)
    dictionary_block = "".join("\n" + part + "\n" for part in parts)
    return CLEANUP_PROMPT_TEMPLATE.format(transcript=transcript, dictionary_block=dictionary_block)


def _call_ollama(
    transcript: str, config: FlowConfig, dictionary: Dictionary | None = None
) -> tuple[str | None, str | None, float]:
    """POST to Ollama. Returns (content, error, latency_s)."""
    payload = {
        "model": config.ollama_model,
        "messages": [
            {
                "role": "user",
                "content": build_user_message(
                    transcript, dictionary, rhotacism=config.speaker_rhotacism
                ),
            }
        ],
        "temperature": 0,
        "stream": False,
        "keep_alive": config.ollama_keep_alive,
    }
    req = urllib.request.Request(
        config.ollama_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=config.ollama_timeout_s) as resp:
            body = resp.read().decode("utf-8")
        parsed = json.loads(body)
        content = parsed["choices"][0]["message"]["content"]
        return str(content), None, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception as body_err:
            detail = f"unreadable error body: {body_err!r}"
        return None, f"HTTP {e.code}: {detail}", time.monotonic() - t0
    except urllib.error.URLError as e:
        return None, f"URLError: {e.reason}", time.monotonic() - t0
    except (http.client.HTTPException, OSError) as e:
        # urlopen only wraps request-phase errors in URLError; response-phase
        # failures leak unwrapped (socket READ timeout as bare TimeoutError,
        # Ollama restarted mid-generation as ConnectionResetError /
        # RemoteDisconnected / IncompleteRead) — same fallback for all.
        if isinstance(e, TimeoutError):
            return None, f"Timeout after {config.ollama_timeout_s}s: {e}", time.monotonic() - t0
        return None, f"Connection failed: {e!r}", time.monotonic() - t0
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return None, f"Bad response shape: {e}", time.monotonic() - t0


# --- Output guards -----------------------------------------------------------

_REFUSAL_META_PREFIXES: tuple[str, ...] = (
    # English refusal / meta / chat-wrapper openings
    "i can't", "i cannot", "i'm sorry", "i am sorry", "i apologize",
    "as an ai", "sure,", "sure!", "certainly", "of course", "here is", "here's",
    "the cleaned text", "cleaned text:", "note:",
    # Bulgarian equivalents
    "съжалявам", "не мога да", "като изкуствен интелект", "като ai",
    "разбира се", "ето", "прочистеният текст", "изчистеният текст",
)


def _strip_wrappers(text: str) -> str:
    """Remove harmless formatting the model sometimes adds around the answer."""
    text = text.strip()
    if text.lower().startswith("cleaned:"):
        text = text[len("cleaned:"):].strip()
    # whole-output ```fences```
    fence = re.match(r"^```[a-zA-Z]*\n(.*?)\n?```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # symmetric quote wrapping
    if len(text) >= 2 and text[0] in "\"'„“«" and text[-1] in "\"'”“»":
        text = text[1:-1].strip()
    return text


def looks_like_refusal_or_meta(cleaned: str, raw_transcript: str) -> str | None:
    """Return a reason string if the model output should not be trusted, else None."""
    if not cleaned:
        return "empty response"
    lowered = cleaned.lower()
    for prefix in _REFUSAL_META_PREFIXES:
        if lowered.startswith(prefix):
            return f"refusal/meta prefix: {prefix!r}"
    if "\n" in cleaned and len(cleaned.splitlines()) > 3:
        return "multi-paragraph/list output for a dictation snippet"
    if len(cleaned) > max(120, 3 * len(raw_transcript)):
        return "response much longer than transcript (invented content?)"
    return None


def clean_transcript(
    raw_transcript: str, config: FlowConfig = DEFAULT, dictionary: Dictionary | None = None
) -> CleanupResult:
    """Clean a raw transcript; on any doubt, fall back to the raw transcript.

    dictionary: personal dictionary (flow.dictionary.Dictionary) whose terms
        are injected into the prompt as a "known terms" block. None (default)
        loads config.dictionary_path; pass flow.dictionary.EMPTY to force no
        block. An empty dictionary never changes the prompt.

    Rhotacism compensation is controlled by config.speaker_rhotacism (an
    opt-in accessibility feature, off by default; see RHOTACISM_BLOCK above
    and docs/phase3-rhotacism.md).
    """
    raw_transcript = raw_transcript.strip()
    if not raw_transcript:
        return CleanupResult("", None, True, "empty transcript", 0.0)

    if dictionary is None:
        dictionary = load_configured_dictionary(config)

    content, error, latency = _call_ollama(raw_transcript, config, dictionary)
    if content is None:
        return CleanupResult(raw_transcript, None, True, error, latency)

    cleaned = _strip_wrappers(content)
    reason = looks_like_refusal_or_meta(cleaned, raw_transcript)
    if reason is not None:
        return CleanupResult(raw_transcript, content, True, reason, latency)
    return CleanupResult(cleaned, content, False, None, latency)


def warm_up(config: FlowConfig = DEFAULT) -> float:
    """One tiny request so the model is resident before the first dictation.

    Returns the warm-up latency on success; raises CleanupWarmupError when the
    request fails — otherwise a dead Ollama looks "ready" and every dictation
    silently falls back to the raw transcript."""
    t0 = time.monotonic()
    content, error, _ = _call_ollama("hello", config)
    if content is None:
        raise CleanupWarmupError(error or "unknown error")
    return time.monotonic() - t0
