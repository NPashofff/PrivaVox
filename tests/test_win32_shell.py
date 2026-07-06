"""W2: win32 shell pieces testable on macOS + the simulated-win32 checks.

The pystray tray app itself can only run on real Windows (W4), but its
settings merge-write, model normalization, tray-image fallback and module
import are platform-neutral — tested here. The subprocess tests re-run flow
with sys.platform faked to "win32" BEFORE any flow import (the W1 trick) to
prove the dispatch/path/config wiring picks the Windows branches.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from flow.config import FlowConfig

shell = pytest.importorskip(
    "flow.platform_win32.shell",
    reason="pystray/Pillow not installed (W2 dev dependencies)",
)

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# settings.json: tolerant load + merge-write (the Install-PrivaVox.ps1 contract)
# --------------------------------------------------------------------------

def test_load_settings_reads_installer_provisioned_keys(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "language_mode": "bg",
        "ollama_model": "todorov/bggpt:latest",
        "stt_engine": "faster-whisper-cuda",
        "stt_model": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "speaker_rhotacism": True,
    }), encoding="utf-8")
    config = FlowConfig()
    shell.load_settings(config, str(path))
    assert config.language_mode == "bg"
    assert config.ollama_model == "todorov/bggpt:latest"
    assert config.stt_engine == "faster-whisper-cuda"
    assert config.stt_model == "deepdml/faster-whisper-large-v3-turbo-ct2"
    assert config.speaker_rhotacism is True  # accessibility opt-in honored


def test_load_settings_is_tolerant(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "language_mode": "klingon",          # invalid → ignored
        "ollama_model": "",                  # empty → ignored
        "stt_engine": "whisper-cpp",         # unknown → ignored (loudly)
        "stt_model": 42,                     # wrong type → ignored
        "speaker_rhotacism": "да",           # wrong type (not bool) → ignored
        "future_key": {"x": 1},              # unknown → ignored
    }), encoding="utf-8")
    config = FlowConfig()
    shell.load_settings(config, str(path))
    defaults = FlowConfig()
    assert (config.language_mode, config.ollama_model,
            config.stt_engine, config.stt_model, config.speaker_rhotacism) == (
        defaults.language_mode, defaults.ollama_model,
        defaults.stt_engine, defaults.stt_model, defaults.speaker_rhotacism)


def test_load_settings_missing_and_corrupt_files(tmp_path):
    config = FlowConfig()
    shell.load_settings(config, str(tmp_path / "missing.json"))  # no raise
    bad = tmp_path / "bad.json"
    bad.write_text("{нещо счупено", encoding="utf-8")
    shell.load_settings(config, str(bad))                        # no raise
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2]", encoding="utf-8")
    shell.load_settings(config, str(lst))                        # no raise
    assert config.language_mode == FlowConfig().language_mode


def test_save_settings_merges_over_installer_keys(tmp_path):
    path = tmp_path / "settings.json"
    # what Install-PrivaVox.ps1 provisioned (plus a hypothetical future key)
    path.write_text(json.dumps({
        "stt_engine": "faster-whisper-cpu",
        "stt_model": "small",
        "ollama_model": "todorov/bggpt:latest",
        "speaker_rhotacism": True,
        "installer_marker": "v3",
    }), encoding="utf-8")
    config = FlowConfig()
    shell.load_settings(config, str(path))
    config.language_mode = "en"              # the user picked a language
    shell.save_settings(config, str(path))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {
        "language_mode": "en",
        "ollama_model": "todorov/bggpt:latest",
        "stt_engine": "faster-whisper-cpu",  # SURVIVED the save
        "stt_model": "small",                # SURVIVED the save
        "speaker_rhotacism": True,           # accessibility opt-in survives too
        "installer_marker": "v3",            # unknown keys survive too
    }


def test_save_settings_creates_fresh_file(tmp_path):
    path = tmp_path / "settings.json"
    config = FlowConfig(language_mode="bg")
    shell.save_settings(config, str(path))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["language_mode"] == "bg"
    assert set(on_disk) == {"language_mode", "ollama_model",
                            "stt_engine", "stt_model"}


# --------------------------------------------------------------------------
# small shell helpers
# --------------------------------------------------------------------------

def test_norm_model_matches_ollama_latest_semantics():
    assert shell._norm_model("todorov/bggpt") == "todorov/bggpt:latest"
    assert shell._norm_model("todorov/bggpt:latest") == "todorov/bggpt:latest"
    assert shell._norm_model("a:b") == "a:b"


def test_tray_image_loads_or_falls_back(tmp_path, monkeypatch):
    img = shell._load_tray_image()          # repo run: assets/menubar-icon.png
    assert img.size[0] > 0
    monkeypatch.chdir(tmp_path)             # nothing to load → drawn fallback
    img = shell._load_tray_image()
    assert img.mode == "RGBA" and img.size == (64, 64)


def test_play_never_raises_off_windows():
    shell._play("Pop")
    shell._play("несъществуващ")


# --------------------------------------------------------------------------
# dictionary token cap without mlx_whisper (the Windows runtime has none)
# --------------------------------------------------------------------------

def test_dictionary_prompt_works_without_mlx_tokenizer(monkeypatch):
    from flow import dictionary

    monkeypatch.setattr(dictionary, "_tokenizer", lambda: None)  # win32 regime
    d = dictionary.Dictionary(terms=("Примеров", "PrivaVox", "ротацизъм"))
    prompt = dictionary.build_stt_prompt(d)
    assert prompt is not None and prompt.startswith(dictionary.STT_PROMPT_PREFIX)
    assert "Примеров" in prompt
    assert dictionary.build_stt_prompt(dictionary.EMPTY) is None  # unchanged


def test_dictionary_fallback_estimate_is_conservative():
    from flow import dictionary

    tok = dictionary._tokenizer()
    if tok is None:  # pragma: no cover - only when mlx_whisper is absent
        pytest.skip("mlx_whisper missing here — nothing to compare against")
    for text in (
        " Речник/Glossary: Кузманов, Щърбева, въглехидрати, Джурджевич, кюфтенца",
        " ъглъчът щърбият по-щастлив",
        " PrivaVox BgGPT Ollama faster-whisper Примеров",
    ):
        real = len(tok.encode(text))
        estimate = max(1, -(-3 * len(text) // 4))  # the fallback formula
        assert estimate >= real, f"estimate {estimate} < real {real} for {text!r}"


def test_dictionary_fallback_cap_truncates(monkeypatch):
    from flow import dictionary

    monkeypatch.setattr(dictionary, "_tokenizer", lambda: None)
    many = tuple(f"термин{i:03}" for i in range(200))  # ~far past 120 tokens
    prompt = dictionary.build_stt_prompt(dictionary.Dictionary(terms=many))
    assert prompt is not None
    kept = prompt[len(dictionary.STT_PROMPT_PREFIX) + 1:].split(", ")
    assert 0 < len(kept) < 200                       # capped, not everything
    assert kept == list(many[: len(kept)])           # order preserved, prefix kept


# --------------------------------------------------------------------------
# simulated win32 (sys.platform faked before ANY flow import — the W1 trick)
# --------------------------------------------------------------------------

def _run_faked_win32(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "import sys; sys.platform = 'win32'\n" + code],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )


def test_simulated_win32_paths_and_dispatch():
    proc = _run_faked_win32(
        "import flow.platform_impl as p; assert p.IS_WINDOWS and not p.IS_MAC\n"
        "import flow.paths as paths\n"
        "assert paths.runtime_dir().endswith('PrivaVox'), paths.runtime_dir()\n"
        "assert paths.lock_path().endswith('flow.lock')\n"
        "assert paths.log_path().endswith('PrivaVox.log')\n"
        "import flow.stt as stt\n"
        "from flow.config import FlowConfig\n"
        "assert stt.resolve_engine_kind(FlowConfig()) == 'faster-whisper'\n"
        "assert stt.resolve_model(FlowConfig()) == stt.FASTER_WHISPER_DEFAULT_MODEL\n"
        "c = FlowConfig(stt_engine='faster-whisper-cpu', stt_model='small')\n"
        "assert stt.resolve_model(c) == 'small'\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_simulated_win32_modules_compile():
    # Full import needs Windows (msvcrt, pynput's win32 backend); syntax/AST
    # validity of every platform_win32 module is checkable here.
    proc = _run_faked_win32(
        "import ast, pathlib\n"
        "for p in sorted(pathlib.Path('flow/platform_win32').glob('*.py')):\n"
        "    src = p.read_text(encoding='utf-8')\n"
        "    ast.parse(src, filename=str(p))\n"
        "    compile(src, str(p), 'exec')\n"
        "print('OK')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_hotkey_auto_resolves_per_platform(monkeypatch):
    import flow.hotkey as hotkey
    from pynput import keyboard

    monkeypatch.setattr(hotkey, "IS_MAC", True)
    assert hotkey.resolve_hotkey("auto") == keyboard.Key.alt_r
    monkeypatch.setattr(hotkey, "IS_MAC", False)
    assert hotkey.resolve_hotkey("auto") == keyboard.Key.ctrl_r
    assert hotkey.resolve_hotkey("ctrl_r") == keyboard.Key.ctrl_r  # explicit
    with pytest.raises(ValueError, match="Unsupported hotkey"):
        hotkey.resolve_hotkey("hyper")


def test_mac_app_import_still_clean():
    proc = subprocess.run(
        [sys.executable, "-c", "import flow.app; print('OK')"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
