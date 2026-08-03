#!/usr/bin/env python3
"""
Benchmark the transcript-cleanup LLM stage against Ollama's OpenAI-compatible
chat-completions endpoint.

Standard library ONLY (urllib, json, time, argparse) -- must run on the
system Python 3.9 interpreter with no virtualenv and no third-party deps.

Usage:
    /usr/bin/python3 scripts/bench_llm.py --model todorov/bggpt
    /usr/bin/python3 scripts/bench_llm.py --model todorov/bggpt --passes 2 --out results.json
    /usr/bin/python3 scripts/bench_llm.py --model todorov/bggpt --list-only   # just print prompts

The entire cleanup instruction (system-role text + transcript) is placed in
a single USER message for every model. Rationale: BgGPT-Gemma-2-2.6B (Gemma-2
chat template) rejects a `system` role entirely, so to keep the benchmark
apples-to-apples across both candidate models we use the identical
user-turn-only pattern for both.
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

OLLAMA_CHAT_URL = "http://localhost:11434/v1/chat/completions"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"

CLEANUP_PROMPT_TEMPLATE = (
    "You are a dictation cleanup tool. Rewrite the raw transcript: remove "
    "filler words and false starts, apply the speaker's self-corrections, "
    "fix punctuation and capitalization, format naturally. Keep the "
    "language unchanged (English or Bulgarian). Preserve meaning exactly. "
    "Never answer questions or add content. Output ONLY the cleaned text.\n\n"
    "RAW TRANSCRIPT:\n{transcript}"
)


def build_user_message(transcript):
    return CLEANUP_PROMPT_TEMPLATE.format(transcript=transcript)


def load_cases(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def call_model(model, transcript, timeout=120):
    """
    POST to the Ollama OpenAI-compatible endpoint with temperature=0.
    Returns dict: {ok, latency_s, content, raw_response, error, usage}
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": build_user_message(transcript)}
        ],
        "temperature": 0,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_CHAT_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            # Ollama does not check the key, but the OpenAI-compatible
            # client convention (and openless's own client) expects one.
            "Authorization": "Bearer ollama",
        },
        method="POST",
    )

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        latency = time.monotonic() - t0
        err_body = e.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "latency_s": latency,
            "content": None,
            "raw_response": None,
            "error": "HTTP %s: %s" % (e.code, err_body),
            "usage": None,
        }
    except urllib.error.URLError as e:
        latency = time.monotonic() - t0
        return {
            "ok": False,
            "latency_s": latency,
            "content": None,
            "raw_response": None,
            "error": "URLError: %s" % (e.reason,),
            "usage": None,
        }
    latency = time.monotonic() - t0

    try:
        parsed = json.loads(body)
    except ValueError as e:
        return {
            "ok": False,
            "latency_s": latency,
            "content": None,
            "raw_response": body,
            "error": "JSON decode error: %s" % (e,),
            "usage": None,
        }

    try:
        content = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        return {
            "ok": False,
            "latency_s": latency,
            "content": None,
            "raw_response": parsed,
            "error": "Unexpected response shape: %s" % (e,),
            "usage": None,
        }

    usage = parsed.get("usage")
    return {
        "ok": True,
        "latency_s": latency,
        "content": content,
        "raw_response": parsed,
        "error": None,
        "usage": usage,
    }


def get_available_models():
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [m["name"] for m in body.get("models", [])]
    except Exception as e:
        return ["<error fetching model list: %s>" % (e,)]


def run_suite(model, cases, label):
    """Run all cases once for a given model; return list of result dicts."""
    results = []
    for case in cases:
        r = call_model(model, case["raw"])
        tok_s = None
        if r["ok"] and r["usage"]:
            completion_tokens = r["usage"].get("completion_tokens")
            if completion_tokens and r["latency_s"] > 0:
                tok_s = completion_tokens / r["latency_s"]
        result = {
            "model": model,
            "pass_label": label,
            "case_id": case["id"],
            "lang": case["lang"],
            "raw": case["raw"],
            "expectations": case["expectations"],
            "ok": r["ok"],
            "latency_s": round(r["latency_s"], 3),
            "output": r["content"],
            "usage": r["usage"],
            "tokens_per_sec": round(tok_s, 1) if tok_s else None,
            "error": r["error"],
        }
        results.append(result)
        sys.stderr.write(
            "[%s] %-28s %6.2fs  ok=%s\n"
            % (label, case["id"], r["latency_s"], r["ok"])
        )
        sys.stderr.flush()
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=False, help="Ollama model name/tag to benchmark")
    ap.add_argument(
        "--cases",
        default="tests/fixtures/llm_cases.json",
        help="Path to test-case JSON file",
    )
    ap.add_argument(
        "--passes",
        type=int,
        default=2,
        help="Number of full suite passes to run (pass 1 = cold-start-ish, "
        "last pass = warm; default 2)",
    )
    ap.add_argument("--out", default=None, help="Optional path to write full JSON results")
    ap.add_argument(
        "--list-models",
        action="store_true",
        help="Print models currently pulled in Ollama and exit",
    )
    args = ap.parse_args()

    if args.list_models:
        for m in get_available_models():
            print(m)
        return

    if not args.model:
        ap.error("--model is required unless --list-models is given")

    cases = load_cases(args.cases)

    all_results = []
    for p in range(1, args.passes + 1):
        label = "pass%d" % p
        if p == 1:
            label += "-cold"
        elif p == args.passes:
            label += "-warm"
        sys.stderr.write("\n=== %s: model=%s ===\n" % (label, args.model))
        pass_results = run_suite(args.model, cases, label)
        all_results.extend(pass_results)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        sys.stderr.write("\nWrote results to %s\n" % (args.out,))
    else:
        print(json.dumps(all_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
