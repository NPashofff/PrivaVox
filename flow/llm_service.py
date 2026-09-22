"""Lifecycle control for the local Ollama server.

PrivaVox talks to Ollama over HTTP and normally lets somebody else keep it
alive — the Homebrew service on macOS, Ollama.app on Windows. That leaves an
LLM server (and, for `keep_alive` minutes after a dictation, a multi-GB model)
resident long after PrivaVox is gone.

With `manage_ollama` on, PrivaVox runs its own: if nothing answers at boot we
start `ollama serve` as a child and stop it again on quit. Ownership is the
whole point — an Ollama that was ALREADY answering belongs to someone else
(the brew service, another app, the user's own terminal) and we never touch
it, neither at boot nor at quit.

Note this only bites once the always-on server is out of the way — on macOS
`brew services stop ollama`, on Windows quitting Ollama and clearing it from
Startup. While either keeps Ollama up, it answers before we look, so it is
never ours to stop.
"""
from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request

from .config import FlowConfig
from .platform_impl import IS_WINDOWS

# The `ollama serve` child we own, or None (nothing running, or the server is
# somebody else's). Only stop() clears it.
_proc: subprocess.Popen | None = None

# A .app bundle's PATH is not the login shell's, so which() can come up empty
# even with ollama installed; these are where each platform's installer puts
# it (brew and the .pkg on mac, winget's per-user install on Windows).
def _bin_fallbacks() -> tuple[str, ...]:
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        return (os.path.join(local, "Programs", "Ollama", "ollama.exe"),) if local else ()
    return ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama")


def base_url(config: FlowConfig) -> str:
    """http://host:port, from the chat-completions URL in the config."""
    return config.ollama_url.split("/v1/")[0]


def is_up(config: FlowConfig, timeout: float = 1.5) -> bool:
    """Does anything answer Ollama's /api/version on the configured host?"""
    try:
        with urllib.request.urlopen(f"{base_url(config)}/api/version", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def find_binary() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    return next((p for p in _bin_fallbacks() if os.access(p, os.X_OK)), None)


def owns_server() -> bool:
    """True while a serve child we started is still alive."""
    return _proc is not None and _proc.poll() is None


def ensure_running(config: FlowConfig, wait_s: float = 20.0) -> bool:
    """Start Ollama unless it is already up. Returns True if the server is ours.

    Called once at boot, before the LLM warm-up. Safe to call again: a serve
    child we already own short-circuits.
    """
    global _proc
    if not config.manage_ollama:
        return owns_server()
    if owns_server():
        return True
    if is_up(config):
        print("[flow.llm] Ollama вече върви — чужд процес, няма да го спираме на изход")
        return False
    binary = find_binary()
    if binary is None:
        print("[flow.llm] ollama не е намерен (PATH + пътищата на инсталаторите) — не мога да го пусна")
        return False

    print(f"[flow.llm] стартирам {binary} serve")
    if IS_WINDOWS:
        # No console window for the server (the shell runs under pythonw), and
        # its own process group so our Ctrl-C never reaches it.
        spawn_kwargs = {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                                          | subprocess.CREATE_NO_WINDOW)}
    else:
        # Own session (= own process group): stop() signals the whole group, so
        # the model runners ollama spawns die with the server, and a Ctrl-C in
        # the dev terminal does not kill the LLM mid-dictation.
        spawn_kwargs = {"start_new_session": True}
    try:
        _proc = subprocess.Popen(
            [binary, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **spawn_kwargs)
    except OSError as e:
        print(f"[flow.llm] ollama serve не тръгна: {e!r}")
        _proc = None
        return False

    # atexit covers the paths that skip _graceful_quit (an unhandled crash);
    # os._exit and SIGKILL still leak the child — nothing in-process can help.
    atexit.register(stop)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if is_up(config):
            print("[flow.llm] Ollama отговаря — наш процес, ще го спрем на изход")
            return True
        if _proc.poll() is not None:
            print(f"[flow.llm] ollama serve умря веднага (код {_proc.returncode})")
            _proc = None
            return False
        time.sleep(0.5)
    # Still ours even if slow to bind — the warm-up below reports the failure.
    print(f"[flow.llm] Ollama не отговори за {wait_s:g}s — продължавам, той е наш")
    return True


def disown() -> None:
    """Give up ownership without stopping: the server stays up past our quit.

    What the menu toggle does when switched OFF mid-session.
    """
    global _proc
    if owns_server():
        print("[flow.llm] Ollama вече не е наш — ще остане да върви след изход")
    _proc = None


def _signal_tree(proc: subprocess.Popen) -> None:
    """Ask the whole ollama tree to exit — the server AND the runner children
    it spawns to hold the model, which are the ones costing gigabytes.

    Killing only the server leaves those runners orphaned, which is exactly
    the leak this module exists to prevent.
    """
    if IS_WINDOWS:
        # Windows has no process groups to signal (CREATE_NEW_PROCESS_GROUP
        # only routes Ctrl events) and TerminateProcess does not walk children
        # — taskkill /T is the one call that does.
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False)
            return
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[flow.llm] taskkill не мина ({e!r}) — пробвам terminate")
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            return
        except OSError as e:  # already reaped, or no such group
            print(f"[flow.llm] killpg не мина ({e!r}) — пробвам terminate")
    proc.terminate()


def stop(timeout: float = 5.0) -> None:
    """Stop the serve child we started. No-op when Ollama is not ours."""
    global _proc
    proc, _proc = _proc, None
    if proc is None or proc.poll() is not None:
        return
    print("[flow.llm] спирам Ollama (наш процес)")
    _signal_tree(proc)
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        print("[flow.llm] Ollama не спря — kill")
    proc.kill()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        print("[flow.llm] Ollama не спря и с KILL — оставям го")
