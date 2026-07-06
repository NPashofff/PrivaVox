# PrivaVox

**Диктовка, която остава при теб.** / *Dictation that stays on your machine.*

PrivaVox is a fully-local, privacy-first dictation tool for macOS and Windows:
**hold the push-to-talk key → speak (English or Bulgarian) → release** — your
words are transcribed locally (Whisper), cleaned up locally (BgGPT via Ollama:
fillers removed, self-corrections resolved, punctuation fixed), and pasted
into whatever app has focus. No audio or text ever leaves the machine. A
personal dictionary biases transcription and cleanup toward your own names,
brands, and jargon.

## Инсталация / Install

Свали инсталатора за твоята система (последна версия) / download the latest
installer bundle:

| | |
|---|---|
| 🍎 **macOS** (Apple Silicon) | [PrivaVox-macOS.zip](https://github.com/NPashofff/PrivaVox/releases/latest/download/PrivaVox-macOS.zip) |
| 🪟 **Windows** (10/11, 64-bit) | [PrivaVox-Windows.zip](https://github.com/NPashofff/PrivaVox/releases/latest/download/PrivaVox-Windows.zip) |

Всички издания: [Releases](https://github.com/NPashofff/PrivaVox/releases).

**macOS:** разархивирай → двоен клик на **`Инсталирай PrivaVox.command`**.
Инсталаторът проверява/инсталира всичко (Ollama, Python средата, Whisper
модела), пита кой AI модел да свали (BgGPT 4B препоръчителен, 2.5 GB — 
размерите са в диалога), слага PrivaVox в Applications и по избор в Login
Items. При първия старт macOS иска стандартните разрешения (Accessibility,
Input Monitoring, Микрофон) — натискаш Allow и PrivaVox продължава сам.
Ползване: **задръж дясната ⌥ (Option) → говори → пусни**.

**Windows:** разархивирай → двоен клик на **`Инсталирай PrivaVox.bat`**.
Инсталаторът ползва winget за Ollama и uv, разпознава хардуера (NVIDIA →
GPU режим; иначе избор бърз/качествен CPU режим), сваля избраните модели и
слага пряк път в Start Menu (+ автостарт по избор).
Ползване: **задръж дясната Ctrl → говори → пусни**.

Менюто (Dock иконата на macOS / системната лента на Windows) има: избор на
език (авто/BG/EN), избор на AI модел, личния речник и лога.

## Quickstart (manual, for development)

Prereqs (from Phase 0): Python 3.12 venv at `.venv/` with `mlx-whisper`,
`ffmpeg` via brew, Ollama running (`brew services start ollama`) with
`todorov/bggpt` pulled, and `mlx-community/whisper-large-v3-turbo` in the HF
cache.

```bash
uv pip install --python .venv/bin/python sounddevice pynput pyobjc-framework-Quartz

# grant your terminal Microphone + Input Monitoring + Accessibility in
# System Settings -> Privacy & Security, then:
.venv/bin/python -m flow
```

Hold **right Option**, speak, release. Esc while recording cancels.

Other modes:

```bash
.venv/bin/python -m flow --dry-run                    # print instead of paste
.venv/bin/python -m flow --file clip.wav --lang bg    # run pipeline on a file
.venv/bin/python -m pytest tests/test_pipeline.py -v  # headless test suite
.venv/bin/python -m flow.server                       # ASR sidecar, http://127.0.0.1:8880
.venv/bin/python -m pytest tests/test_server.py -v    # sidecar test suite
```

## Status

- **Phase 0 — done**: model benchmarks. STT = whisper-large-v3-turbo,
  cleanup LLM = BgGPT-Gemma-3-4B via Ollama.
- **Phase 1 — done**: working Python MVP daemon; ~2 s warm end-to-end
  per dictation (EN ≈ 1.98 s, BG ≈ 2.22 s avg); 14/14 tests pass; the Phase 0
  Bulgarian self-correction bug is fixed via a few-shot prompt.
- **ASR sidecar**: localhost OpenAI-compatible `/v1/audio/transcriptions`
  HTTP server wrapping the same STT pipeline, for pluggability into openless
  or any OpenAI-audio-compatible client — `.venv/bin/python -m flow.server`.
- **Phase 3 — done**: personal dictionary. Add names/brands/jargon (EN or BG,
  one per line) to `dictionary.txt` and restart the daemon/sidecar — terms
  bias Whisper decoding (`initial_prompt`) and get their spelling normalized
  during cleanup.
- **Rhotacism compensation — optional accessibility feature**: the cleanup
  prompt can carry a gated speaker-profile block that restores "р"/"r" in
  words Whisper mis-hears when the speaker has rhotacism (р→л/в, r→w/l
  substitutions) — "отволи влатата" → "отвори вратата", "wead the wepowt" →
  "read the report" — while never touching legitimate л/в/w/l words
  ("молив" stays "молив", "walk" stays "walk"). Off by default: enable it
  with `{"speaker_rhotacism": true}` in the app's `settings.json`
  (macOS: `~/Library/Application Support/Flow/`, Windows:
  `%LOCALAPPDATA%\PrivaVox\`) or `FlowConfig(speaker_rhotacism=True)`.

## Layout

```
flow/        the package: config, audio, stt, cleanup, insert, hotkey, __main__
tests/       headless pipeline tests + LLM fixtures
test_audio/  synthetic EN/BG test clips (macOS `say`) + silence
scripts/     Phase 0 benchmark harnesses + release/runtime sync helpers
```
